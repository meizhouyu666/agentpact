"""Phase 2.0 governance contract and state-machine tests."""

from datetime import datetime, timedelta, timezone
import asyncio

import pytest

from enterprise.governance.classification import (
    DataClassification,
    ModelEgressPolicy,
    action_fingerprint,
    classify_value,
    hmac_fingerprint,
    redact_for_egress,
)
from enterprise.governance.contracts import (
    ExecutionPermit,
    GovernanceMode,
    PageReadiness,
    TaskContract,
)


class TestGovernanceContracts:
    def test_task_contract_defaults_to_audit_mode(self):
        contract = TaskContract(
            contract_id="tc_1",
            task_id="task_1",
            organization_id="org_1",
            goal="Download a report",
        )

        assert contract.mode == GovernanceMode.AUDIT
        assert contract.policy_version == "phase2-v1"
        assert contract.allowed_operations == set()

    def test_permit_requires_matching_unused_observation(self):
        now = datetime.now(timezone.utc)
        permit = ExecutionPermit(
            permit_id="permit_1",
            task_id="task_1",
            step_id="step_1",
            action_fingerprint="fp_1",
            observation_id="obs_1",
            policy_decision_id="decision_1",
            issued_at=now,
            expires_at=now + timedelta(minutes=1),
        )

        assert permit.matches(action_fingerprint="fp_1", observation_id="obs_1", now=now)
        assert not permit.matches(action_fingerprint="fp_2", observation_id="obs_1", now=now)
        assert not permit.matches(action_fingerprint="fp_1", observation_id="obs_2", now=now)

        permit.used_at = now
        assert not permit.matches(action_fingerprint="fp_1", observation_id="obs_1", now=now)

    def test_page_readiness_values_are_explicit(self):
        assert PageReadiness.BLOCKED.value == "blocked"
        assert PageReadiness.UNKNOWN.value == "unknown"


class TestDataClassification:
    def test_sensitive_values_are_classified_conservatively(self):
        assert classify_value("password", "anything") == DataClassification.CREDENTIAL
        assert classify_value("verification_code", "123456") == DataClassification.OTP
        assert classify_value("phone", "13800138000") == DataClassification.PII
        assert classify_value("beneficiary_account", "1234") == DataClassification.FINANCIAL
        assert classify_value("金额", "100") == DataClassification.FINANCIAL
        assert classify_value("收款方账号", "1234") == DataClassification.FINANCIAL

    def test_model_policy_and_redaction(self):
        policy = ModelEgressPolicy(model_id="internal", region="cn")
        assert policy.allows(DataClassification.INTERNAL)
        assert not policy.allows(DataClassification.CREDENTIAL)
        assert redact_for_egress("secret", DataClassification.CREDENTIAL) == "[REDACTED_SECRET]"

    def test_hmac_fingerprint_is_keyed_and_deterministic(self):
        first = hmac_fingerprint("6222020202020202", "key-a")
        assert first == hmac_fingerprint("6222020202020202", "key-a")
        assert first != hmac_fingerprint("6222020202020202", "key-b")
        with pytest.raises(ValueError):
            hmac_fingerprint("value", "")

    def test_action_fingerprint_binds_action_and_execution_context(self):
        kwargs = {
            "task_id": "task_1",
            "step_id": "step_1",
            "action_payload": {"action_type": "input_text", "element_id": "e_1", "text": "secret-value"},
            "observation_hash": "obs_hash_1",
            "secret": "key-a",
        }

        first = action_fingerprint(**kwargs)
        assert first == action_fingerprint(**kwargs)
        assert first != action_fingerprint(**(kwargs | {"observation_hash": "obs_hash_2"}))
        assert first != action_fingerprint(**(kwargs | {"step_id": "step_2"}))
        assert first != action_fingerprint(
            **(kwargs | {"action_payload": {"action_type": "input_text", "element_id": "e_1", "text": "other"}})
        )


class TestPausedStateTransitions:
    def test_task_can_pause_and_resume(self):
        from skyvern.forge.sdk.schemas.tasks import TaskStatus

        assert TaskStatus.running.can_update_to(TaskStatus.pending_approval)
        assert TaskStatus.pending_approval.can_update_to(TaskStatus.resuming)
        assert TaskStatus.resuming.can_update_to(TaskStatus.running)
        assert TaskStatus.pending_approval.can_update_to(TaskStatus.failed)
        assert not TaskStatus.pending_approval.is_final()

    def test_step_can_pause_and_resume_without_output(self):
        from skyvern.forge.sdk.models import StepStatus

        assert StepStatus.running.can_update_to(StepStatus.pending_approval)
        assert StepStatus.pending_approval.can_update_to(StepStatus.resuming)
        assert StepStatus.resuming.can_update_to(StepStatus.running)
        assert StepStatus.pending_approval.cant_have_output()
        assert not StepStatus.pending_approval.is_terminal()


class TestTaskContractPersistence:
    def test_ensure_contract_persists_a_trusted_native_creation_snapshot_once(self):
        from enterprise.governance.contracts_service import ensure_task_contract
        from enterprise.governance.creation_snapshot import TaskCreationPath, TrustedTaskCreationSnapshot

        class Result:
            def first(self):
                return None

        class Session:
            def __init__(self):
                self.added = []

            async def scalars(self, _statement):
                return Result()

            def add(self, value):
                self.added.append(value)

            async def flush(self):
                pass

        class Task:
            task_id = "task_1"
            organization_id = "org_1"
            navigation_goal = "Download statement"
            title = "Statement"

        session = Session()
        snapshot = TrustedTaskCreationSnapshot(
            task_id="task_1",
            organization_id="org_1",
            creation_path=TaskCreationPath.NATIVE,
            initiator_id="user_requester",
            authorization_snapshot={"department_ids": ["dept_finance"]},
            policy_version="policy-v2",
            contract_version=2,
            created_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
            request_id="request_1",
        )
        contract = asyncio.run(
            ensure_task_contract(
                db_session=session,
                task=Task(),
                mode="audit",
                creation_snapshot=snapshot,
            )
        )

        assert contract.task_id == "task_1"
        assert contract.mode == "audit"
        assert contract.authorization_snapshot["creation_path"] == "native_task"
        assert contract.initiator_id == "user_requester"
        assert contract.version == 2
        assert contract.policy_version == "policy-v2"
        assert session.added == [contract]

    def test_contract_rejects_a_task_that_does_not_match_its_creation_snapshot(self):
        from enterprise.governance.contracts_service import ensure_task_contract
        from enterprise.governance.creation_snapshot import TaskCreationPath, TrustedTaskCreationSnapshot

        class Result:
            def first(self):
                return None

        class Session:
            async def scalars(self, _statement):
                return Result()

            async def flush(self):
                pass

        class Task:
            task_id = "task_1"
            organization_id = "org_1"
            navigation_goal = "Pay supplier"
            title = "Payment"

        snapshot = TrustedTaskCreationSnapshot(
            task_id="task_other",
            organization_id="org_1",
            creation_path=TaskCreationPath.WORKFLOW,
            initiator_id="user_requester",
            policy_version="policy-v1",
            contract_version=1,
            created_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
            workflow_id="workflow_1",
            workflow_run_id="workflow_run_1",
        )
        with pytest.raises(ValueError, match="must match"):
            asyncio.run(
                ensure_task_contract(
                    db_session=Session(),
                    task=Task(),
                    mode="audit",
                    creation_snapshot=snapshot,
                )
            )
