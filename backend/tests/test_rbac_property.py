"""Property test for RBAC enforcement (task 5.3).

**Property 6: RBAC enforcement** — for all protected endpoints a request whose
authenticated role is not in the endpoint's required-role set is rejected with
403 *before any business logic runs*.

**Validates: Requirements 2.2, 15.9**

Strategy: generate an arbitrary non-empty set of required roles and an arbitrary
caller role drawn from the exactly three recognized roles. Mount a guarded probe
route whose handler flips a side-effect flag; the property asserts that whenever
the caller's role is excluded the response is 403 and the side-effect flag was
never set (business logic did not run). The complementary allowed case is also
checked to keep the generator honest.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from app.api.deps import AuthUser, require_role
from app.core.config import Settings, get_settings
from app.core.errors import register_error_handlers
from app.core.logging import RequestIdMiddleware
from app.core.security import issue_token_pair
from app.models.enums import Role

SECRET_VALUE = "super-secret-jwt-value-for-rbac-property-tests-1234"
API_KEY_VALUE = "sk-openai-secret-key-for-rbac-property-tests"

ALL_ROLES = list(Role)


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("JWT_SECRET", SECRET_VALUE)
    monkeypatch.setenv("OPENAI_API_KEY", API_KEY_VALUE)
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
    return get_settings()


def _build_client(required_roles: frozenset[Role]) -> tuple[TestClient, dict]:
    """Build a probe app guarding a route by ``required_roles``.

    The handler sets ``state["ran"] = True`` so the test can assert business
    logic never executed when the request is rejected.
    """
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    register_error_handlers(app)
    state = {"ran": False}

    @app.get("/protected")
    def _protected(user: AuthUser = Depends(require_role(*required_roles))):
        state["ran"] = True
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False), state


@hyp_settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    required=st.lists(
        st.sampled_from(ALL_ROLES), min_size=1, max_size=3, unique=True
    ).map(frozenset),
    caller=st.sampled_from(ALL_ROLES),
)
def test_rbac_rejects_unauthorized_role_before_business_logic(
    required: frozenset[Role], caller: Role, _settings
) -> None:
    client, state = _build_client(required)
    token = issue_token_pair("user-123", caller.value).access_token

    resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    if caller in required:
        # Authorized: business logic runs and the route succeeds.
        assert resp.status_code == 200
        assert state["ran"] is True
    else:
        # Unauthorized: 403 and business logic never executed (2.2, 15.9).
        assert resp.status_code == 403
        assert state["ran"] is False
