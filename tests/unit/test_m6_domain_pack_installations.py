"""M6 process-local Domain Pack installation boundary tests."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from enterprise.domains.synthetic_payment.constants import CAPABILITY_ID, PACK_ID, POLICY_VERSION
from enterprise.domains.synthetic_payment.definition import build_manifest
from enterprise.domains.synthetic_payment.sdk_manifest import build_pack_sdk_manifest
from enterprise.governance.domain_pack_installations import (
    DomainPackInstallationStatus,
    build_active_domain_pack_set,
)
from enterprise.governance.pack_conformance import ConformanceStatus, evaluate_static_pack_conformance
from tests.fixtures.synthetic_payment_runtime.m6_runtime import SYNTHETIC_ADAPTER_REF, build_synthetic_installation

NOW = datetime(2026, 7, 29, 11, 30, tzinfo=timezone.utc)
TENANT = "synthetic-m6-tenant"


def _build(installations, *, report=None):
    return build_active_domain_pack_set(
        tenant_id=TENANT,
        runtime_manifests=[build_manifest()],
        conformance_reports=[report or evaluate_static_pack_conformance(build_pack_sdk_manifest())],
        installations=installations,
        expected_policy_versions={PACK_ID: POLICY_VERSION},
        expected_adapter_refs={PACK_ID: SYNTHETIC_ADAPTER_REF},
        now=NOW,
    )


def _installation(**updates):
    installation = build_synthetic_installation(
        tenant_id=TENANT,
        accepted_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=30),
        contract_digest=build_pack_sdk_manifest().manifest_digest,
    )
    return installation.model_copy(update=updates)


def test_offline_or_uninstalled_pack_never_enters_active_registry():
    active = _build([])

    assert active.installations == ()
    with pytest.raises(ValueError, match="not installed"):
        active.registry.require(PACK_ID)


@pytest.mark.parametrize(
    "status",
    [DomainPackInstallationStatus.REJECTED, DomainPackInstallationStatus.DISABLED],
)
def test_rejected_and_disabled_installations_are_not_active(status):
    active = _build([_installation(status=status)])

    assert active.installations == ()
    assert active.registry.capability_registry().definitions() == []


def test_accepted_installation_activates_only_its_exact_enabled_capability():
    installation = _installation()
    active = _build([installation])

    assert active.installations == (installation,)
    assert [item.capability_id for item in active.registry.capability_registry().definitions()] == [CAPABILITY_ID]


def test_accepted_installation_with_failed_conformance_report_fails_closed():
    report = evaluate_static_pack_conformance(build_pack_sdk_manifest()).model_copy(
        update={"status": ConformanceStatus.FAIL}
    )

    with pytest.raises(ValueError, match="failed static conformance"):
        _build([_installation()], report=report)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"expires_at": NOW}, "stale"),
        ({"pack_version": "0.9.0"}, "version"),
        ({"contract_digest": "0" * 64}, "digest"),
        ({"policy_version": "stale-policy"}, "policy version"),
        ({"adapter_ref": "synthetic.payment.untrusted-adapter"}, "adapter reference"),
        ({"enabled_capability_ids": ("synthetic.payment.unknown",)}, "unresolved Capability"),
        ({"result_probe_ref": "synthetic.payment.unknown-probe"}, "result probe"),
    ],
)
def test_stale_or_mismatched_accepted_installation_fails_closed(updates, message):
    with pytest.raises(ValueError, match=message):
        _build([_installation(**updates)])


def test_installation_is_closed_and_immutable():
    installation = _installation()

    with pytest.raises(ValidationError, match="extra_forbidden"):
        installation.__class__(**installation.model_dump(), browser_executor="forged")
    with pytest.raises(ValidationError, match="frozen_instance"):
        installation.status = DomainPackInstallationStatus.DISABLED
