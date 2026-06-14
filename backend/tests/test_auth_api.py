"""Unit tests for the auth router and RBAC dependencies (tasks 5.1, 5.2, 5.4).

Covers Requirement 1 (login/refresh/me happy + failure paths, non-disclosing
401, password-length 400, expired/malformed token rejection) and the
request-time pieces of Requirement 2 (missing/expired token -> 401 before
business logic).

A small FastAPI app mounts the real auth router plus a couple of role-guarded
routes so the dependencies are exercised end-to-end against an in-memory DB.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import auth as auth_module
from app.api.auth import (
    INVALID_CREDENTIALS_CODE,
    PASSWORD_LENGTH_CODE,
    router as auth_router,
)
from app.api.deps import AuthUser, enforce_student_ownership, require_role
from app.core.config import Settings, get_settings
from app.core.db import Base, get_db
from app.core.errors import register_error_handlers
from app.core.logging import RequestIdMiddleware
from app.core.security import (
    create_access_token,
    get_refresh_registry,
    hash_password,
    issue_token_pair,
)
from app.models.enums import Role
from app.models.orm import User

SECRET_VALUE = "super-secret-jwt-value-for-auth-api-tests-123456"
API_KEY_VALUE = "sk-openai-secret-key-for-auth-api-tests"

STUDENT_PASSWORD = "studentpass1"
ADMIN_PASSWORD = "adminpass123"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Deterministic settings with known secrets for every test."""
    monkeypatch.setenv("JWT_SECRET", SECRET_VALUE)
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY_VALUE)
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    # Isolate the process-wide refresh-token registry between tests.
    get_refresh_registry().clear()
    return get_settings()


@pytest.fixture()
def session_factory():
    import app.models  # noqa: F401 - register tables

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
    yield factory
    engine.dispose()


@pytest.fixture()
def seeded_users(session_factory):
    """Create one student and one admin; return their records."""
    session = session_factory()
    student = User(
        email="student@example.com",
        full_name="Stu Dent",
        role=Role.STUDENT,
        password_hash=hash_password(STUDENT_PASSWORD),
    )
    admin = User(
        email="admin@example.com",
        full_name="Ada Min",
        role=Role.ADMIN,
        password_hash=hash_password(ADMIN_PASSWORD),
    )
    session.add_all([student, admin])
    session.commit()
    session.refresh(student)
    session.refresh(admin)
    ids = {"student": student.id, "admin": admin.id}
    session.close()
    return ids


@pytest.fixture()
def client(session_factory):
    """A test app mounting the auth router + role-guarded probe routes."""
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    register_error_handlers(app)
    app.include_router(auth_router)

    @app.get("/admin-only")
    def _admin_only(user: AuthUser = Depends(require_role(Role.ADMIN))):
        # Business logic sentinel: only reached when role check passes.
        return {"ok": True, "user": user.id}

    @app.get("/staff-only")
    def _staff_only(
        user: AuthUser = Depends(require_role(Role.ADMIN, Role.INVIGILATOR)),
    ):
        return {"ok": True, "user": user.id}

    @app.get("/sessions/{owner_id}")
    def _owned_resource(
        owner_id: str, user: AuthUser = Depends(require_role(*list(Role)))
    ):
        enforce_student_ownership(user, owner_id)
        return {"ok": True, "owner": owner_id}

    def _override_get_db():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db
    return TestClient(app, raise_server_exceptions=False)


# --- /auth/login ------------------------------------------------------------


def test_login_success_returns_token_pair(client, seeded_users) -> None:
    """1.1: valid credentials return an access + refresh token pair."""
    resp = client.post(
        "/auth/login",
        json={"email": "student@example.com", "password": STUDENT_PASSWORD},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["token_type"] == "bearer"


def test_login_wrong_password_is_non_disclosing_401(client, seeded_users) -> None:
    """1.2: a wrong password yields a 401 with a non-disclosing code."""
    resp = client.post(
        "/auth/login",
        json={"email": "student@example.com", "password": "wrongpassword"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == INVALID_CREDENTIALS_CODE


def test_login_unknown_email_uses_same_code_as_wrong_password(
    client, seeded_users
) -> None:
    """1.2: an unknown email returns the SAME code as a wrong password."""
    resp = client.post(
        "/auth/login",
        json={"email": "nobody@example.com", "password": STUDENT_PASSWORD},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == INVALID_CREDENTIALS_CODE


def test_login_short_password_is_400_with_length_code(client, seeded_users) -> None:
    """1.8: a too-short password yields a 400 naming the length constraint."""
    resp = client.post(
        "/auth/login",
        json={"email": "student@example.com", "password": "short"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == PASSWORD_LENGTH_CODE


def test_login_overlong_password_is_400(client, seeded_users) -> None:
    """1.8: a too-long password (>128) yields a 400 length error."""
    resp = client.post(
        "/auth/login",
        json={"email": "student@example.com", "password": "x" * 129},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == PASSWORD_LENGTH_CODE


# --- /auth/refresh ----------------------------------------------------------


def test_refresh_rotates_and_invalidates_prior_token(client, seeded_users) -> None:
    """1.3/1.4: refresh issues a new pair and the old token cannot be reused."""
    login = client.post(
        "/auth/login",
        json={"email": "student@example.com", "password": STUDENT_PASSWORD},
    ).json()
    old_refresh = login["refresh_token"]

    rotated = client.post("/auth/refresh", json={"refresh_token": old_refresh})
    assert rotated.status_code == 200
    assert rotated.json()["access_token"]

    # Reusing the original refresh token is rejected (1.4, 1.5).
    reused = client.post("/auth/refresh", json={"refresh_token": old_refresh})
    assert reused.status_code == 401


def test_refresh_malformed_token_is_401(client, seeded_users) -> None:
    """1.5: a malformed refresh token is rejected with 401, no token issued."""
    resp = client.post("/auth/refresh", json={"refresh_token": "not-a-jwt"})
    assert resp.status_code == 401
    assert "access_token" not in resp.json()


# --- /auth/me ---------------------------------------------------------------


def test_me_returns_profile_with_role(client, seeded_users) -> None:
    """1.9: /auth/me returns the caller's profile including the role."""
    login = client.post(
        "/auth/login",
        json={"email": "admin@example.com", "password": ADMIN_PASSWORD},
    ).json()
    resp = client.get(
        "/auth/me", headers={"Authorization": f"Bearer {login['access_token']}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "admin@example.com"
    assert body["role"] == Role.ADMIN.value


def test_me_without_token_is_401(client, seeded_users) -> None:
    """2.1: a missing access token is rejected with 401 before any logic."""
    resp = client.get("/auth/me")
    assert resp.status_code == 401


def test_me_with_malformed_token_is_401(client, seeded_users) -> None:
    """1.6: a malformed access token is rejected with 401."""
    resp = client.get("/auth/me", headers={"Authorization": "Bearer garbage.token"})
    assert resp.status_code == 401


def test_me_with_expired_token_is_401(client, seeded_users, _settings) -> None:
    """1.6: an expired access token is rejected with 401."""
    settings = _settings
    # Mint an access token that expired an hour ago.
    expired = jwt.encode(
        {
            "sub": seeded_users["admin"],
            "role": Role.ADMIN.value,
            "type": "access",
            "exp": int((__import__("time").time()) - 3600),
        },
        settings.JWT_SECRET.get_secret_value(),
        algorithm=settings.JWT_ALGORITHM,
    )
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401


# --- RBAC role guards (2.1, 2.2) -------------------------------------------


def test_role_guard_allows_matching_role(client, seeded_users) -> None:
    token = issue_token_pair(seeded_users["admin"], Role.ADMIN.value).access_token
    resp = client.get("/admin-only", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_role_guard_rejects_wrong_role_with_403(client, seeded_users) -> None:
    """2.2: an authenticated caller with the wrong role gets 403 before logic."""
    token = issue_token_pair(seeded_users["student"], Role.STUDENT.value).access_token
    resp = client.get("/admin-only", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_role_guard_missing_token_is_401_before_role_check(client) -> None:
    """2.1: missing token yields 401 (not 403) before any role/business logic."""
    resp = client.get("/admin-only")
    assert resp.status_code == 401


# --- Student ownership (2.4, 2.5) ------------------------------------------


def test_student_can_access_own_resource(client, seeded_users) -> None:
    sid = seeded_users["student"]
    token = issue_token_pair(sid, Role.STUDENT.value).access_token
    resp = client.get(f"/sessions/{sid}", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_student_cannot_access_other_students_resource(client, seeded_users) -> None:
    """2.5: a Student requesting another student's resource is rejected 403."""
    token = issue_token_pair(seeded_users["student"], Role.STUDENT.value).access_token
    resp = client.get(
        "/sessions/some-other-student-id",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_admin_can_access_any_students_resource(client, seeded_users) -> None:
    """2.4: an Admin is not blocked by Student ownership checks."""
    token = issue_token_pair(seeded_users["admin"], Role.ADMIN.value).access_token
    resp = client.get(
        "/sessions/any-student-id", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
