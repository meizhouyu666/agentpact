from __future__ import annotations

import ast
from pathlib import Path

import pytest

from enterprise.agent_runs.journal import is_plan_application_marker
from enterprise.governance.capabilities import CapabilityDefinition
from enterprise.governance.domain_packs import DomainPackKind, DomainPackManifest
from enterprise.governance.pack_runtime import (
    ApprovalRequestSpecification,
    ExecutionCheckpoint,
    PackAdmissionResult,
    PackAdvanceResult,
    PackAdvanceStatus,
    PackLifecycleError,
    PackProbeResult,
    PackProbeStatus,
    PackRuntimeAdapter,
    PackRuntimeBinding,
    PackRuntimeRegistry,
    PreparedRunReference,
    validate_pack_admission_result,
    validate_pack_advance_result,
    validate_pack_probe_result,
)
from tests.fixtures.fake_domain_pack import (
    FAKE_ADAPTER_ID,
    FAKE_CAPABILITY_IDS,
    FAKE_PACK_DISPLAY_NAME,
    FAKE_PACK_ID,
    FAKE_PACK_VERSION,
    FAKE_RUNTIME_CONTRACT,
    FakeDomainPackAdapter,
)


def test_registry_validates_adapter_identity_and_exact_capabilities() -> None:
    contract = FAKE_RUNTIME_CONTRACT
    registry = PackRuntimeRegistry([contract])
    with pytest.raises(ValueError, match="identity"):
        registry.register(
            FakeDomainPackAdapter(
                PackRuntimeBinding(
                    pack_id=contract.pack_id,
                    pack_version=contract.pack_version,
                    capability_ids=contract.capability_ids,
                    adapter_id="substituted.runtime",
                )
            )
        )
    with pytest.raises(ValueError, match="capabilities"):
        registry.register(
            FakeDomainPackAdapter(
                PackRuntimeBinding(
                    pack_id=contract.pack_id,
                    pack_version=contract.pack_version,
                    capability_ids=("fake.domain.unexpected",),
                    adapter_id=contract.adapter_id,
                )
            )
        )

    adapter = FakeDomainPackAdapter()
    assert isinstance(adapter, PackRuntimeAdapter)
    registry.register(adapter)
    assert registry.require(pack_id=FAKE_PACK_ID, pack_version=FAKE_PACK_VERSION) is adapter
    assert registry.public_metadata(pack_id=FAKE_PACK_ID, pack_version=FAKE_PACK_VERSION).model_dump() == {
        "pack_id": FAKE_PACK_ID,
        "pack_version": FAKE_PACK_VERSION,
        "display_name": FAKE_PACK_DISPLAY_NAME,
    }
    assert adapter.binding.capability_ids == FAKE_CAPABILITY_IDS
    assert adapter.binding.adapter_id == FAKE_ADAPTER_ID


def test_registry_rejects_duplicate_pack_contracts() -> None:
    with pytest.raises(ValueError, match="already registered"):
        PackRuntimeRegistry([FAKE_RUNTIME_CONTRACT, FAKE_RUNTIME_CONTRACT])


def test_lifecycle_status_enums_are_closed_generic_sets() -> None:
    assert {item.value for item in PackAdvanceStatus} == {
        "COMPLETED",
        "AWAITING_APPROVAL",
        "PENDING_RESULT_PROBE",
        "FAILED",
    }
    assert {item.value for item in PackProbeStatus} == {
        "CONFIRMED",
        "NOT_CONFIRMED",
        "INCONCLUSIVE",
    }


def test_advance_result_rejects_fields_illegal_for_status() -> None:
    with pytest.raises(ValueError, match="AWAITING_APPROVAL"):
        PackAdvanceResult(status=PackAdvanceStatus.AWAITING_APPROVAL, run_id="run-1")
    with pytest.raises(ValueError, match="PENDING_RESULT_PROBE"):
        PackAdvanceResult(
            status=PackAdvanceStatus.COMPLETED,
            run_id="run-1",
            execution_checkpoint={
                "permit_id": "permit-1",
                "attempt_id": "attempt-1",
                "task_id": "task-1",
                "step_id": "step-1",
                "action_fingerprint": "fingerprint",
                "observation_hash": "observation",
                "idempotency_key_digest": "b" * 64,
                "execution_effect": "external_write",
                "result_probe_ref": "probe://orders/1",
                "attempt_status": "unknown",
            },
        )


def _checkpoint(*, attempt_status: str = "unknown") -> ExecutionCheckpoint:
    return ExecutionCheckpoint(
        permit_id="permit-1",
        attempt_id="attempt-1",
        task_id="task-1",
        step_id="step-1",
        action_fingerprint="a" * 64,
        observation_hash="b" * 64,
        idempotency_key_digest="c" * 64,
        execution_effect="external_write",
        result_probe_ref="probe://orders/1",
        attempt_status=attempt_status,
    )


def test_closed_lifecycle_statuses_reject_unknown_values_and_non_unknown_probe_checkpoints() -> None:
    with pytest.raises(ValueError):
        PackAdvanceResult.model_validate({"status": "RUNNING", "run_id": "run-1"})
    with pytest.raises(ValueError, match="UNKNOWN"):
        PackAdvanceResult(
            status=PackAdvanceStatus.PENDING_RESULT_PROBE,
            run_id="run-1",
            step_id="step-1",
            reason_code="RESULT_UNCERTAIN",
            execution_checkpoint=_checkpoint(attempt_status="confirmed"),
        )
    with pytest.raises(ValueError, match="UNKNOWN"):
        PackProbeResult(
            status=PackProbeStatus.CONFIRMED,
            checkpoint=_checkpoint(attempt_status="confirmed"),
            reason_code="RESULT_CONFIRMED",
        )


def test_platform_result_validation_maps_shape_and_identity_errors_to_generic_codes() -> None:
    with pytest.raises(PackLifecycleError, match="PACK_ADVANCE_RESULT_INVALID"):
        validate_pack_advance_result({"status": "RUNNING", "run_id": "run-1"}, run_id="run-1")
    with pytest.raises(PackLifecycleError, match="PACK_PROBE_RESULT_CORRELATION_MISMATCH"):
        validate_pack_probe_result(
            PackProbeResult(
                status=PackProbeStatus.CONFIRMED,
                checkpoint=_checkpoint(),
                reason_code="RESULT_CONFIRMED",
            ),
            run_id="run-1",
            native_task_id="other-task",
            native_step_id="step-1",
            permit_id="permit-1",
            attempt_id="attempt-1",
        )
    validated = validate_pack_probe_result(
        PackProbeResult(
            status=PackProbeStatus.CONFIRMED,
            checkpoint=_checkpoint(),
            reason_code="RESULT_CONFIRMED",
        ),
        run_id="run-1",
        native_task_id="task-1",
        native_step_id="step-1",
        permit_id="permit-1",
        attempt_id="attempt-1",
    )
    assert validated.checkpoint.task_id == "task-1"


def test_admission_result_validation_maps_shape_and_identity_errors_to_generic_codes() -> None:
    prepared = PreparedRunReference(
        run_id="run-1",
        tenant_id="tenant-1",
        request_id="request-1",
        pack_id="fixture.orders",
        pack_version="1.0.0",
        adapter_id="fixture.orders.runtime",
        admission_id="admission-1",
        contract_id="contract-1",
        provider_mode="recorded",
        opaque_payload={},
    )
    with pytest.raises(PackLifecycleError, match="PACK_ADMISSION_RESULT_INVALID"):
        validate_pack_admission_result(
            {"prepared": prepared.model_dump(), "admission_id": "admission-1"},
            prepared=prepared,
        )
    mismatched = PackAdmissionResult(
        prepared=prepared.model_copy(update={"run_id": "run-2"}),
        admission_id="admission-1",
        initial=PackAdvanceResult(status=PackAdvanceStatus.COMPLETED, run_id="run-2"),
    )
    with pytest.raises(PackLifecycleError, match="PACK_ADMISSION_RESULT_CORRELATION_MISMATCH"):
        validate_pack_admission_result(mismatched, prepared=prepared)


def test_approval_spec_contains_no_executable_action_shape() -> None:
    assert "action" not in ApprovalRequestSpecification.model_fields
    assert "selector" not in ApprovalRequestSpecification.model_fields
    assert "challenge_id" not in ApprovalRequestSpecification.model_fields


def _imported_modules(source: str, *, module_name: str, is_package: bool = False) -> tuple[str, ...]:
    tree = ast.parse(source, filename=module_name)
    package = module_name.split(".") if is_package else module_name.split(".")[:-1]
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = max(0, len(package) - (node.level - 1))
                prefix = package[:keep]
                if node.module:
                    prefix.extend(node.module.split("."))
                resolved = ".".join(prefix)
            else:
                resolved = node.module or ""
            if resolved:
                imported.add(resolved)
            imported.update(f"{resolved}.{alias.name}" if resolved else alias.name for alias in node.names)
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            function = node.func
            is_dynamic_import = (
                isinstance(function, ast.Name)
                and function.id in {"__import__", "import_module"}
                or isinstance(function, ast.Attribute)
                and function.attr == "import_module"
            )
            if is_dynamic_import and isinstance(node.args[0].value, str):
                imported.add(node.args[0].value)
    return tuple(sorted(imported))


def _imports(root: Path, path: Path) -> tuple[str, ...]:
    relative = path.relative_to(root).with_suffix("")
    parts = relative.parts
    is_package = parts[-1] == "__init__"
    module_name = ".".join(parts[:-1] if is_package else parts)
    return _imported_modules(
        path.read_text(encoding="utf-8"),
        module_name=module_name,
        is_package=is_package,
    )


def _is_concrete_pack_import(module_name: str) -> bool:
    return module_name == "enterprise.domains" or module_name.startswith("enterprise.domains.")


def test_platform_runtime_packages_do_not_import_concrete_packs() -> None:
    root = Path(__file__).resolve().parents[2]
    violations: list[tuple[Path, str]] = []
    for package in (
        root / "enterprise" / "agent_runs",
        root / "enterprise" / "browser_loop",
        root / "enterprise" / "evaluation",
        root / "enterprise" / "governance",
    ):
        for path in package.rglob("*.py"):
            violations.extend(
                (path.relative_to(root), name) for name in _imports(root, path) if _is_concrete_pack_import(name)
            )
    assert not violations


def test_formal_runtime_boundary_excludes_pack_and_compatibility_shims() -> None:
    root = Path(__file__).resolve().parents[2]
    paths = [
        *sorted((root / "enterprise" / "agent_runs").rglob("*.py")),
        *sorted((root / "enterprise" / "browser_loop").rglob("*.py")),
        root / "enterprise" / "governance" / "pack_runtime.py",
    ]
    source = "\n".join(path.read_text(encoding="utf-8").lower() for path in paths)
    for forbidden in (
        "enterprise.domains.",
        "synthetic_payment",
        "stripe_payment",
        "trusted_inputs",
        "agentpact-m8",
        "append_m10_transition",
    ):
        assert forbidden not in source

    pack_runtime_tree = ast.parse(
        (root / "enterprise" / "governance" / "pack_runtime.py").read_text(encoding="utf-8")
    )
    adapter = next(node for node in pack_runtime_tree.body if isinstance(node, ast.ClassDef) and node.name == "PackRuntimeAdapter")
    adapter_source = ast.unparse(adapter)
    assert "object" not in adapter_source
    assert "trusted_inputs" not in adapter_source


@pytest.mark.parametrize(
    ("module_name", "source"),
    [
        ("enterprise.agent_runs.service", "from enterprise.domains.future_pack import runtime\n"),
        ("enterprise.browser_loop.loop", "from ..domains.future_pack import runtime\n"),
        (
            "enterprise.evaluation.benchmark",
            'import importlib\nimportlib.import_module("enterprise.domains.future_pack.runtime")\n',
        ),
    ],
)
def test_platform_pack_guard_rejects_absolute_relative_and_dynamic_imports(
    module_name: str,
    source: str,
) -> None:
    imports = _imported_modules(source, module_name=module_name)
    assert any(_is_concrete_pack_import(name) for name in imports)


def test_synthetic_kind_accepts_owner_defined_pack_namespace_but_stays_nonproduction() -> None:
    capability = CapabilityDefinition(
        capability_id="orders.read",
        version="1.0.0",
        domain="orders",
        display_name="Read orders",
        access_policy_ref="policy://orders/read",
        risk_policy_ref="policy://orders/read",
        work_order_template_ref="template://orders/read",
        result_probe_ref="probe://orders/read",
    )
    manifest = DomainPackManifest(
        pack_id="fixture.orders",
        version="1.0.0",
        kind=DomainPackKind.SYNTHETIC,
        display_name="Order Fixture",
        owner="fixture-owner",
        capabilities=[capability],
    )

    assert manifest.pack_id == "fixture.orders"
    with pytest.raises(ValueError, match="never be production eligible"):
        DomainPackManifest(
            **manifest.model_dump(exclude={"production_eligible"}),
            production_eligible=True,
        )


def test_synthetic_agent_run_composition_is_test_fixture_only() -> None:
    root = Path(__file__).resolve().parents[2]
    production_path = root / "enterprise" / "applications" / "synthetic_payment_agent_runs.py"
    composition = root / "tests" / "fixtures" / "synthetic_payment_agent_runs.py"

    assert not production_path.exists()
    imports = _imports(root, composition)
    assert any(name.startswith("enterprise.agent_runs") for name in imports)
    assert any(name.startswith("tests.fixtures.synthetic_payment_runtime") for name in imports)


def test_formal_api_startup_does_not_import_or_mount_synthetic_application() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "skyvern" / "forge" / "api_app.py").read_text(encoding="utf-8")

    assert "synthetic_payment" not in source
    assert "mount_synthetic_agent_run_api" not in source


def test_generic_contract_sources_contain_no_payment_schema_or_pack_ids() -> None:
    root = Path(__file__).resolve().parents[2]
    source = "\n".join(
        (root / relative).read_text(encoding="utf-8")
        for relative in (
            "enterprise/governance/pack_runtime.py",
            "enterprise/agent_runs/journal.py",
            "enterprise/agent_runs/service.py",
        )
    ).lower()
    for forbidden in (
        "beneficiary_id",
        "challenge_id",
        "synthetic.payment",
        "stripe.payment",
        "run_m10_",
        "m10-intent",
        'literal["precheck", "submit", "confirm"]',
        "agentpact-m8",
        "m8.plan.",
        "m10:",
    ):
        assert forbidden not in source


def test_generic_run_identity_is_pack_neutral() -> None:
    from enterprise.governance.pack_runtime import derive_pack_run_id

    run_id = derive_pack_run_id(tenant_id="tenant-a", request_id="request-a")
    assert run_id.startswith("run_")
    assert not run_id.startswith("run_m10_")


def test_agent_run_journal_accepts_legacy_root_marker_during_migration() -> None:
    assert is_plan_application_marker("agentpact:agent-run:plan:v1")
    assert is_plan_application_marker("agentpact:m8:plan:v1")
    assert not is_plan_application_marker("untrusted.application")
