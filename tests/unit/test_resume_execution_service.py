"""Tests for safely claiming a task before fresh recovery perception."""

import asyncio
from types import SimpleNamespace

import pytest

from enterprise.governance import resume_execution_service
from enterprise.governance.models import ExecutionAttemptModel
from enterprise.governance.pack_runtime import ExecutionCheckpoint
from enterprise.governance.resume_execution_service import (
    ResumeExecutionError,
    claim_resuming_task_for_execution,
    execute_resuming_task,
    suspend_unknown_execution_for_probe,
)
from skyvern.forge.sdk.db.models import StepModel, TaskModel
from skyvern.schemas.runs import RunEngine


class _Result:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class FakeSession:
    def __init__(self, task_status="resuming", step_status="resuming"):
        self.task = TaskModel(task_id="task_1", organization_id="org_1", status=task_status)
        self.step = StepModel(step_id="step_1", task_id="task_1", organization_id="org_1", status=step_status)
        self.attempt = ExecutionAttemptModel(
            attempt_id="attempt_1",
            permit_id="permit_1",
            task_id="task_1",
            step_id="step_1",
            contract_id="contract_1",
            action_fingerprint="action_fp",
            observation_hash="observation_fp",
            idempotency_key="secret-key",
            idempotency_key_digest="a" * 64,
            execution_effect="external_write",
            result_probe_ref="probe://orders/v1",
            status="unknown",
        )
        self.flush_count = 0

    async def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        values = {
            TaskModel: self.task,
            StepModel: self.step,
            ExecutionAttemptModel: self.attempt,
        }
        return _Result(values[entity])

    async def flush(self):
        self.flush_count += 1


def _claim(session):
    return asyncio.run(
        claim_resuming_task_for_execution(
            db_session=session,
            task_id="task_1",
            step_id="step_1",
            organization_id="org_1",
        )
    )


def test_claim_moves_only_task_to_running_before_agent_starts():
    session = FakeSession()
    _claim(session)

    assert session.task.status == "running"
    assert session.step.status == "resuming"
    assert session.flush_count == 1


def test_claim_is_idempotent_after_worker_crash_before_agent_start():
    session = FakeSession(task_status="running", step_status="resuming")
    _claim(session)

    assert session.task.status == "running"
    assert session.step.status == "resuming"


def test_claim_rejects_non_resuming_step():
    with pytest.raises(ResumeExecutionError, match="resuming step"):
        _claim(FakeSession(step_status="pending_approval"))


def _checkpoint() -> ExecutionCheckpoint:
    return ExecutionCheckpoint(
        permit_id="permit_1",
        attempt_id="attempt_1",
        task_id="task_1",
        step_id="step_1",
        action_fingerprint="action_fp",
        observation_hash="observation_fp",
        idempotency_key_digest="a" * 64,
        execution_effect="external_write",
        result_probe_ref="probe://orders/v1",
        attempt_status="unknown",
    )


def test_exact_unknown_checkpoint_suspends_running_task_and_step_for_probe():
    session = FakeSession(task_status="running", step_status="running")

    asyncio.run(
        suspend_unknown_execution_for_probe(
            db_session=session,
            organization_id="org_1",
            checkpoint=_checkpoint(),
        )
    )

    assert session.task.status == "pending_result_probe"
    assert session.step.status == "pending_result_probe"


def test_substituted_unknown_checkpoint_is_rejected():
    session = FakeSession(task_status="running", step_status="running")

    with pytest.raises(ResumeExecutionError, match="substituted"):
        asyncio.run(
            suspend_unknown_execution_for_probe(
                db_session=session,
                organization_id="org_1",
                checkpoint=_checkpoint().model_copy(update={"permit_id": "permit_substituted"}),
            )
        )


def test_execute_resuming_task_reuses_step_for_fresh_agent_perception():
    session = FakeSession()

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return False

    class Database:
        Session = staticmethod(lambda: SessionContext())

        async def get_organization(self, _organization_id):
            return SimpleNamespace(organization_id="org_1")

        async def get_task(self, **_kwargs):
            return SimpleNamespace(browser_session_id=None, browser_address=None)

        async def get_step(self, **_kwargs):
            return SimpleNamespace(step_id="step_1")

        async def get_run(self, **_kwargs):
            return None

    executed = []

    class Agent:
        async def execute_step(self, **kwargs):
            executed.append(kwargs)

    async def commit():
        pass

    session.commit = commit
    previous_app = object.__getattribute__(resume_execution_service.app, "_inst")
    object.__setattr__(resume_execution_service.app, "_inst", SimpleNamespace(DATABASE=Database(), agent=Agent()))
    try:
        asyncio.run(execute_resuming_task(task_id="task_1", step_id="step_1", organization_id="org_1"))
    finally:
        object.__setattr__(resume_execution_service.app, "_inst", previous_app)

    assert session.task.status == "running"
    assert executed[0]["step"].step_id == "step_1"
    assert executed[0]["engine"] == RunEngine.skyvern_v1
