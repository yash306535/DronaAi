"""Unit tests for JWT security utilities (app/core/security.py).

Covers Requirement 1 acceptance criteria:
- 1.1: access token TTL = 15 min, refresh token TTL = 7 days.
- 1.3: a valid refresh token rotates into a new access + refresh pair.
- 1.4: rotation invalidates the submitted refresh token (no reuse).
- 1.5: expired/malformed/revoked refresh tokens are rejected; no token issued.
- 1.6: expired/malformed access tokens are rejected.
- 1.7: passwords are stored only as bcrypt hashes (never as raw text).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from jose import jwt

from app.core.config import Settings, get_settings
from app.core import security
from app.core.security import (
    RefreshTokenRegistry,
    TokenError,
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
    hash_password,
    issue_token_pair,
    rotate_refresh_token,
    verify_password,
)

SECRET_VALUE = "super-secret-jwt-value-1234567890"
API_KEY_VALUE = "sk-openai-secret-key-abcdef"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Provide deterministic settings with known secrets for every test."""
    monkeypatch.setenv("JWT_SECRET", SECRET_VALUE)
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY_VALUE)
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    settings = get_settings()
    return settings


@pytest.fixture
def registry() -> RefreshTokenRegistry:
    """A fresh, isolated refresh-token registry per test."""
    return RefreshTokenRegistry()


def _decode_raw(token: str, settings: Settings) -> dict:
    return jwt.decode(
        token,
        settings.JWT_SECRET.get_secret_value(),
        algorithms=[settings.JWT_ALGORITHM],
    )


# --- 1.7: password hashing --------------------------------------------------


def test_password_is_stored_only_as_bcrypt_hash() -> None:
    """1.7: hashing yields a bcrypt hash that does not contain the raw password."""
    raw = "correct horse battery"
    hashed = hash_password(raw)

    assert hashed != raw
    assert raw not in hashed
    # bcrypt hashes carry the $2b$ (or $2a$/$2y$) identifier prefix.
    assert hashed.startswith("$2")
    assert verify_password(raw, hashed) is True


def test_verify_password_rejects_wrong_password() -> None:
    hashed = hash_password("the-right-one")
    assert verify_password("the-wrong-one", hashed) is False


def test_verify_password_handles_malformed_hash() -> None:
    """A malformed stored hash yields False instead of raising."""
    assert verify_password("anything", "not-a-real-hash") is False


def test_hashing_same_password_twice_differs() -> None:
    """bcrypt salts each hash, so identical inputs produce distinct hashes."""
    h1 = hash_password("same-password")
    h2 = hash_password("same-password")
    assert h1 != h2


# --- 1.1: token TTLs --------------------------------------------------------


def test_access_token_ttl_is_15_minutes(_settings: Settings) -> None:
    """1.1: access token expires 15 minutes (ACCESS_TOKEN_TTL_MINUTES) after issue."""
    token = create_access_token("user-1", "admin", settings=_settings)
    claims = _decode_raw(token, _settings)

    ttl = claims["exp"] - claims["iat"]
    assert ttl == _settings.ACCESS_TOKEN_TTL_MINUTES * 60 == 15 * 60
    assert claims["type"] == TokenType.ACCESS.value
    assert claims["sub"] == "user-1"
    assert claims["role"] == "admin"


def test_refresh_token_ttl_is_7_days(
    _settings: Settings, registry: RefreshTokenRegistry
) -> None:
    """1.1: refresh token expires 7 days (REFRESH_TOKEN_TTL_DAYS) after issue."""
    token, jti = create_refresh_token(
        "user-1", "student", settings=_settings, registry=registry
    )
    claims = _decode_raw(token, _settings)

    ttl = claims["exp"] - claims["iat"]
    assert ttl == _settings.REFRESH_TOKEN_TTL_DAYS * 86400 == 7 * 86400
    assert claims["type"] == TokenType.REFRESH.value
    assert claims["jti"] == jti
    assert registry.is_active(jti)


def test_issue_token_pair_returns_both_tokens(
    _settings: Settings, registry: RefreshTokenRegistry
) -> None:
    """1.1: issue_token_pair returns a valid access + refresh pair."""
    pair = issue_token_pair("user-7", "invigilator", settings=_settings, registry=registry)

    access_claims = decode_access_token(pair.access_token, settings=_settings)
    refresh_claims = decode_refresh_token(
        pair.refresh_token, settings=_settings, registry=registry
    )
    assert access_claims["sub"] == "user-7"
    assert refresh_claims["sub"] == "user-7"
    assert registry.is_active(pair.refresh_jti)


# --- 1.3 / 1.4: rotation + invalidation -------------------------------------


def test_rotation_issues_new_pair(
    _settings: Settings, registry: RefreshTokenRegistry
) -> None:
    """1.3: a valid refresh token rotates into a new access + refresh pair."""
    pair = issue_token_pair("user-2", "student", settings=_settings, registry=registry)

    rotated = rotate_refresh_token(
        pair.refresh_token, settings=_settings, registry=registry
    )

    assert rotated.refresh_token != pair.refresh_token
    # The new tokens are valid.
    decode_access_token(rotated.access_token, settings=_settings)
    decode_refresh_token(rotated.refresh_token, settings=_settings, registry=registry)


def test_rotation_invalidates_old_refresh_token(
    _settings: Settings, registry: RefreshTokenRegistry
) -> None:
    """1.4: the submitted refresh token cannot be reused after rotation."""
    pair = issue_token_pair("user-3", "admin", settings=_settings, registry=registry)

    rotate_refresh_token(pair.refresh_token, settings=_settings, registry=registry)

    # Reusing the old refresh token must now fail.
    with pytest.raises(TokenError) as exc_info:
        decode_refresh_token(pair.refresh_token, settings=_settings, registry=registry)
    assert exc_info.value.code == "refresh_token_revoked"

    # And attempting to rotate it again must also fail (no new token issued).
    with pytest.raises(TokenError):
        rotate_refresh_token(pair.refresh_token, settings=_settings, registry=registry)


def test_rotated_token_chain_only_latest_valid(
    _settings: Settings, registry: RefreshTokenRegistry
) -> None:
    """Only the most recently issued refresh token in a chain stays valid."""
    pair = issue_token_pair("user-4", "student", settings=_settings, registry=registry)
    r1 = rotate_refresh_token(pair.refresh_token, settings=_settings, registry=registry)
    r2 = rotate_refresh_token(r1.refresh_token, settings=_settings, registry=registry)

    # Latest is valid.
    decode_refresh_token(r2.refresh_token, settings=_settings, registry=registry)
    # Earlier ones are revoked.
    with pytest.raises(TokenError):
        decode_refresh_token(r1.refresh_token, settings=_settings, registry=registry)
    with pytest.raises(TokenError):
        decode_refresh_token(pair.refresh_token, settings=_settings, registry=registry)


# --- 1.5: invalid refresh tokens rejected -----------------------------------


def test_expired_refresh_token_rejected(
    _settings: Settings, registry: RefreshTokenRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1.5: an expired refresh token is rejected."""
    # Force a negative TTL so the freshly minted token is already expired.
    monkeypatch.setattr(_settings, "REFRESH_TOKEN_TTL_DAYS", -1)
    token, _jti = create_refresh_token(
        "user-5", "student", settings=_settings, registry=registry
    )

    with pytest.raises(TokenError) as exc_info:
        decode_refresh_token(token, settings=_settings, registry=registry)
    assert exc_info.value.code in {"token_expired", "invalid_token"}


def test_malformed_refresh_token_rejected(
    _settings: Settings, registry: RefreshTokenRegistry
) -> None:
    """1.5: a malformed refresh token is rejected."""
    with pytest.raises(TokenError):
        decode_refresh_token("not.a.jwt", settings=_settings, registry=registry)


def test_access_token_rejected_at_refresh_endpoint(
    _settings: Settings, registry: RefreshTokenRegistry
) -> None:
    """1.5: presenting an access token where a refresh token is expected fails."""
    access = create_access_token("user-6", "admin", settings=_settings)
    with pytest.raises(TokenError):
        decode_refresh_token(access, settings=_settings, registry=registry)


# --- 1.6: invalid access tokens rejected ------------------------------------


def test_expired_access_token_rejected(
    _settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1.6: an expired access token is rejected."""
    monkeypatch.setattr(_settings, "ACCESS_TOKEN_TTL_MINUTES", -1)
    token = create_access_token("user-8", "admin", settings=_settings)

    with pytest.raises(TokenError):
        decode_access_token(token, settings=_settings)


def test_malformed_access_token_rejected(_settings: Settings) -> None:
    """1.6: a malformed access token is rejected."""
    with pytest.raises(TokenError):
        decode_access_token("garbage-token", settings=_settings)


def test_access_token_with_wrong_signature_rejected(_settings: Settings) -> None:
    """1.6: a token signed with a different key is rejected."""
    forged = jwt.encode(
        {"sub": "x", "role": "admin", "type": TokenType.ACCESS.value},
        "a-different-signing-key",
        algorithm=_settings.JWT_ALGORITHM,
    )
    with pytest.raises(TokenError):
        decode_access_token(forged, settings=_settings)


def test_refresh_token_rejected_at_access_path(
    _settings: Settings, registry: RefreshTokenRegistry
) -> None:
    """1.6: presenting a refresh token where an access token is expected fails."""
    token, _jti = create_refresh_token(
        "user-9", "student", settings=_settings, registry=registry
    )
    with pytest.raises(TokenError):
        decode_access_token(token, settings=_settings)


def test_empty_token_rejected(_settings: Settings) -> None:
    """1.6: an empty token string is rejected."""
    with pytest.raises(TokenError):
        decode_access_token("", settings=_settings)
