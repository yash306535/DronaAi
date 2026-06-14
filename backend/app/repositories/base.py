"""Repository base class and transient-fault retry policy.

Implements the persistence-access contract for Requirements 15.7 and 15.10:

- **Parameterized access only (15.7)**: repositories perform every database
  operation through the SQLAlchemy ORM / Core ``select()`` API. Values are
  always passed as bound parameters; no query is ever built by concatenating
  or interpolating request-derived values into SQL text.
- **Retry-once on transient faults (15.10)**: a unit of work that fails with a
  *transient* fault (connection failure, connection/pool timeout, or deadlock,
  surfaced by SQLAlchemy as ``OperationalError`` / ``DBAPIError`` /
  ``TimeoutError``) is retried exactly once after a delay of at most 500 ms.
  Before each retry the session is rolled back so partially-applied work is
  discarded and persisted data is left unchanged. If the single retry also
  fails transiently, the fault is surfaced as :class:`UpstreamError` (HTTP 503)
  carrying the request id, again leaving persisted data unchanged.

Non-transient failures (e.g. ``IntegrityError`` from a constraint violation)
are never retried; the session is rolled back and the original exception is
re-raised so callers/handlers can map it appropriately.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.orm import Session

from app.core.errors import UpstreamError
from app.core.logging import get_logger

logger = get_logger("app.repositories")

T = TypeVar("T")

# Delay before the single permitted retry. Requirement 15.10 caps this at
# 500 ms; a short fixed back-off keeps latency low while letting a transient
# blip (e.g. a momentarily invalidated connection) clear.
TRANSIENT_RETRY_DELAY_SECONDS = 0.05
MAX_TRANSIENT_RETRY_DELAY_SECONDS = 0.5

# Substrings that identify a transient condition inside a generic ``DBAPIError``
# whose driver did not raise the more specific ``OperationalError``.
_TRANSIENT_MARKERS = ("deadlock", "timeout", "timed out", "connection")

# Stable, safe-to-expose error code surfaced when retries are exhausted. It
# identifies the failing dependency (the database) without leaking driver text.
DB_UNAVAILABLE_CODE = "UPSTREAM_DB_UNAVAILABLE"
DB_UNAVAILABLE_MESSAGE = "The database is temporarily unavailable. Please retry."


def is_transient_fault(exc: BaseException) -> bool:
    """Return ``True`` if ``exc`` represents a retryable transient DB fault.

    Per Requirement 15.10 the transient set is connection failure, connection
    timeout, and deadlock. These reach the application as:

    - ``sqlalchemy.exc.TimeoutError`` — connection-pool checkout timeout.
    - ``sqlalchemy.exc.OperationalError`` — the driver's class for lost
      connections, server-side timeouts, and deadlocks.
    - ``sqlalchemy.exc.DBAPIError`` with ``connection_invalidated`` set, or
      whose underlying driver message names a deadlock/timeout/connection issue.
    """
    if isinstance(exc, SATimeoutError):
        return True
    if isinstance(exc, OperationalError):
        return True
    if isinstance(exc, DBAPIError):
        if getattr(exc, "connection_invalidated", False):
            return True
        orig_text = str(getattr(exc, "orig", "") or "").lower()
        return any(marker in orig_text for marker in _TRANSIENT_MARKERS)
    return False


class BaseRepository:
    """Base for all repositories: owns the session and the retry policy.

    Subclasses express each operation as a closure over the session and run it
    through :meth:`_run`, which applies the transient-fault retry-once policy.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def _run(self, work: Callable[[Session], T], *, commit: bool) -> T:
        """Execute ``work`` (optionally committing) with retry-once semantics.

        Args:
            work: A callable receiving the session and returning a result. It
                must contain the full unit of work so a retry re-applies it
                from a clean state.
            commit: When ``True`` the session is committed after ``work``
                succeeds. Reads pass ``False``.

        Returns:
            Whatever ``work`` returns.

        Raises:
            UpstreamError: When a transient fault persists across the single
                retry (HTTP 503; data left unchanged).
            Exception: Any non-transient error is re-raised unchanged after the
                session is rolled back.
        """
        attempted_retry = False
        while True:
            try:
                result = work(self.session)
                if commit:
                    self.session.commit()
                return result
            except Exception as exc:  # noqa: BLE001 - classified below
                # Always discard partial work so persisted data is unchanged.
                self._safe_rollback()

                if not is_transient_fault(exc):
                    # Programming/constraint errors must surface unchanged.
                    raise

                if not attempted_retry:
                    attempted_retry = True
                    logger.warning(
                        "Transient database fault; retrying once",
                        extra={"errorType": type(exc).__name__},
                    )
                    time.sleep(TRANSIENT_RETRY_DELAY_SECONDS)
                    continue

                # The single permitted retry also failed transiently.
                logger.error(
                    "Transient database fault persisted after retry",
                    extra={"errorType": type(exc).__name__},
                )
                raise UpstreamError(
                    DB_UNAVAILABLE_MESSAGE, code=DB_UNAVAILABLE_CODE
                ) from exc

    def _safe_rollback(self) -> None:
        """Roll back the session, swallowing any secondary rollback failure."""
        try:
            self.session.rollback()
        except Exception:  # noqa: BLE001 - rollback failure must not mask cause
            logger.warning("Session rollback failed during fault handling")
