"""Authentication endpoints (Auth_Service).

Implements Requirement 1 (Authentication and Token Management):

- ``POST /auth/login`` (1.1, 1.2, 1.8): validates email+password, returns a
  fresh access (15 min) + refresh (7 day) token pair on success. Bad
  credentials are rejected with a 401 and a single, non-disclosing error code
  that does not reveal whether the email or the password was wrong (1.2). A
  password whose length is outside 8..128 is rejected with a 400 and a
  machine-readable code naming the length constraint, without issuing tokens
  (1.8).
- ``POST /auth/refresh`` (1.3, 1.4, 1.5): rotates a valid refresh token into a
  new pair, invalidating the submitted token so it cannot be reused. An
  expired/malformed/revoked token yields a 401 and no new token.
- ``GET /auth/me`` (1.9, 1.6): returns the authenticated caller's profile
  (including role) for a valid access token; a missing/expired/invalid access
  token is rejected with a 401 by the :func:`get_current_user` dependency.

The router holds no business logic beyond credential verification and token
issuance; it delegates hashing/token work to :mod:`app.core.security` and user
lookups to :class:`~app.repositories.user.UserRepository`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.api.deps import AuthUser, get_current_user
from app.core.db import get_db
from app.core.errors import AppError, AuthError
from app.core.security import (
    TokenError,
    issue_token_pair,
    rotate_refresh_token,
    verify_password,
)
from app.repositories.user import UserRepository
from app.schemas.user import (
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    RefreshRequest,
    TokenPair,
    UserRead,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# Single non-disclosing credential-failure code (1.2): it never indicates
# whether the email or the password was the incorrect part.
INVALID_CREDENTIALS_CODE = "invalid_credentials"
PASSWORD_LENGTH_CODE = "password_length"
INVALID_REFRESH_CODE = "invalid_refresh_token"


class LoginInput(BaseModel):
    """Login credentials.

    ``password`` is intentionally unconstrained at the schema layer so that a
    length violation surfaces as a 400 with a specific code from the endpoint
    (Requirement 1.8) rather than a generic 422 validation envelope.
    """

    email: EmailStr
    password: str


def _validate_password_length(password: str) -> None:
    """Reject passwords outside the 8..128 length window with a 400 (1.8)."""
    if not (PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH):
        raise AppError(
            "Password must be between "
            f"{PASSWORD_MIN_LENGTH} and {PASSWORD_MAX_LENGTH} characters",
            code=PASSWORD_LENGTH_CODE,
            status_code=400,
        )


@router.post("/login", response_model=TokenPair)
def login(credentials: LoginInput, db: Session = Depends(get_db)) -> TokenPair:
    """Authenticate a user and issue an access + refresh token pair (1.1)."""
    # Enforce the length constraint first (1.8) so out-of-range inputs get the
    # specific 400 code rather than being treated as a credential mismatch.
    _validate_password_length(credentials.password)

    user = UserRepository(db).get_by_email(credentials.email)
    # Verify against the stored hash. A missing user and a wrong password both
    # take the same path and return the same code (1.2: non-disclosing).
    if user is None or not verify_password(credentials.password, user.password_hash):
        raise AuthError("Invalid email or password", code=INVALID_CREDENTIALS_CODE)

    pair = issue_token_pair(user.id, str(user.role))
    return TokenPair(access_token=pair.access_token, refresh_token=pair.refresh_token)


@router.post("/refresh", response_model=TokenPair)
def refresh(body: RefreshRequest) -> TokenPair:
    """Rotate a valid refresh token into a new pair (1.3, 1.4, 1.5)."""
    try:
        pair = rotate_refresh_token(body.refresh_token)
    except TokenError as exc:
        # Expired/malformed/revoked -> 401, no new token issued (1.5).
        raise AuthError(
            "Invalid or expired refresh token", code=exc.code or INVALID_REFRESH_CODE
        ) from exc
    return TokenPair(access_token=pair.access_token, refresh_token=pair.refresh_token)


@router.get("/me", response_model=UserRead)
def me(
    current: AuthUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserRead:
    """Return the authenticated user's profile including role (1.9)."""
    user = UserRepository(db).get(current.id)
    if user is None:
        # Token is valid but the account no longer exists; treat as unauthorized.
        raise AuthError("Account not found", code="account_not_found")
    return UserRead.model_validate(user)


__all__ = [
    "router",
    "LoginInput",
    "INVALID_CREDENTIALS_CODE",
    "PASSWORD_LENGTH_CODE",
    "INVALID_REFRESH_CODE",
]
