"""JWT security utilities: password hashing and token issuance/validation.

Implements Requirement 1 acceptance criteria:

- 1.1: ``issue_token_pair`` mints a JWT access token valid for 15 minutes
  (``ACCESS_TOKEN_TTL_MINUTES``) and a refresh token valid for 7 days
  (``REFRESH_TOKEN_TTL_DAYS``).
- 1.3: ``rotate_refresh_token`` validates an unexpired refresh token and issues
  a fresh access + refresh token pair.
- 1.4: rotation invalidates the submitted refresh token (its ``jti`` is revoked
  in the registry) so it cannot be reused.
- 1.5: an expired, malformed, or already-rotated refresh token is rejected by
  raising ``TokenError`` (which the auth layer maps to a 401); no new token is
  issued.
- 1.6: an expired, malformed, or otherwise invalid access token is rejected by
  ``decode_access_token`` raising ``TokenError``.
- 1.7: passwords are stored only as bcrypt hashes via ``hash_password``; raw
  passwords are never persisted or logged by this module.

Design references: design.md "Auth & RBAC" — JWT (access + refresh) via
``python-jose``; passwords hashed with ``passlib[bcrypt]``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import Settings, get_settings

# --- Password hashing -------------------------------------------------------

# bcrypt has a 72-byte input limit; passlib handles truncation detection. We
# constrain password length in the API/schema layer (8..128 chars) per 1.8.
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """Return a bcrypt hash for ``password`` (Requirement 1.7).

    The raw password is never returned, persisted, or logged by this function.
    """
    return _pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Return ``True`` iff ``password`` matches the stored bcrypt ``password_hash``.

    A malformed or unknown hash yields ``False`` rather than raising, so callers
    can present a uniform non-disclosing failure (Requirement 1.2).
    """
    try:
        return _pwd_context.verify(password, password_hash)
    except (ValueError, TypeError):
        return False


# --- Tokens -----------------------------------------------------------------


class TokenType(StrEnum):
    """Discriminates access tokens from refresh tokens via the ``type`` claim."""

    ACCESS = "access"
    REFRESH = "refresh"


class TokenError(Exception):
    """Raised when a token is missing, malformed, expired, or revoked.

    Carries a machine-readable ``code`` the auth layer maps to a 401 response
    (Requirements 1.5, 1.6, 2.1). The message is intentionally generic and never
    contains secret material or token contents.
    """

    def __init__(self, code: str = "invalid_token", message: str = "Invalid token") -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class TokenPair:
    """A freshly issued access + refresh token pair (Requirements 1.1, 1.3)."""

    __slots__ = ("access_token", "refresh_token", "refresh_jti")

    def __init__(self, access_token: str, refresh_token: str, refresh_jti: str) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.refresh_jti = refresh_jti


class RefreshTokenRegistry:
    """Tracks active refresh-token ``jti`` values to support rotation/invalidation.

    A refresh token is only honored if its ``jti`` is still registered as active.
    Rotating a token revokes the old ``jti`` and registers the new one, so a
    rotated (or explicitly revoked) refresh token can never be reused
    (Requirement 1.4). This is the in-process implementation; it can later be
    backed by a shared store (e.g. Redis) without changing callers.
    """

    def __init__(self) -> None:
        self._active: set[str] = set()

    def register(self, jti: str) -> None:
        """Mark ``jti`` as an active, usable refresh-token identifier."""
        self._active.add(jti)

    def is_active(self, jti: str) -> bool:
        """Return whether ``jti`` is currently active (not revoked/rotated)."""
        return jti in self._active

    def revoke(self, jti: str) -> None:
        """Invalidate ``jti`` so the corresponding refresh token cannot be reused."""
        self._active.discard(jti)

    def clear(self) -> None:
        """Drop all active jtis (primarily for tests)."""
        self._active.clear()


# Process-wide registry instance used by the convenience helpers below.
_default_registry = RefreshTokenRegistry()


def get_refresh_registry() -> RefreshTokenRegistry:
    """Return the process-wide refresh-token registry."""
    return _default_registry


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _encode(claims: dict[str, Any], settings: Settings) -> str:
    secret = settings.JWT_SECRET
    if secret is None:
        # Should never happen: get_settings() aborts startup if JWT_SECRET is
        # absent. Guard anyway so we never sign with an empty key.
        raise TokenError("server_misconfigured", "Signing key unavailable")
    return jwt.encode(
        claims, secret.get_secret_value(), algorithm=settings.JWT_ALGORITHM
    )


def create_access_token(
    subject: str,
    role: str,
    *,
    settings: Settings | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Create a signed access token valid for ``ACCESS_TOKEN_TTL_MINUTES`` (1.1)."""
    settings = settings or get_settings()
    now = _now()
    expires = now + timedelta(minutes=settings.ACCESS_TOKEN_TTL_MINUTES)
    claims: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "type": TokenType.ACCESS.value,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    if extra_claims:
        claims.update(extra_claims)
    return _encode(claims, settings)


def create_refresh_token(
    subject: str,
    role: str,
    *,
    settings: Settings | None = None,
    registry: RefreshTokenRegistry | None = None,
) -> tuple[str, str]:
    """Create a refresh token valid for ``REFRESH_TOKEN_TTL_DAYS`` (1.1).

    The token's ``jti`` is registered as active so it can be honored exactly
    once before rotation. Returns ``(token, jti)``.
    """
    settings = settings or get_settings()
    registry = registry if registry is not None else _default_registry
    now = _now()
    expires = now + timedelta(days=settings.REFRESH_TOKEN_TTL_DAYS)
    jti = uuid.uuid4().hex
    claims: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "type": TokenType.REFRESH.value,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "jti": jti,
    }
    token = _encode(claims, settings)
    registry.register(jti)
    return token, jti


def issue_token_pair(
    subject: str,
    role: str,
    *,
    settings: Settings | None = None,
    registry: RefreshTokenRegistry | None = None,
) -> TokenPair:
    """Issue a fresh access + refresh token pair for a subject (Requirement 1.1)."""
    settings = settings or get_settings()
    registry = registry if registry is not None else _default_registry
    access_token = create_access_token(subject, role, settings=settings)
    refresh_token, refresh_jti = create_refresh_token(
        subject, role, settings=settings, registry=registry
    )
    return TokenPair(access_token, refresh_token, refresh_jti)


def _decode(token: str, settings: Settings) -> dict[str, Any]:
    secret = settings.JWT_SECRET
    if secret is None:
        raise TokenError("server_misconfigured", "Signing key unavailable")
    try:
        return jwt.decode(
            token,
            secret.get_secret_value(),
            algorithms=[settings.JWT_ALGORITHM],
        )
    except JWTError as exc:  # expired, bad signature, malformed, etc.
        # python-jose raises ExpiredSignatureError (a JWTError subclass) for
        # expiry; distinguish it for a more specific machine-readable code.
        code = "token_expired" if "expire" in str(exc).lower() else "invalid_token"
        raise TokenError(code, "Invalid or expired token") from exc


def decode_token(
    token: str,
    *,
    expected_type: TokenType | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Decode and validate a token, returning its claims.

    Raises ``TokenError`` on a missing, malformed, expired, or wrong-type token
    (Requirements 1.5, 1.6). When ``expected_type`` is given, the token's
    ``type`` claim must match.
    """
    settings = settings or get_settings()
    if not token or not isinstance(token, str):
        raise TokenError("invalid_token", "Missing token")
    claims = _decode(token, settings)
    if expected_type is not None and claims.get("type") != expected_type.value:
        raise TokenError("invalid_token", "Unexpected token type")
    return claims


def decode_access_token(
    token: str, *, settings: Settings | None = None
) -> dict[str, Any]:
    """Decode and validate an access token (Requirement 1.6)."""
    return decode_token(token, expected_type=TokenType.ACCESS, settings=settings)


def decode_refresh_token(
    token: str,
    *,
    settings: Settings | None = None,
    registry: RefreshTokenRegistry | None = None,
) -> dict[str, Any]:
    """Decode and validate a refresh token, including its active-jti status.

    Raises ``TokenError`` if the token is expired, malformed, not a refresh
    token, or has been rotated/revoked (Requirement 1.5).
    """
    registry = registry if registry is not None else _default_registry
    claims = decode_token(token, expected_type=TokenType.REFRESH, settings=settings)
    jti = claims.get("jti")
    if not jti or not registry.is_active(jti):
        # Already rotated, revoked, or never registered -> not reusable.
        raise TokenError("refresh_token_revoked", "Refresh token is no longer valid")
    return claims


def rotate_refresh_token(
    refresh_token: str,
    *,
    settings: Settings | None = None,
    registry: RefreshTokenRegistry | None = None,
) -> TokenPair:
    """Rotate a valid refresh token into a new access + refresh pair.

    Validates the submitted refresh token (Requirement 1.3), then invalidates
    its ``jti`` so it cannot be reused (Requirement 1.4) before issuing the new
    pair. Raises ``TokenError`` for an expired/malformed/revoked token without
    issuing any new token (Requirement 1.5).
    """
    settings = settings or get_settings()
    registry = registry if registry is not None else _default_registry

    claims = decode_refresh_token(
        refresh_token, settings=settings, registry=registry
    )

    # Invalidate the presented refresh token first so it can never be reused,
    # even if it is replayed concurrently.
    registry.revoke(claims["jti"])

    subject = claims["sub"]
    role = claims.get("role", "")
    return issue_token_pair(subject, role, settings=settings, registry=registry)
