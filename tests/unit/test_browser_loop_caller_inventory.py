"""Static inventory for AgentPact browser-loop migration callers."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MATRIX = json.loads(
    (ROOT / "tests" / "fixtures" / "browser_loop_caller_inventory.json").read_text(encoding="utf-8")
)
DOC = (ROOT / "docs" / "architecture" / "agentpact-browser-loop.md").read_text(encoding="utf-8")


def _legacy_handler_references(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    references: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "skyvern.webeye.actions.handler":
            references.append(f"import:{node.lineno}")
        elif isinstance(node, ast.Name) and node.id == "ActionHandler":
            references.append(f"name:{node.lineno}")
    return references


def test_inventory_declares_the_migrated_submit_and_retained_legacy_boundary() -> None:
    by_id = {entry["id"]: entry for entry in MATRIX}
    assert set(by_id) == {"synthetic_m10_submit", "legacy_synthetic_e2e_support"}
    assert by_id["synthetic_m10_submit"]["current_status"] == "migrated"
    assert by_id["synthetic_m10_submit"]["required_disposition"] == "route_through_agentpact_browser_loop"
    assert by_id["legacy_synthetic_e2e_support"]["current_status"] == "retained"
    assert by_id["legacy_synthetic_e2e_support"]["required_disposition"] == "retain_until_explicit_migration"

    migrated_source = (ROOT / by_id["synthetic_m10_submit"]["source"]).read_text(encoding="utf-8")
    for marker in by_id["synthetic_m10_submit"]["markers"]:
        assert marker in migrated_source
    for forbidden in by_id["synthetic_m10_submit"]["forbidden_markers"]:
        assert forbidden not in migrated_source

    for entry in MATRIX:
        assert f"`{entry['id']}`" in DOC
        closure_test = entry.get("closure_test")
        if closure_test:
            assert closure_test in (ROOT / "tests" / "unit" / "test_synthetic_m10_agentpact_submit.py").read_text(
                encoding="utf-8"
            )


def test_agentpact_owned_sources_have_no_legacy_action_handler_side_effect_callers() -> None:
    offenders: dict[str, list[str]] = {}
    for relative_root in ("enterprise", "tests/fixtures"):
        for path in (ROOT / relative_root).rglob("*.py"):
            references = _legacy_handler_references(path)
            if references:
                offenders[path.relative_to(ROOT).as_posix()] = references
    assert offenders == {}


def test_retained_legacy_callers_are_explicitly_present_and_documented() -> None:
    retained = next(entry for entry in MATRIX if entry["id"] == "legacy_synthetic_e2e_support")
    for source in retained["sources"]:
        path = ROOT / source["path"]
        assert source["marker"] in path.read_text(encoding="utf-8")
    assert "Skyvern product/test evidence" in DOC
    assert "SkyvernScraperRuntimeAdapter" in DOC


@pytest.mark.parametrize(
    "marker",
    [
        "async def _fresh_submit_context(",
        "AgentPactBrowserLoop(",
        "PersistedBrowserExecutor(",
        "suspend_unknown_execution_for_probe(",
        "async def probe("
    ],
)
def test_migrated_submit_contains_each_owned_boundary_marker(marker: str) -> None:
    source = (
        ROOT / "tests" / "fixtures" / "synthetic_payment_runtime" / "m10_runtime.py"
    ).read_text(encoding="utf-8")
    assert marker in source
