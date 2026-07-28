"""Static source-to-document matrix for unsealed execution entry points."""

import ast
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
MATRIX = json.loads((ROOT / "tests" / "fixtures" / "execution_entrypoint_inventory.json").read_text(encoding="utf-8"))
INVENTORY = (ROOT / "docs" / "phase-2" / "execution-entrypoints.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("entry", MATRIX, ids=[entry["id"] for entry in MATRIX])
def test_inventory_entry_is_backed_by_source_markers_and_documented(entry):
    assert entry["inventory_marker"] in INVENTORY
    for source in entry["sources"]:
        text = (ROOT / source["path"]).read_text(encoding="utf-8")
        for marker in source["markers"]:
            assert marker in text, f"{entry['id']} lost source marker {marker!r} in {source['path']}"


def test_matrix_covers_every_independently_tracked_execution_family():
    assert {entry["id"] for entry in MATRIX} == {
        "handler_locator_coordinate_javascript",
        "skyvern_page_script_proxy",
        "shared_script_launchers",
        "cua_ui_tars",
        "cached_speculative_actions",
        "sdk_direct_script_clients",
    }
    assert "Every row is sealed and regression-tested" in INVENTORY
    assert "`GOVERNANCE_MODE=enforce` remains rejected at configuration load" in INVENTORY


def test_every_entry_has_an_explicit_pre_enforce_closure_contract():
    required_fields = {
        "current_status",
        "required_disposition",
        "guard_owner",
        "required_controls",
        "closure_test",
        "enforce_eligible",
    }
    allowed_dispositions = {
        "route_through_public_handler",
        "reject_governed_script",
        "fresh_reobserve",
        "inventory_only",
    }
    declared_tests = {
        node.name
        for path in (ROOT / "tests" / "unit").glob("test_*governance_entrypoint*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    for entry in MATRIX:
        assert required_fields <= entry.keys()
        assert entry["current_status"] in {"sealed", "unsealed"}
        assert entry["required_disposition"] in allowed_dispositions
        assert entry["guard_owner"]
        assert entry["required_controls"]
        assert entry["closure_test"].startswith("test_")
        assert entry["enforce_eligible"] is (entry["current_status"] == "sealed")
        if entry["current_status"] == "sealed":
            assert entry["closure_test"] in declared_tests
        for value in (
            entry["id"],
            entry["required_disposition"],
            entry["guard_owner"],
            entry["closure_test"],
        ):
            assert f"`{value}`" in INVENTORY


def test_script_proxy_and_launchers_share_one_rejection_owner():
    by_id = {entry["id"]: entry for entry in MATRIX}
    script_entries = [
        by_id["skyvern_page_script_proxy"],
        by_id["shared_script_launchers"],
    ]

    assert {entry["required_disposition"] for entry in script_entries} == {"reject_governed_script"}
    assert {entry["guard_owner"] for entry in script_entries} == {"shared_governed_script_rejection"}
    assert {entry["current_status"] for entry in script_entries} == {"sealed"}
    assert all(entry["enforce_eligible"] is True for entry in script_entries)


def test_cached_cua_and_direct_client_rows_remain_fail_closed():
    by_id = {entry["id"]: entry for entry in MATRIX}

    cached = by_id["cached_speculative_actions"]
    assert cached["required_disposition"] == "fresh_reobserve"
    assert {"fresh_observation", "fresh_policy_decision", "execution_authorization"} <= set(cached["required_controls"])

    cua = by_id["cua_ui_tars"]
    assert {"fresh_observation", "execution_profile", "execution_authorization", "engine_evidence"} <= set(
        cua["required_controls"]
    )

    direct = by_id["sdk_direct_script_clients"]
    assert direct["required_disposition"] == "inventory_only"
    assert {cached["current_status"], cua["current_status"], direct["current_status"]} == {"sealed"}
    assert all(entry["enforce_eligible"] is True for entry in (cached, cua, direct))


def test_script_and_direct_client_inventory_covers_known_external_callers():
    by_id = {entry["id"]: entry for entry in MATRIX}
    source_paths = {
        entry_id: {source["path"] for source in by_id[entry_id]["sources"]}
        for entry_id in ("shared_script_launchers", "sdk_direct_script_clients")
    }

    assert {
        "skyvern/services/script_service.py",
        "skyvern/core/script_generations/run_initializer.py",
        "skyvern/forge/sdk/routes/scripts.py",
        "skyvern/forge/sdk/executor/background_task_executor.py",
        "skyvern/cli/run_commands.py",
    } <= source_paths["shared_script_launchers"]
    assert {
        "skyvern/client/scripts/raw_client.py",
        "skyvern/client/scripts/client.py",
        "skyvern/forge/sdk/routes/sdk.py",
        "skyvern/forge/sdk/routes/scripts.py",
        "skyvern/forge/sdk/executor/background_task_executor.py",
        "skyvern/services/script_service.py",
        "skyvern/core/script_generations/script_skyvern_page.py",
    } <= source_paths["sdk_direct_script_clients"]
