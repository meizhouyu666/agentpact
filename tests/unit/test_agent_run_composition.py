"""Formal Agent Run composition and fail-closed startup tests."""

from __future__ import annotations

import ast
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from enterprise.applications.agent_runs import compose_agent_run_service, mount_agent_run_application
from enterprise.auth.dependencies import get_current_user
from enterprise.auth.schemas import DepartmentRole, UserContext
from enterprise.governance.pack_runtime import PackRuntimeRegistry
from tests.fixtures.fake_domain_pack import (
    FAKE_PACK_ID,
    FAKE_PACK_VERSION,
    FAKE_RUNTIME_BINDING,
    FAKE_RUNTIME_CONTRACT,
    FakeDomainPackAdapter,
)

SECOND_PACK_ID = "second.domain"
SECOND_PACK_VERSION = "1.0.0"


@asynccontextmanager
async def _unused_session():
    raise AssertionError("fail-closed Pack selection must happen before database access")
    yield


def _operator() -> UserContext:
    return UserContext(
        user_id="composition-operator",
        org_id="composition-tenant",
        department_roles=[DepartmentRole(department_id="operations", department_name="Operations", role="operator")],
        business_line_ids=[],
    )


def test_formal_composition_mounts_empty_registry_and_rejects_pack_execution() -> None:
    application = FastAPI()
    service = mount_agent_run_application(application, session_factory=_unused_session)
    application.dependency_overrides[get_current_user] = _operator

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/enterprise/agent-runs/",
            json={
                "request_id": "composition-empty-registry",
                "intent": "Execute a business operation",
                "business_inputs": {},
                "pack_id": FAKE_PACK_ID,
                "pack_version": FAKE_PACK_VERSION,
            },
        )

    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "PACK_RUNTIME_UNAVAILABLE"}}
    assert application.state.agentpact_agent_run_service is service


def test_registered_runtime_requires_target_and_accepts_explicit_binding() -> None:
    registry = PackRuntimeRegistry([FAKE_RUNTIME_CONTRACT])
    registry.register(FakeDomainPackAdapter())

    with pytest.raises(ValueError, match="target URL is required"):
        compose_agent_run_service(_unused_session, runtime_registry=registry)

    service = compose_agent_run_service(
        _unused_session,
        runtime_registry=registry,
        target_url="https://operations.example.test",
        default_pack_binding=FAKE_RUNTIME_BINDING,
    )
    assert service is not None


def test_formal_composition_accepts_multiple_explicit_pack_runtimes() -> None:
    second_contract = FAKE_RUNTIME_CONTRACT.model_copy(
        update={
            "pack_id": SECOND_PACK_ID,
            "pack_version": SECOND_PACK_VERSION,
            "display_name": "Second Domain Pack",
            "manifest_digest": "e" * 64,
        }
    )
    second_binding = FAKE_RUNTIME_BINDING.model_copy(
        update={"pack_id": SECOND_PACK_ID, "pack_version": SECOND_PACK_VERSION}
    )
    registry = PackRuntimeRegistry([FAKE_RUNTIME_CONTRACT, second_contract])
    registry.register(FakeDomainPackAdapter())
    registry.register(FakeDomainPackAdapter(second_binding))

    service = compose_agent_run_service(
        _unused_session,
        runtime_registry=registry,
        target_url="https://operations.example.test",
    )

    assert {binding.pack_id for binding in service._registry.registered_bindings} == {
        FAKE_PACK_ID,
        SECOND_PACK_ID,
    }


def test_agent_run_mount_is_app_scoped_and_rejects_duplicate_mount() -> None:
    first = FastAPI()
    second = FastAPI()
    first_service = mount_agent_run_application(first, session_factory=_unused_session)
    second_service = mount_agent_run_application(second, session_factory=_unused_session)

    assert first_service is not second_service
    assert first.state.agentpact_agent_run_service is first_service
    assert second.state.agentpact_agent_run_service is second_service
    with pytest.raises(ValueError, match="already mounted"):
        mount_agent_run_application(first, session_factory=_unused_session)


def test_formal_startup_mounts_only_the_generic_composition_root() -> None:
    root = Path(__file__).resolve().parents[2]
    composition = (root / "enterprise" / "applications" / "agent_runs.py").read_text(encoding="utf-8")
    service = (root / "enterprise" / "agent_runs" / "service.py").read_text(encoding="utf-8")
    startup = (root / "skyvern" / "forge" / "api_app.py").read_text(encoding="utf-8")
    startup_tree = ast.parse(startup)
    factory = next(
        node for node in startup_tree.body if isinstance(node, ast.FunctionDef) and node.name == "create_api_app"
    )

    assert "mount_agent_run_application(" in startup
    assert [argument.arg for argument in factory.args.kwonlyargs] == ["agent_run_composition"]
    assert "enterprise.integrations" not in service
    for forbidden in ("synthetic_payment", "stripe_payment"):
        assert forbidden not in composition
        assert forbidden not in startup
