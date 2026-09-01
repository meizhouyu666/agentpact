from __future__ import annotations

import ast
from pathlib import Path

import pytest

from enterprise.governance.capabilities import CapabilityDefinition
from enterprise.governance.domain_packs import DomainPackKind, DomainPackManifest
from enterprise.governance.pack_runtime import (
    ApprovalRequestSpecification,
    PackAdvanceResult,
    PackAdvanceStatus,
    PackRuntimeBinding,
    PackRuntimeContract,
    PackRuntimeRegistry,
)


class _Adapter:
    def __init__(self, binding: PackRuntimeBinding) -> None:
        self._binding = binding

    @property
    def binding(self) -> PackRuntimeBinding:
        return self._binding


def _contract() -> PackRuntimeContract:
    return PackRuntimeContract(
        pack_id="fixture.orders",
        pack_version="2.0.0",
        display_name="Order Fixture",
        capability_ids=("orders.read", "orders.submit"),
        adapter_id="fixture.orders.runtime.v2",
        manifest_digest="a" * 64,
    )


def test_registry_validates_adapter_identity_and_exact_capabilities() -> None:
    contract = _contract()
    registry = PackRuntimeRegistry([contract])
    with pytest.raises(ValueError, match="identity"):
        registry.register(
            _Adapter(
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
            _Adapter(
                PackRuntimeBinding(
                    pack_id=contract.pack_id,
                    pack_version=contract.pack_version,
                    capability_ids=("orders.read",),
                    adapter_id=contract.adapter_id,
                )
            )
        )


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


def test_approval_spec_contains_no_executable_action_shape() -> None:
    assert "action" not in ApprovalRequestSpecification.model_fields
    assert "selector" not in ApprovalRequestSpecification.model_fields
    assert "challenge_id" not in ApprovalRequestSpecification.model_fields


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return tuple(
        [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        + [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ]
    )


def test_platform_runtime_packages_do_not_import_concrete_packs() -> None:
    root = Path(__file__).resolve().parents[2]
    for package in (
        root / "enterprise" / "agent_runs",
        root / "enterprise" / "browser_loop",
        root / "enterprise" / "evaluation",
        root / "enterprise" / "governance",
    ):
        for path in package.rglob("*.py"):
            imports = _imports(path)
            assert not any(
                name.startswith("enterprise.domains.synthetic_payment")
                or name.startswith("enterprise.domains.stripe_payment")
                for name in imports
            ), path


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
    imports = _imports(composition)
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
    ):
        assert forbidden not in source


def test_generic_run_identity_is_pack_neutral() -> None:
    from enterprise.governance.pack_runtime import derive_pack_run_id

    run_id = derive_pack_run_id(tenant_id="tenant-a", request_id="request-a")
    assert run_id.startswith("run_")
    assert not run_id.startswith("run_m10_")
