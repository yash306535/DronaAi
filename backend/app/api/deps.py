"""Authentication and RBAC dependencies (RBAC_Guard).

Implements Requirement 2 (Role-Based Access Control) and the request-time
authorization pieces of Requirements 15.8/15.9:

- ``get_current_user`` (Requirement 2.1): extracts the bearer access token from
  the ``Authorization`` header, validates it via
  :func:`app.core.security.decode_access_token`, and returns an
  :class:`AuthUser` carrying the subject id and role. A missing, malformed, or
  expired token is rejected with :class:`~app.core.errors.AuthError` (HTTP 401)
  *before any business logic runs* — because it is a FastAPI dependency it is
  resolved before the route handler body executes.
- ``require_role(*roles)`` (Requirements 2.2, 2.3, 15.9): a dependency factory
  that first authenticates the caller (so a bad token still yields 401 before
  logic) and then rejects an authenticated caller whose role is not in the
  allowed set with :class:`AuthError` (HTTP 403), again before any business
  logic runs.
- Exactly three roles are recognized (Requirement 2.3): Admin, Invigilator,
  Student — re-exported here from :class:`app.models.enums.Role`.
- ``require_student_ownership`` / :func:`enforce_student_ownership`
  (Requirements 2.4, 2.5, 15.8): an ownership helper that lets an Admin or
  Invigilator through but rejects a Student requesting a resource whose owning
  student id does not match their own id with HTTP 403, before any resource
  data is returned.

These dependencies never touch the database directly; the token is the source
of identity and role. Resource-level ownership is enforced by passing the
resource's owning-student id into :func:`enforce_student_ownership`.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request

from app.core.errors import AuthError
from app.core.security import TokenError, decode_access_token
from app.models.enums import Role

# Machine-readable error codes (kept stable for clients; never leak internals).
MISSING_TOKEN_CODE = "missing_token"
INVALID_TOKEN_CODE = "invalid_token"
FORBIDDEN_ROLE_CODE = "forbidden_role"
FORBIDDEN_OWNERSHIP_CODE = "forbidden_resource"

_BEARER_PREFIX = "bearer"


@dataclass(frozen=True, slots=True)
class AuthUser:
    """The authenticated principal derived from a validated access token.

    Carries only what the token asserts: the subject (user id) and the role.
    """

    id: str
    role: Role


def _extract_bearer_token(request: Request) -> str:
    """Return the bearer token from the ``Authorization`` header.

    Raises :class:`AuthError` (401) when the header is absent or not a
    well-formed ``Bearer <token>`` value (Requirement 2.1).
    """
    header = request.headers.get("Authorization")
    if not header:
        raise AuthError("Authentication required", code=MISSING_TOKEN_CODE)
    parts = header.split(" ", 1)
    if len(parts) != 2 or parts[0].strip().lower() != _BEARER_PREFIX:
        raise AuthError("Invalid authorization header", code=INVALID_TOKEN_CODE)
    token = parts[1].strip()
    if not token:
        raise AuthError("Authentication required", code=MISSING_TOKEN_CODE)
    return token


def _coerce_role(raw_role: object) -> Role:
    """Map a token's ``role`` claim to a recognized :class:`Role` (2.3).

    A claim that is not one of the exactly three recognized roles is treated as
    an invalid token (401) rather than silently granting access.
    """
    try:
        return Role(raw_role)
    except ValueError as exc:
        raise AuthError("Invalid token", code=INVALID_TOKEN_CODE) from exc


def get_current_user(request: Request) -> AuthUser:
    """FastAPI dependency that authenticates the caller (Requirement 2.1).

    Extracts and validates the bearer access token and returns the
    :class:`AuthUser`. A missing/malformed/expired token raises
    :class:`AuthError` (401) before any route business logic runs.
    """
    token = _extract_bearer_token(request)
    try:
        claims = decode_access_token(token)
    except TokenError as exc:
        # Preserve the security layer's machine-readable code (e.g.
        # ``token_expired`` / ``invalid_token``); never echo token contents.
        raise AuthError("Invalid or expired token", code=exc.code) from exc

    subject = claims.get("sub")
    if not subject or not isinstance(subject, str):
        raise AuthError("Invalid token", code=INVALID_TOKEN_CODE)
    role = _coerce_role(claims.get("role"))
    return AuthUser(id=subject, role=role)


def require_role(*roles: Role):
    """Build a dependency enforcing that the caller holds one of ``roles``.

    The returned dependency first authenticates the caller (so a missing/expired
    token yields 401 before any logic, Requirement 2.1) and then rejects a
    caller whose role is not in ``roles`` with HTTP 403 before business logic
    runs (Requirements 2.2, 15.9).
    """
    allowed = frozenset(roles)

    def _dependency(user: AuthUser = Depends(get_current_user)) -> AuthUser:
        if user.role not in allowed:
            raise AuthError(
                "Insufficient role for this resource",
                code=FORBIDDEN_ROLE_CODE,
                status_code=403,
            )
        return user

    return _dependency


def enforce_student_ownership(user: AuthUser, owner_student_id: str) -> None:
    """Reject a Student accessing a resource they do not own (2.4, 2.5, 15.8).

    Admins and Invigilators are permitted (they are authorized to view any
    session/paper by their role). A Student is permitted only when the
    resource's owning student id equals their own id; otherwise HTTP 403 is
    raised before any resource data is returned.
    """
    if user.role != Role.STUDENT:
        return
    if user.id != owner_student_id:
        raise AuthError(
            "You do not have access to this resource",
            code=FORBIDDEN_OWNERSHIP_CODE,
            status_code=403,
        )


__all__ = [
    "AuthUser",
    "Role",
    "get_current_user",
    "require_role",
    "enforce_student_ownership",
    "MISSING_TOKEN_CODE",
    "INVALID_TOKEN_CODE",
    "FORBIDDEN_ROLE_CODE",
    "FORBIDDEN_OWNERSHIP_CODE",
]
