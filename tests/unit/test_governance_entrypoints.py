"""Regression checks for known browser-execution bypasses and their inventory."""

import ast
from pathlib import Path

import pytest

from enterprise.governance.execution_profiles import ExecutionMechanism, ExecutionProfile
from skyvern.config import settings
from skyvern.governance_guard import (
    GovernedScriptExecutionDisabled,
    MissingExecutionAuthorization,
    MissingExecutionProfile,
    assert_execution_authorization_present,
    assert_script_execution_is_not_governed,
    has_complete_governed_execution_context,
    precomputed_action_reuse_is_allowed,
)

ROOT = Path(__file__).resolve().parents[2]


def test_script_execution_is_disabled_when_governance_enforce_is_active(monkeypatch):
    monkeypatch.setattr(settings, "GOVERNANCE_MODE", "enforce")

    with pytest.raises(GovernedScriptExecutionDisabled, match="disabled"):
        assert_script_execution_is_not_governed()


def test_script_execution_remains_available_in_off_and_audit_modes(monkeypatch):
    monkeypatch.setattr(settings, "GOVERNANCE_MODE", "off")
    assert_script_execution_is_not_governed()
    monkeypatch.setattr(settings, "GOVERNANCE_MODE", "audit")
    assert_script_execution_is_not_governed()


def test_action_handler_rejects_ungoverned_enforce_entry(monkeypatch):
    monkeypatch.setattr(settings, "GOVERNANCE_MODE", "enforce")

    with pytest.raises(MissingExecutionAuthorization, match="requires ExecutionAuthorization"):
        assert_execution_authorization_present(None)


def test_action_handler_requires_authorization_and_profile_as_one_context(monkeypatch):
    profile = ExecutionProfile(mechanism=ExecutionMechanism.LOCATOR, evidence_refs=["dom:button"])
    monkeypatch.setattr(settings, "GOVERNANCE_MODE", "audit")

    assert has_complete_governed_execution_context(None, None) is False
    with pytest.raises(MissingExecutionAuthorization, match="without ExecutionAuthorization"):
        has_complete_governed_execution_context(None, profile)
    with pytest.raises(MissingExecutionProfile, match="requires ExecutionProfile"):
        has_complete_governed_execution_context(object(), None)
    assert has_complete_governed_execution_context(object(), profile) is True


def test_precomputed_action_reuse_guard_preserves_legacy_modes_and_rejects_enforce(monkeypatch):
    monkeypatch.setattr(settings, "GOVERNANCE_MODE", "off")
    assert precomputed_action_reuse_is_allowed() is True
    monkeypatch.setattr(settings, "GOVERNANCE_MODE", "audit")
    assert precomputed_action_reuse_is_allowed() is True
    monkeypatch.setattr(settings, "GOVERNANCE_MODE", "enforce")
    assert precomputed_action_reuse_is_allowed() is False


def test_handler_mechanisms_require_profile_authorization_and_attempt():
    """Pin the dormant governed boundary and its internal fallback controls."""

    path = ROOT / "skyvern/webeye/actions/handler.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    handler = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ActionHandler")
    public = _find_function(handler, "handle_action")
    governed = _find_function(handler, "_handle_governed_action")

    assert {argument.arg for argument in public.args.args} >= {
        "cua_execution_evidence",
        "execution_authorization",
        "execution_profile",
    }
    assert "has_complete_governed_execution_context" in {
        name for node in ast.walk(public) if isinstance(node, ast.Call) and (name := _call_name(node))
    }

    call_lines: dict[str, list[int]] = {}
    for node in ast.walk(governed):
        if isinstance(node, ast.Call) and (name := _call_name(node)):
            call_lines.setdefault(name, []).append(node.lineno)
    assert min(call_lines["verify_execution_authorization"]) < min(call_lines["require_allowed_profile"])
    assert min(call_lines["require_allowed_profile"]) < min(call_lines["require_cua_execution_evidence"])
    assert min(call_lines["require_cua_execution_evidence"]) < min(call_lines["authorize_execution_attempt"])
    assert min(call_lines["authorize_execution_attempt"]) < min(call_lines["mark_execution_attempt_executing"])
    assert min(call_lines["mark_execution_attempt_executing"]) < min(call_lines["governed_execution_profile"])
    assert min(call_lines["governed_execution_profile"]) < min(call_lines["_handle_action_ungoverned"])

    authorize_call = next(
        node
        for node in ast.walk(governed)
        if isinstance(node, ast.Call) and _call_name(node) == "authorize_execution_attempt"
    )
    assert {keyword.arg for keyword in authorize_call.keywords} >= {
        "effect",
        "execution_profile",
        "cua_execution_evidence",
    }

    for mechanism in ("LOCATOR", "LABEL", "COORDINATE", "JAVASCRIPT"):
        assert f"require_execution_mechanism(ExecutionMechanism.{mechanism})" in source


def test_cua_requires_fresh_evidence_profile_and_authorization():
    handler_source = (ROOT / "skyvern/webeye/actions/handler.py").read_text(encoding="utf-8")
    permit_source = (ROOT / "enterprise/governance/permit_service.py").read_text(encoding="utf-8")
    attempt_source = (ROOT / "enterprise/governance/execution_attempt_service.py").read_text(encoding="utf-8")
    agent_source = (ROOT / "skyvern/forge/agent.py").read_text(encoding="utf-8")

    assert "RunEngine.openai_cua" in agent_source
    assert "RunEngine.anthropic_cua" in agent_source
    assert "RunEngine.ui_tars" in agent_source
    assert "ActionHandler.handle_action(" in agent_source
    assert "require_cua_execution_evidence(" in handler_source
    assert '"cua_execution_evidence"' in permit_source
    assert "persisted_cua_evidence != cua_execution_evidence" in attempt_source


def test_cached_actions_require_fresh_observation_and_authorization():
    agent_tree = ast.parse((ROOT / "skyvern/forge/agent.py").read_text(encoding="utf-8"))
    forge_agent = next(node for node in agent_tree.body if isinstance(node, ast.ClassDef) and node.name == "ForgeAgent")
    step = _find_function(forge_agent, "agent_step")
    speculate = _find_function(forge_agent, "_speculate_next_step_plan")
    cache_tree = ast.parse((ROOT / "skyvern/webeye/actions/caching.py").read_text(encoding="utf-8"))
    retrieve = _find_function(cache_tree, "retrieve_action_plan")

    step_calls = [
        node for node in ast.walk(step) if isinstance(node, ast.Call) and _call_name(node)
    ]
    guard_lines = [node.lineno for node in step_calls if _call_name(node) == "precomputed_action_reuse_is_allowed"]
    build_lines = [node.lineno for node in step_calls if _call_name(node) == "build_and_record_step_prompt"]
    assert guard_lines and build_lines and min(guard_lines) < min(build_lines)

    speculate_calls = {
        _call_name(node): node.lineno
        for node in ast.walk(speculate)
        if isinstance(node, ast.Call) and _call_name(node)
    }
    assert speculate_calls["precomputed_action_reuse_is_allowed"] < speculate_calls["build_and_record_step_prompt"]

    retrieve_calls = {
        _call_name(node): node.lineno
        for node in ast.walk(retrieve)
        if isinstance(node, ast.Call) and _call_name(node)
    }
    assert retrieve_calls["precomputed_action_reuse_is_allowed"] < retrieve_calls["_retrieve_action_plan"]


def test_direct_clients_reach_handler_or_shared_script_rejection():
    raw_client = (ROOT / "skyvern/client/scripts/raw_client.py").read_text(encoding="utf-8")
    client = (ROOT / "skyvern/client/scripts/client.py").read_text(encoding="utf-8")
    script_route = (ROOT / "skyvern/forge/sdk/routes/scripts.py").read_text(encoding="utf-8")
    background_executor = (
        ROOT / "skyvern/forge/sdk/executor/background_task_executor.py"
    ).read_text(encoding="utf-8")
    script_service = (ROOT / "skyvern/services/script_service.py").read_text(encoding="utf-8")
    sdk_route = (ROOT / "skyvern/forge/sdk/routes/sdk.py").read_text(encoding="utf-8")
    script_page = (ROOT / "skyvern/core/script_generations/script_skyvern_page.py").read_text(encoding="utf-8")

    assert raw_client.count('f"v1/scripts/{jsonable_encoder(script_id)}/run"') == 2
    assert raw_client.count('method="POST"') >= 2
    assert client.count("self._raw_client.run_script") == 2

    route_rejection = script_route.index('raise HTTPException(status_code=400, detail="Not implemented")')
    route_executor = script_route.index("await AsyncExecutorFactory.get_executor().execute_script")
    assert route_rejection < route_executor
    assert "script_service.execute_script" in background_executor

    service_tree = ast.parse(script_service)
    for function_name in ("execute_script", "run_script"):
        assert _called_name(_find_function(service_tree, function_name).body[0]) == (
            "assert_script_execution_is_not_governed"
        )

    assert sdk_route.index("ScriptSkyvernPage.create_scraped_page") < sdk_route.index("RealSkyvernPageAi(")
    script_page_tree = ast.parse(script_page)
    script_page_class = next(
        node
        for node in script_page_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ScriptSkyvernPage"
    )
    assert _called_name(_find_function(script_page_class, "create_scraped_page").body[0]) == (
        "assert_script_execution_is_not_governed"
    )


def test_all_governed_script_launchers_share_rejection():
    tree = ast.parse((ROOT / "skyvern/services/script_service.py").read_text(encoding="utf-8"))

    for function_name in ("execute_script", "run_script"):
        function = _find_function(tree, function_name)
        assert _called_name(function.body[0]) == "assert_script_execution_is_not_governed"

    caller_markers = {
        "skyvern/forge/sdk/routes/scripts.py": "AsyncExecutorFactory.get_executor().execute_script",
        "skyvern/forge/sdk/executor/background_task_executor.py": "script_service.execute_script",
        "skyvern/cli/run_commands.py": "asyncio.run(run_script",
        "skyvern/core/script_generations/run_initializer.py": "ScriptSkyvernPage.create",
    }
    for path, marker in caller_markers.items():
        assert marker in (ROOT / path).read_text(encoding="utf-8")


def test_governed_script_page_creation_is_rejected():
    tree = ast.parse((ROOT / "skyvern/core/script_generations/script_skyvern_page.py").read_text(encoding="utf-8"))
    script_page = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ScriptSkyvernPage"
    )

    for function_name in ("__init__", "create", "create_scraped_page"):
        function = _find_function(script_page, function_name)
        assert _called_name(function.body[0]) == "assert_script_execution_is_not_governed"


def test_script_page_proxies_are_documented_as_sealed_by_shared_rejection():
    """Keep the inventory aligned with the shared script-entry guard."""

    inventory = (ROOT / "docs/phase-2/execution-entrypoints.md").read_text(encoding="utf-8")
    skyvern_page = (ROOT / "skyvern/core/script_generations/skyvern_page.py").read_text(encoding="utf-8")
    script_page = (ROOT / "skyvern/core/script_generations/script_skyvern_page.py").read_text(encoding="utf-8")

    assert "await locator.click" in skyvern_page
    assert "def __getattribute__" in skyvern_page
    assert "await handle_complete_action" in script_page
    assert "**Sealed for governed scripts.**" in inventory
    assert "Task 5 decision: reject governed scripts at shared entry points" in inventory
    assert "do not add one guard per Page proxy" in inventory


def _find_function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    return next(
        node
        for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def _called_name(statement: ast.stmt) -> str | None:
    if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
        return None
    function = statement.value.func
    return function.id if isinstance(function, ast.Name) else None


def _call_name(call: ast.Call) -> str | None:
    function = call.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return None
