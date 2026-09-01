"""Atomic persistence tests for audit-only governed Task admissions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect

from enterprise.domains.synthetic_payment.constants import CAPABILITY_ID, TENANT_ID
from enterprise.domains.synthetic_payment.models import PaymentFacts
from enterprise.governance.admission import (
    GovernedTaskAdmissionService,
    TaskAdmissionBundle,
    canonical_task_admission_payload,
)
from enterprise.governance.admission_persistence import (
    SqlAlchemyTaskAdmissionRepository,
    TaskAdmissionConflict,
)
from enterprise.governance.models import GovernanceOutboxModel, GovernedTaskAdmissionModel
from tests.fixtures.synthetic_payment_admission import SyntheticPaymentTaskAdmissionEntry

NOW = datetime(2026, 7, 25, tzinfo=timezone.utc)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value


class _Store:
    def __init__(self):
        self.admissions: dict[tuple[str, str], GovernedTaskAdmissionModel] = {}
        self.outboxes: list[GovernanceOutboxModel] = []
        self.commit_count = 0
        self.rollback_count = 0
        self.flush_count = 0
        self.fail_flush = False


class _Transaction:
    def __init__(self, session: "_FakeSession") -> None:
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, _exc, _tb):
        if exc_type is not None:
            self._session.store.rollback_count += 1
            self._session.pending.clear()
            return False
        for model in self._session.pending:
            if isinstance(model, GovernedTaskAdmissionModel):
                key = (model.organization_id, model.request_id)
                self._session.store.admissions[key] = model
            elif isinstance(model, GovernanceOutboxModel):
                self._session.store.outboxes.append(model)
        self._session.store.commit_count += 1
        self._session.pending.clear()
        return False


class _FakeSession:
    def __init__(self, store: _Store) -> None:
        self.store = store
        self.pending: list[object] = []

    def begin(self):
        return _Transaction(self)

    def add(self, model):
        self.pending.append(model)

    async def flush(self):
        self.store.flush_count += 1
        if self.store.fail_flush:
            raise RuntimeError("injected flush failure")

    async def scalars(self, _statement):
        existing = next(iter(self.store.admissions.values()), None)
        return _ScalarResult(existing)


class _SessionContext:
    def __init__(self, store: _Store) -> None:
        self.session = _FakeSession(store)

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False


def _repository(store: _Store) -> SqlAlchemyTaskAdmissionRepository:
    return SqlAlchemyTaskAdmissionRepository(
        lambda: _SessionContext(store),
        allowed_tenant_ids=frozenset({TENANT_ID}),
        allowed_capability_ids=frozenset({CAPABILITY_ID}),
        clock=lambda: NOW,
    )


def _facts(amount: str = "125.00") -> PaymentFacts:
    return PaymentFacts(
        payment_id="admission-pay-1",
        beneficiary_id="synthetic-vendor-1",
        amount=amount,
        currency="CNY",
        reference="Synthetic admission invoice",
    )


def _bundle(*, amount: str = "125.00", now: datetime = NOW):
    entry = SyntheticPaymentTaskAdmissionEntry(
        GovernedTaskAdmissionService(_repository(_Store())),
        clock=lambda: now,
    )
    return entry.prepare_bundle(request_id="admission-request-1", facts=_facts(amount))


def test_admission_models_define_audit_only_idempotency_and_outbox_constraints():
    admission = inspect(GovernedTaskAdmissionModel)
    outbox = inspect(GovernanceOutboxModel)

    assert admission.local_table.name == "governed_task_admissions"
    assert outbox.local_table.name == "governance_outbox"
    assert {constraint.name for constraint in admission.local_table.constraints} >= {
        "ck_governed_task_admission_audit_only",
        "uq_gta_org_request",
        "uq_gta_org_task",
    }
    assert {constraint.name for constraint in outbox.local_table.constraints} >= {
        "ck_governance_outbox_status",
        "ck_governance_outbox_attempt_count",
        "uq_governance_outbox_admission_event",
    }


def test_repository_requires_explicit_nonempty_scope_allowlists():
    with pytest.raises(ValueError, match="explicit tenant and capability allowlists"):
        SqlAlchemyTaskAdmissionRepository(
            lambda: _SessionContext(_Store()),
            allowed_tenant_ids=frozenset(),
            allowed_capability_ids=frozenset(),
        )


def test_canonical_admission_payload_is_stable_across_unordered_json_round_trip() -> None:
    bundle = _bundle()
    expected = canonical_task_admission_payload(bundle)
    reordered = bundle.model_dump(mode="json")
    reordered["contract"]["allowed_operations"].reverse()
    reordered["request"]["resource_refs"].reverse()
    reordered["grants"][0]["allowed_dimensions"].reverse()
    for work_order in reordered["work_orders"]:
        work_order["allowed_operations"].reverse()
        work_order["prohibited_operations"].reverse()

    restored = TaskAdmissionBundle.model_validate(reordered)

    assert canonical_task_admission_payload(restored) == expected


@pytest.mark.asyncio
async def test_repository_rejects_a_tenant_or_capability_outside_its_allowlist():
    bundle = _bundle()
    wrong_tenant = SqlAlchemyTaskAdmissionRepository(
        lambda: _SessionContext(_Store()),
        allowed_tenant_ids=frozenset({"another_tenant"}),
        allowed_capability_ids=frozenset({CAPABILITY_ID}),
    )
    with pytest.raises(ValueError, match="tenant is outside"):
        await wrong_tenant.persist_atomic(bundle)

    wrong_capability = SqlAlchemyTaskAdmissionRepository(
        lambda: _SessionContext(_Store()),
        allowed_tenant_ids=frozenset({TENANT_ID}),
        allowed_capability_ids=frozenset({"another.capability"}),
    )
    with pytest.raises(ValueError, match="capability is outside"):
        await wrong_capability.persist_atomic(bundle)

    hidden_request_capability = bundle.model_copy(
        update={"request": bundle.request.model_copy(update={"capability_ref": "another.capability"})},
        deep=True,
    )
    with pytest.raises(ValueError, match="capability is outside"):
        await _repository(_Store()).persist_atomic(hidden_request_capability)


@pytest.mark.asyncio
async def test_repository_commits_admission_and_redacted_outbox_in_one_transaction():
    store = _Store()
    bundle = _bundle()

    receipt = await _repository(store).persist_atomic(bundle)

    assert receipt.duplicate is False
    assert store.commit_count == 1
    assert store.rollback_count == 0
    assert store.flush_count == 2
    assert len(store.admissions) == 1
    assert len(store.outboxes) == 1
    assert store.admissions[(TENANT_ID, bundle.request.request_id)].bundle_payload == canonical_task_admission_payload(
        bundle
    )
    assert store.outboxes[0].admission_id == receipt.admission_id
    assert "typed_inputs" not in str(store.outboxes[0].payload)
    assert store.outboxes[0].status == "pending"


@pytest.mark.asyncio
async def test_repository_returns_prior_receipt_for_semantic_retry_and_rejects_changed_request():
    store = _Store()
    repository = _repository(store)
    first = _bundle(now=NOW)
    regenerated_retry = _bundle(now=NOW + timedelta(seconds=10))

    original_receipt = await repository.persist_atomic(first)
    duplicate_receipt = await repository.persist_atomic(regenerated_retry)

    assert duplicate_receipt.duplicate is True
    assert duplicate_receipt.admission_id == original_receipt.admission_id
    assert duplicate_receipt.committed_at == original_receipt.committed_at
    assert len(store.admissions) == 1
    assert len(store.outboxes) == 1

    with pytest.raises(TaskAdmissionConflict, match="different admission semantics"):
        await repository.persist_atomic(_bundle(amount="126.00"))

    changed_work_order = _bundle().model_copy(deep=True)
    changed_work_order.work_orders[0].navigation_goal = "Substituted navigation goal"
    with pytest.raises(TaskAdmissionConflict, match="different admission semantics"):
        await repository.persist_atomic(changed_work_order)

    changed_validity = _bundle().model_copy(deep=True)
    changed_validity.grants[0].expires_at += timedelta(seconds=1)
    changed_validity.contract.expires_at += timedelta(seconds=1)
    with pytest.raises(TaskAdmissionConflict, match="different admission semantics"):
        await repository.persist_atomic(changed_validity)


@pytest.mark.asyncio
async def test_repository_rolls_back_both_rows_when_flush_fails():
    store = _Store()
    store.fail_flush = True

    with pytest.raises(RuntimeError, match="injected flush failure"):
        await _repository(store).persist_atomic(_bundle())

    assert store.admissions == {}
    assert store.outboxes == []
    assert store.commit_count == 0
    assert store.rollback_count == 1


def test_ent_007_migration_is_additive_and_drops_outbox_before_admission():
    migration = (
        Path(__file__).parents[2] / "alembic" / "versions" / "2026_07_25_0007-governed_task_admission_outbox.py"
    ).read_text(encoding="utf-8")

    assert 'revision = "ent_007"' in migration
    assert 'down_revision = "ent_006"' in migration
    assert '"governed_task_admissions"' in migration
    assert '"governance_outbox"' in migration
    assert "uq_gta_org_request" in migration
    assert "ck_governed_task_admission_audit_only" in migration
    assert migration.index('op.drop_table("governance_outbox")') < migration.index(
        'op.drop_table("governed_task_admissions")'
    )
