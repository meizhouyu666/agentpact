from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from enterprise.governance.capabilities import CapabilityDefinition
from enterprise.governance.domain_pack_installations import (
    DomainPackInstallation,
    DomainPackInstallationStatus,
    build_active_domain_pack_set,
)
from enterprise.governance.domain_packs import DomainPackKind, DomainPackManifest, DomainPackRegistry
from enterprise.governance.pack_conformance import ConformanceStatus, StaticConformanceReport
from enterprise.governance.pack_runtime import PackRuntimeBinding, PackRuntimeContract, PackRuntimeRegistry

NOW = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
PACK_ID = "fixture.orders"
PACK_VERSION = "1.2.3"
CAPABILITY_ID = "fixture.orders.submit"
ADAPTER_ID = "fixture.orders.runtime.v1"
ADAPTER_REF = "fixture.orders.browser.v1"
DIGEST = "a" * 64


def _manifest(*, version: str = PACK_VERSION) -> DomainPackManifest:
    return DomainPackManifest(
        pack_id=PACK_ID,
        version=version,
        kind=DomainPackKind.PRODUCTION,
        display_name="Fixture Orders",
        owner="fixture-owner",
        capabilities=[
            CapabilityDefinition(
                capability_id=CAPABILITY_ID,
                version=version,
                domain=PACK_ID,
                display_name="Submit order",
                access_policy_ref="policy://fixture/orders",
                risk_policy_ref="risk://fixture/orders",
                work_order_template_ref="work-order://fixture/orders",
                result_probe_ref="probe://fixture/orders",
            )
        ],
        result_probe_refs={"probe://fixture/orders"},
    )


def _installation(*, tenant_id: str = "tenant-a", **updates: object) -> DomainPackInstallation:
    values = {
        "installation_id": f"installation-{tenant_id}",
        "tenant_id": tenant_id,
        "pack_id": PACK_ID,
        "pack_version": PACK_VERSION,
        "contract_digest": DIGEST,
        "enabled_capability_ids": (CAPABILITY_ID,),
        "adapter_ref": ADAPTER_REF,
        "result_probe_ref": "probe://fixture/orders",
        "policy_version": "fixture-policy.v1",
        "status": DomainPackInstallationStatus.ACCEPTED,
        "accepted_at": NOW - timedelta(minutes=1),
        "expires_at": NOW + timedelta(minutes=30),
    }
    values.update(updates)
    return DomainPackInstallation(**values)


def _report(*, pack_version: str = PACK_VERSION) -> StaticConformanceReport:
    return StaticConformanceReport(
        candidate_pack_id=PACK_ID,
        candidate_pack_version=pack_version,
        manifest_digest=DIGEST,
        status=ConformanceStatus.PASS,
        checks=(),
        violations=(),
    )


def _contract() -> PackRuntimeContract:
    return PackRuntimeContract(
        pack_id=PACK_ID,
        pack_version=PACK_VERSION,
        display_name="Fixture Orders",
        capability_ids=(CAPABILITY_ID,),
        adapter_id=ADAPTER_ID,
        manifest_digest=DIGEST,
    )


def _adapter() -> SimpleNamespace:
    return SimpleNamespace(
        binding=PackRuntimeBinding(
            pack_id=PACK_ID,
            pack_version=PACK_VERSION,
            capability_ids=(CAPABILITY_ID,),
            adapter_id=ADAPTER_ID,
        )
    )


def test_active_registry_is_version_keyed_and_offline_objects_are_rejected() -> None:
    older = _manifest(version="1.2.2")
    current = _manifest()
    registry = DomainPackRegistry([older, current])

    assert registry.require_exact(pack_id=PACK_ID, pack_version=PACK_VERSION) is current
    with pytest.raises(ValueError, match="version is required"):
        registry.require(PACK_ID)
    with pytest.raises(TypeError, match="runtime manifests only"):
        registry.register(object())  # type: ignore[arg-type]


def test_active_installation_uses_exact_report_identity() -> None:
    with pytest.raises(ValueError, match="version"):
        build_active_domain_pack_set(
            tenant_id="tenant-a",
            runtime_manifests=[_manifest()],
            conformance_reports=[_report(pack_version="1.2.2")],
            installations=[_installation()],
            expected_policy_versions={PACK_ID: "fixture-policy.v1"},
            expected_adapter_refs={PACK_ID: ADAPTER_REF},
            now=NOW,
        )


def test_tenant_runtime_lookup_requires_the_exact_active_installation() -> None:
    installation = _installation()
    registry = PackRuntimeRegistry(
        [_contract()],
        installations=[installation],
        trusted_adapter_refs={(PACK_ID, PACK_VERSION): ADAPTER_REF},
        now=NOW,
    )

    with pytest.raises(ValueError, match="explicit installation"):
        registry.register(_adapter())
    registry.register_for_installation(_adapter(), installation)
    assert registry.require_for_tenant(
        tenant_id="tenant-a", pack_id=PACK_ID, pack_version=PACK_VERSION
    ).binding == _adapter().binding
    with pytest.raises(LookupError, match="No active Domain Pack installation"):
        registry.require_for_tenant(tenant_id="tenant-b", pack_id=PACK_ID, pack_version=PACK_VERSION)
    with pytest.raises(LookupError, match="stale"):
        registry.require_for_tenant(
            tenant_id="tenant-a", pack_id=PACK_ID, pack_version=PACK_VERSION, now=installation.expires_at
        )
    with pytest.raises(LookupError, match="capabilities"):
        registry.require_for_tenant(
            tenant_id="tenant-a",
            pack_id=PACK_ID,
            pack_version=PACK_VERSION,
            capability_ids=("fixture.orders.other",),
        )
    with pytest.raises(LookupError, match="adapter identity"):
        registry.require_for_tenant(
            tenant_id="tenant-a",
            pack_id=PACK_ID,
            pack_version=PACK_VERSION,
            adapter_id="fixture.orders.forged.v1",
        )
    with pytest.raises(LookupError, match="[Tt]enant-scoped runtime lookup"):
        registry.require(pack_id=PACK_ID, pack_version=PACK_VERSION)
    with pytest.raises(LookupError, match="No active Domain Pack installation"):
        PackRuntimeRegistry([_contract()]).resolve_for_execution(tenant_id="tenant-a", binding=_adapter().binding)
    assert registry.resolve_for_execution(tenant_id="tenant-a", binding=_adapter().binding) is not None


def test_tenant_runtime_lookup_uses_a_clock_when_now_is_not_supplied() -> None:
    current = [NOW]
    installation = _installation(expires_at=NOW + timedelta(minutes=1))
    registry = PackRuntimeRegistry(
        [_contract()],
        installations=[installation],
        trusted_adapter_refs={(PACK_ID, PACK_VERSION): ADAPTER_REF},
        clock=lambda: current[0],
    )
    registry.register_for_installation(_adapter(), installation)
    current[0] = installation.expires_at
    with pytest.raises(LookupError, match="stale"):
        registry.require_for_tenant(tenant_id="tenant-a", pack_id=PACK_ID, pack_version=PACK_VERSION)


def test_tenant_binding_cannot_be_resolved_without_an_explicit_installation() -> None:
    installation = _installation()
    registry = PackRuntimeRegistry(
        [_contract()],
        installations=[installation],
        trusted_adapter_refs={(PACK_ID, PACK_VERSION): ADAPTER_REF},
        now=NOW,
    )
    registry.register_for_installation(_adapter(), installation)
    with pytest.raises(LookupError, match="explicit tenant"):
        registry.require_binding(_adapter().binding)


def test_execution_resolution_requires_requested_capability_to_be_enabled() -> None:
    second_capability = "fixture.orders.read"
    binding = PackRuntimeBinding(
        pack_id=PACK_ID,
        pack_version=PACK_VERSION,
        capability_ids=(CAPABILITY_ID, second_capability),
        adapter_id=ADAPTER_ID,
    )
    contract = _contract().model_copy(update={"capability_ids": (CAPABILITY_ID, second_capability)})
    installation = _installation()
    registry = PackRuntimeRegistry(
        [contract],
        installations=[installation],
        trusted_adapter_refs={(PACK_ID, PACK_VERSION): ADAPTER_REF},
        now=NOW,
    )
    registry.register_for_installation(SimpleNamespace(binding=binding), installation)
    with pytest.raises(LookupError, match="enabled"):
        registry.resolve_for_execution(tenant_id="tenant-a", binding=binding, capability_ids=(second_capability,))
    assert registry.resolve_for_execution(tenant_id="tenant-a", binding=binding, capability_ids=(CAPABILITY_ID,))


def test_tenant_runtime_binding_rejects_stale_or_untrusted_installations() -> None:
    with pytest.raises(ValueError, match="trusted adapter references"):
        PackRuntimeRegistry([_contract()], installations=[_installation()])
    with pytest.raises(ValueError, match="stale"):
        PackRuntimeRegistry(
            [_contract()],
            installations=[
                _installation(
                    accepted_at=NOW - timedelta(hours=2),
                    expires_at=NOW - timedelta(minutes=1),
                )
            ],
            trusted_adapter_refs={(PACK_ID, PACK_VERSION): ADAPTER_REF},
            now=NOW,
        )
    with pytest.raises(ValueError, match="not trusted"):
        PackRuntimeRegistry(
            [_contract()],
            installations=[_installation(adapter_ref="fixture.orders.forged.v1")],
            trusted_adapter_refs={(PACK_ID, PACK_VERSION): ADAPTER_REF},
            now=NOW,
        )


def test_active_set_can_only_create_a_tenant_scoped_runtime_registry() -> None:
    active = build_active_domain_pack_set(
        tenant_id="tenant-a",
        runtime_manifests=[_manifest()],
        conformance_reports=[_report()],
        installations=[_installation()],
        expected_policy_versions={PACK_ID: "fixture-policy.v1"},
        expected_adapter_refs={PACK_ID: ADAPTER_REF},
        now=NOW,
    )

    runtime_registry = active.runtime_registry([_contract()], clock=lambda: NOW)
    runtime_registry.register_for_installation(_adapter(), active.require_installation(pack_id=PACK_ID, pack_version=PACK_VERSION))
    assert runtime_registry.public_metadata_for_tenant(
        tenant_id="tenant-a", pack_id=PACK_ID, pack_version=PACK_VERSION
    ).pack_id == PACK_ID
    with pytest.raises(LookupError, match="without an accepted"):
        PackRuntimeRegistry.from_active_domain_pack_set(
            active.__class__(tenant_id="tenant-a", installations=(), registry=DomainPackRegistry()),
            [_contract()],
        )
