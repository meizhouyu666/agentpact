"""Tests for recovery discovery and scheduler configuration defaults."""

import asyncio

import pytest

from enterprise.governance.resume_execution_service import ResumingTask, discover_resuming_tasks
from skyvern.forge.sdk.db.models import StepModel, TaskModel


class _Result:
    def __init__(self, value):
        self.value = value

    def all(self):
        return self.value

    def first(self):
        return self.value


class FakeSession:
    def __init__(self, task_status="resuming"):
        self.step = StepModel(step_id="step_1", task_id="task_1", organization_id="org_1", status="resuming")
        self.task = TaskModel(task_id="task_1", organization_id="org_1", status=task_status)

    async def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is StepModel:
            return _Result([self.step])
        if entity is TaskModel:
            return _Result(self.task)
        raise AssertionError(f"Unexpected entity query: {entity}")


def test_discovery_finds_resuming_step_for_resuming_or_pre_agent_running_task():
    assert asyncio.run(discover_resuming_tasks(db_session=FakeSession())) == [
        ResumingTask(task_id="task_1", step_id="step_1", organization_id="org_1")
    ]
    assert asyncio.run(discover_resuming_tasks(db_session=FakeSession(task_status="running"))) == [
        ResumingTask(task_id="task_1", step_id="step_1", organization_id="org_1")
    ]


def test_discovery_excludes_nonrecoverable_task_state_and_bad_limit():
    assert asyncio.run(discover_resuming_tasks(db_session=FakeSession(task_status="failed"))) == []
    with pytest.raises(ValueError, match="limit"):
        asyncio.run(discover_resuming_tasks(db_session=FakeSession(), limit=0))
