from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from enterprise.agent_runs.pause_signal import (
    RunPauseAction,
    RunPauseOutcome,
    RunPausePromptMetadata,
    RunPauseSignal,
    RunResumePolicy,
)
from enterprise.governance.input_contracts import InputRequest


def test_awaiting_input_is_redacted_and_identity_bound() -> None:
    signal = RunPauseSignal(
        outcome=RunPauseOutcome.AWAITING_INPUT,
        reason_code="MISSING_BENEFICIARY",
        run_id="run-1",
        task_id="task-1",
        step_id="step-2",
        checkpoint_id="checkpoint-3",
        input_request=InputRequest(request_id="req-1"),
        prompt=RunPausePromptMetadata(title="More information", message="Provide the missing value"),
        allowed_actions=(RunPauseAction.SUBMIT_INPUT, RunPauseAction.CANCEL),
        resume_policy=RunResumePolicy.INPUT_SUBMISSION,
        expires_at=datetime(2026, 9, 6, tzinfo=timezone.utc),
    )
    assert signal.prompt is not None and signal.prompt.redacted is True
    assert signal.run_id == "run-1"


def test_awaiting_input_cannot_follow_external_effect_or_use_human_policy() -> None:
    with pytest.raises(ValueError, match="only before"):
        RunPauseSignal(
            outcome=RunPauseOutcome.AWAITING_INPUT,
            reason_code="NEEDS_VALUE",
            run_id="run-1",
            resume_policy=RunResumePolicy.INPUT_SUBMISSION,
            external_effect_started=True,
            input_request=InputRequest(request_id="req-effect", external_effect_started=True),
        )
    with pytest.raises(ValueError, match="input-submission"):
        RunPauseSignal(
            outcome=RunPauseOutcome.AWAITING_INPUT,
            reason_code="NEEDS_VALUE",
            run_id="run-1",
            resume_policy=RunResumePolicy.HUMAN_TAKEOVER,
            input_request=InputRequest(request_id="req-policy"),
            allowed_actions=(RunPauseAction.SUBMIT_INPUT,),
        )


def test_awaiting_input_requires_request_and_submit_action() -> None:
    with pytest.raises(ValueError, match="input request"):
        RunPauseSignal(
            outcome=RunPauseOutcome.AWAITING_INPUT,
            reason_code="NEEDS_VALUE",
            run_id="run-1",
            allowed_actions=(RunPauseAction.CANCEL,),
            resume_policy=RunResumePolicy.INPUT_SUBMISSION,
        )
    with pytest.raises(ValueError, match="submit-input"):
        RunPauseSignal(
            outcome=RunPauseOutcome.AWAITING_INPUT,
            reason_code="NEEDS_VALUE",
            run_id="run-1",
            input_request=InputRequest(request_id="req-no-submit"),
            allowed_actions=(RunPauseAction.CANCEL,),
            resume_policy=RunResumePolicy.INPUT_SUBMISSION,
        )


def test_needs_human_allows_takeover_but_never_input_recovery_after_effect() -> None:
    signal = RunPauseSignal(
        outcome=RunPauseOutcome.NEEDS_HUMAN,
        reason_code="AMBIGUOUS_RESULT",
        run_id="run-2",
        allowed_actions=(RunPauseAction.TAKE_OVER, RunPauseAction.CANCEL),
        resume_policy=RunResumePolicy.HUMAN_TAKEOVER,
        external_effect_started=True,
        input_request=InputRequest(request_id="req-1", external_effect_started=True),
    )
    assert signal.outcome is RunPauseOutcome.NEEDS_HUMAN
    with pytest.raises(ValueError, match="only valid"):
        RunPauseSignal(
            outcome=RunPauseOutcome.NEEDS_HUMAN,
            reason_code="AMBIGUOUS_RESULT",
            run_id="run-2",
            resume_policy=RunResumePolicy.HUMAN_TAKEOVER,
            input_request=InputRequest(request_id="req-2", recovery=True),
        )


def test_contract_rejects_unstable_or_raw_extra_fields() -> None:
    with pytest.raises(ValidationError):
        RunPauseSignal(
            outcome="needs_human",
            reason_code="vendor.reason",
            run_id="run-1",
            resume_policy="manual_review",
        )
    with pytest.raises(ValidationError):
        RunPauseSignal(
            outcome="needs_human",
            reason_code="AMBIGUOUS",
            run_id="run-1",
            resume_policy="manual_review",
            adapter_payload={"raw": "vendor"},
        )


def test_external_effect_removes_input_submission_action() -> None:
    with pytest.raises(ValueError, match="Input submission"):
        RunPauseSignal(
            outcome=RunPauseOutcome.NEEDS_HUMAN,
            reason_code="EFFECT_STARTED",
            run_id="run-1",
            resume_policy=RunResumePolicy.MANUAL_REVIEW,
            external_effect_started=True,
            allowed_actions=(RunPauseAction.SUBMIT_INPUT,),
        )


def test_external_effect_input_request_is_diagnostic_only() -> None:
    with pytest.raises(ValueError, match="Input submission"):
        RunPauseSignal(
            outcome=RunPauseOutcome.NEEDS_HUMAN,
            reason_code="EFFECT_STARTED",
            run_id="run-1",
            resume_policy=RunResumePolicy.MANUAL_REVIEW,
            external_effect_started=True,
            input_request=InputRequest(request_id="req-diagnostic", external_effect_started=True),
            allowed_actions=(RunPauseAction.SUBMIT_INPUT,),
        )
