"""M9 authority-minimized Planner, trusted compilation, repair, and eval tests."""

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest

from enterprise.agent.constrained_planner import OpenAICompatiblePlanner
from enterprise.agent.work_orders import RecoveryLevel
from enterprise.domains.synthetic_payment.constants import CAPABILITY_ID
from tests.fixtures.synthetic_payment_runtime.m9_runtime import (
    M9PlannerCode,
    M9PlannerDisposition,
    M9PlannerEngine,
    M9ReplanPreconditions,
    M9StepRole,
    OpenAICompatibleM9Provider,
    PlanProposal,
    RecordedM9Provider,
    SuffixReplanProposal,
    build_m9_plan_input,
    build_m9_replan_input,
    compile_m9_plan,
    compile_m9_replan,
    load_agent_eval_cases,
    redact_replan_evidence,
    run_agent_eval,
)
from tests.unit.test_m6_constrained_planner import INPUTS, _compile


def _valid_plan_payload(planner_input):
    return {
        "capability_id": CAPABILITY_ID,
        "input_slots": [item.name for item in planner_input.input_slots],
        "step_roles": ["precheck", "submit", "confirm"],
    }


def _compiled_m9():
    authority = _compile()
    planner_input = build_m9_plan_input(authority)
    decision = M9PlannerEngine(RecordedM9Provider([_valid_plan_payload(planner_input)])).plan(planner_input)
    assert isinstance(decision.proposal, PlanProposal)
    compilation = compile_m9_plan(
        authority,
        decision.proposal,
        admission_id="admission-m9-unit",
        plan_run_id="plan-run-m9-unit",
    )
    return authority, planner_input, compilation


def _authority_with_inputs(inputs):
    authority = _compile()
    step = authority.business_plan.steps[0].model_copy(update={"inputs": inputs}, deep=True)
    plan = authority.business_plan.model_copy(update={"steps": [step]}, deep=True)
    return authority.model_copy(update={"business_plan": plan}, deep=True)


def test_model_input_contains_only_slot_metadata_and_trusted_compiler_injects_values():
    authority, planner_input, compilation = _compiled_m9()
    serialized = json.dumps(planner_input.model_dump(mode="json"), sort_keys=True)

    assert {item.name for item in planner_input.input_slots} == set(INPUTS)
    assert [item.navigation_goal.rsplit(" governed ", 1)[1].split(" for ", 1)[0] for item in compilation.work_orders] == [
        "precheck",
        "submit",
        "confirm",
    ]
    assert all(step.inputs == INPUTS for step in compilation.business_plan.steps)
    assert authority.business_plan.steps[0].inputs == INPUTS
    for value in INPUTS.values():
        if isinstance(value, str):
            assert value not in serialized
    for forbidden in (
        "grant_id",
        "contract_id",
        "tenant_id",
        "permit_id",
        "attempt_id",
        "adapter_ref",
        "probe_ref",
        "browser_action",
        "locator",
        "javascript",
    ):
        assert forbidden not in serialized.lower()


@pytest.mark.parametrize(
    ("trusted_inputs", "intent"),
    [
        ({"nested": {"value": "nested-secret"}}, "Process nested-secret"),
        ({"nested": {"value": 700001}}, "Process version 700001"),
        ({"nested": {"value": 700002.5}}, "Process amount 700002.5"),
        ({"nested": {"value": True}}, "Process flag true"),
        ({"nested": {"value": False}}, "Process flag FALSE"),
        ({"nested": {"value": Decimal("700003.75")}}, "Process decimal 700003.75"),
        ({"nested": [{"deeper": 700004}]}, "Process nested value 700004"),
    ],
)
def test_model_safe_boundary_recursively_rejects_every_trusted_value_type_before_provider(trusted_inputs, intent):
    with pytest.raises(ValueError, match="trusted business value"):
        build_m9_plan_input(
            _authority_with_inputs(trusted_inputs),
            intent_summary=intent,
        )


@pytest.mark.parametrize(
    ("invalid", "code"),
    [
        ("not-json", M9PlannerCode.MALFORMED_JSON),
        (
            lambda value: {"capability_id": CAPABILITY_ID, "input_slots": value["input_slots"]},
            M9PlannerCode.MISSING_REQUIRED_FIELD,
        ),
        (
            lambda value: {**value, "capability_id": 7},
            M9PlannerCode.WRONG_VALUE_TYPE,
        ),
        (
            lambda value: {**value, "step_roles": ["precheck", "audit", "confirm"]},
            M9PlannerCode.INVALID_STEP_ROLE,
        ),
        (
            lambda value: {**value, "comment": "format only"},
            M9PlannerCode.UNRECOGNIZED_NON_AUTHORITY_FIELD,
        ),
    ],
)
def test_only_five_structural_codes_receive_one_repair(invalid, code):
    authority = _compile()
    planner_input = build_m9_plan_input(authority)
    valid = _valid_plan_payload(planner_input)
    first = invalid(valid) if callable(invalid) else invalid
    provider = RecordedM9Provider([first, valid])

    decision = M9PlannerEngine(provider).plan(planner_input)

    assert decision.disposition is M9PlannerDisposition.REPAIRED
    assert decision.provider_calls == 2
    assert decision.repair_count == 1
    assert len(provider.calls) == 2
    assert provider.calls[1].repair is not None
    assert provider.calls[1].repair.structural_codes == (code,)
    repair_json = json.dumps(provider.calls[1].model_dump(mode="json"), sort_keys=True)
    assert str(first) not in repair_json
    for value in INPUTS.values():
        if isinstance(value, str):
            assert value not in repair_json


def test_authority_and_semantic_denials_are_terminal_and_win_over_structural_errors():
    planner_input = build_m9_plan_input(_compile())
    cases = [
        ('{"grant_id":"forged"', M9PlannerCode.FORBIDDEN_AUTHORITY_FIELD),
        (
            {**_valid_plan_payload(planner_input), "metadata": {"browser_action": {"selector": "#pay"}}},
            M9PlannerCode.FORBIDDEN_AUTHORITY_FIELD,
        ),
        (
            {"capability_id": "synthetic.payment.refund", "unexpected": "also malformed"},
            M9PlannerCode.CAPABILITY_NOT_PROJECTED,
        ),
        ({**_valid_plan_payload(planner_input), "business_inputs": {"amount": "secret"}}, M9PlannerCode.INPUT_SCOPE_EXPANSION),
        ({**_valid_plan_payload(planner_input), "input_slots": ["payment_id"]}, M9PlannerCode.UNDECLARED_INPUT_SLOT),
        ({**_valid_plan_payload(planner_input), "step_roles": ["submit", "precheck", "confirm"]}, M9PlannerCode.ILLEGAL_STEP_SEQUENCE),
    ]
    for raw, expected in cases:
        provider = RecordedM9Provider([raw, _valid_plan_payload(planner_input)])
        decision = M9PlannerEngine(provider).plan(planner_input)
        assert decision.disposition is M9PlannerDisposition.REJECTED
        assert decision.codes == (expected,)
        assert decision.provider_calls == 1
        assert decision.repair_count == 0
        assert len(provider.calls) == 1


@pytest.mark.parametrize(
    "reserved_alias",
    [
        "adapter_id",
        "adapter_ids",
        "grant_ids",
        "policy_decision_id",
        "authority_contract_id",
        "task_ids",
        "step_ids",
        "work_order_ids",
        "permit_ids",
        "attempt_ids",
        "result_probe_ids",
        "browser_action_ids",
        "html_ids",
        "screenshot_refs",
        "raw_browser_ids",
        "credential_refs",
        "authority_ids",
    ],
)
def test_reserved_authority_aliases_are_terminal_before_mixed_structural_errors(reserved_alias):
    planner_input = build_m9_plan_input(_compile())
    invalid = {
        "capability_id": CAPABILITY_ID,
        "input_slots": [item.name for item in planner_input.input_slots],
        reserved_alias: "forged",
    }
    provider = RecordedM9Provider([invalid, _valid_plan_payload(planner_input)])

    decision = M9PlannerEngine(provider).plan(planner_input)

    assert decision.disposition is M9PlannerDisposition.REJECTED
    assert decision.codes == (M9PlannerCode.FORBIDDEN_AUTHORITY_FIELD,)
    assert decision.provider_calls == 1
    assert decision.repair_count == 0
    assert len(provider.calls) == 1


def test_reserved_alias_scan_wins_even_when_json_is_malformed():
    planner_input = build_m9_plan_input(_compile())
    provider = RecordedM9Provider(
        ['{"adapter_id":"forged"', _valid_plan_payload(planner_input)]
    )

    decision = M9PlannerEngine(provider).plan(planner_input)

    assert decision.disposition is M9PlannerDisposition.REJECTED
    assert decision.codes == (M9PlannerCode.FORBIDDEN_AUTHORITY_FIELD,)
    assert decision.provider_calls == 1
    assert decision.repair_count == 0
    assert len(provider.calls) == 1


def test_second_invalid_response_is_terminal_without_plan_or_work_order():
    planner_input = build_m9_plan_input(_compile())
    provider = RecordedM9Provider(["not-json", {"step_roles": ["precheck", "submit", "confirm"]}])

    decision = M9PlannerEngine(provider).plan(planner_input)

    assert decision.disposition is M9PlannerDisposition.REJECTED
    assert decision.codes == (M9PlannerCode.MISSING_REQUIRED_FIELD,)
    assert decision.proposal is None
    assert decision.provider_calls == 2
    assert decision.repair_count == 1


def test_provider_and_repair_payloads_omit_recursive_trusted_value_canaries():
    trusted_inputs = {
        "nested": {
            "secret_text": "nested-secret",
            "secret_int": 700001,
            "secret_float": 700002.5,
            "secret_bool": True,
            "secret_decimal": Decimal("700003.75"),
        }
    }
    planner_input = build_m9_plan_input(_authority_with_inputs(trusted_inputs))
    provider = RecordedM9Provider(["not-json", _valid_plan_payload(planner_input)])

    decision = M9PlannerEngine(provider).plan(planner_input)

    assert decision.disposition is M9PlannerDisposition.REPAIRED
    payload = json.dumps([item.model_dump(mode="json") for item in provider.calls], sort_keys=True)
    for token in ("nested-secret", "700001", "700002.5", "700003.75", "secret_bool"):
        assert token not in payload
    assert "nested" not in payload


def test_provider_failure_is_terminal_without_repair_or_fallback():
    planner_input = build_m9_plan_input(_compile())

    class FailedProvider:
        def propose(self, *_args, **_kwargs):
            raise OSError("provider unavailable")

    decision = M9PlannerEngine(FailedProvider()).plan(planner_input)

    assert decision.disposition is M9PlannerDisposition.REJECTED
    assert decision.codes == (M9PlannerCode.PROVIDER_FAILURE,)
    assert decision.provider_calls == 1
    assert decision.repair_count == 0


def test_openai_compatible_adapter_reuses_injected_transport_without_leaking_values(monkeypatch):
    planner_input = build_m9_plan_input(_compile())
    captured = {}

    def transport(*, endpoint, api_key, payload):
        captured.update(endpoint=endpoint, api_key=api_key, payload=payload)
        return {"choices": [{"message": {"content": json.dumps(_valid_plan_payload(planner_input))}}]}

    monkeypatch.setenv("M9_TEST_API_KEY", "environment-only-m9-secret")
    provider = OpenAICompatibleM9Provider(
        OpenAICompatiblePlanner(
            endpoint="https://model.invalid/v1",
            model="recorded-m9",
            transport=transport,
            api_key_env="M9_TEST_API_KEY",
        )
    )

    decision = M9PlannerEngine(provider).plan(planner_input)

    assert decision.disposition is M9PlannerDisposition.ACCEPTED
    prompt = json.dumps(captured["payload"], sort_keys=True)
    assert captured["api_key"] == "environment-only-m9-secret"
    assert "environment-only-m9-secret" not in prompt
    for value in INPUTS.values():
        if isinstance(value, str):
            assert value not in prompt
    assert "grant_id" not in prompt.lower()
    assert "business_inputs" not in prompt.lower()


def test_legal_model_suffix_replan_reuses_m8_authority_and_redacted_evidence():
    _authority, _planner_input, previous = _compiled_m9()
    token = redact_replan_evidence(
        mismatch_code="BUSINESS_STATE_CHANGED",
        step_role=M9StepRole.SUBMIT,
        raw_evidence={"permit_id": "secret-permit", "html": "raw-browser-state"},
    )
    replan_input = build_m9_replan_input(
        previous,
        completed_prefix_length=1,
        remaining_replans=1,
        evidence_tokens=(token,),
    )
    provider = RecordedM9Provider([{"step_roles": ["submit", "confirm"]}])

    decision = M9PlannerEngine(provider).replan(
        replan_input,
        preconditions=M9ReplanPreconditions(),
    )

    assert decision.disposition is M9PlannerDisposition.ACCEPTED
    assert isinstance(decision.proposal, SuffixReplanProposal)
    replacement = compile_m9_replan(previous, decision.proposal, completed_prefix_length=1)
    assert replacement.business_plan.steps[0] == previous.business_plan.steps[0]
    assert replacement.business_plan.version == previous.business_plan.version + 1
    assert [item.inputs for item in replacement.business_plan.steps] == [INPUTS, INPUTS, INPUTS]
    serialized = json.dumps(replan_input.model_dump(mode="json"), sort_keys=True)
    assert "secret-permit" not in serialized
    assert "raw-browser-state" not in serialized
    assert token.content_digest in serialized


@pytest.mark.parametrize(
    ("preconditions", "code"),
    [
        (M9ReplanPreconditions(attempt_unknown=True), M9PlannerCode.UNKNOWN_STATE),
        (
            M9ReplanPreconditions(required_recovery_level=RecoveryLevel.L4),
            M9PlannerCode.L4_REAUTHORIZATION_REQUIRED,
        ),
        (M9ReplanPreconditions(remaining_replans=0), M9PlannerCode.REPLAN_BUDGET_EXHAUSTED),
        (M9ReplanPreconditions(projection_fresh=False), M9PlannerCode.STALE_PROJECTION),
        (M9ReplanPreconditions(executable_capability=False), M9PlannerCode.NO_EXECUTABLE_CAPABILITY),
    ],
)
def test_replan_terminal_preconditions_never_invoke_provider(preconditions, code):
    _authority, _planner_input, previous = _compiled_m9()
    replan_input = build_m9_replan_input(
        previous,
        completed_prefix_length=1,
        remaining_replans=1,
        evidence_tokens=(
            redact_replan_evidence(
                mismatch_code="BUSINESS_STATE_CHANGED",
                step_role=M9StepRole.SUBMIT,
                raw_evidence="redacted-at-boundary",
            ),
        ),
    )
    provider = RecordedM9Provider([{"step_roles": ["submit", "confirm"]}])

    decision = M9PlannerEngine(provider).replan(replan_input, preconditions=preconditions)

    assert decision.disposition is M9PlannerDisposition.REJECTED
    assert decision.codes == (code,)
    assert decision.provider_calls == 0
    assert provider.calls == []


def test_recorded_eval_corpus_is_deterministic_and_report_is_redacted():
    _authority, _normal_plan_input, previous = _compiled_m9()
    planner_input = build_m9_plan_input(
        _authority_with_inputs(
            {
                "nested": {
                    "secret_text": "nested-secret",
                    "secret_int": 700001,
                    "secret_float": 700002.5,
                    "secret_bool": True,
                    "secret_decimal": Decimal("700003.75"),
                }
            }
        )
    )
    replan_input = build_m9_replan_input(
        previous,
        completed_prefix_length=1,
        remaining_replans=1,
        evidence_tokens=(
            redact_replan_evidence(
                mismatch_code="BUSINESS_STATE_CHANGED",
                step_role=M9StepRole.SUBMIT,
                raw_evidence={"browser": "never retained"},
            ),
        ),
    )
    cases = load_agent_eval_cases(Path("tests/fixtures/m9_agent_eval_cases.json"))

    first = run_agent_eval(cases, plan_input=planner_input, replan_input=replan_input)
    second = run_agent_eval(cases, plan_input=planner_input, replan_input=replan_input)

    assert first == second
    assert first.case_count == 10
    assert first.passed_case_count == 10
    assert first.schema_version == "agentpact-agent-eval/v2"
    assert first.rejected_case_count == 7
    assert all(item.passed for item in first.cases)
    assert len(first.report_digest) == 64
    assert all(
        getattr(first, field) == 1.0
        for field in (
            "plan_schema_validity_rate",
            "capability_selection_accuracy",
            "authority_compliance_rate",
            "legal_replan_acceptance_rate",
            "hallucination_rejection_rate",
            "repair_success_rate",
        )
    )
    report_json = json.dumps(first.model_dump(mode="json"), sort_keys=True)
    for value in INPUTS.values():
        if isinstance(value, str):
            assert value not in report_json
    assert "browser" not in report_json.lower()
    assert "permit" not in report_json.lower()
    fixture_json = Path("tests/fixtures/m9_agent_eval_cases.json").read_text(encoding="utf-8")
    for artifact in (fixture_json, report_json):
        for token in ("nested-secret", "700001", "700002.5", "700003.75", "secret_bool"):
            assert token not in artifact


def test_openai_usage_is_invocation_local_under_concurrency(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("M12_USAGE_KEY", "credential-canary")
    _authority, base_input, _previous = _compiled_m9()

    def transport(*, endpoint, api_key, payload):
        del endpoint
        assert api_key == "credential-canary"
        safe_input = json.loads(payload["messages"][1]["content"])["safe_input"]
        token_count = int(safe_input["intent_summary"].rsplit("-", 1)[-1])
        return {
            "choices": [{"message": {"content": json.dumps(_valid_plan_payload(base_input))}}],
            "usage": {"input_tokens": token_count, "output_tokens": 2, "total_tokens": token_count + 2},
        }

    planner = OpenAICompatiblePlanner(
        endpoint="https://provider.invalid/v1",
        model="m12-model",
        api_key_env="M12_USAGE_KEY",
        transport=transport,
    )

    def invoke(token_count: int):
        planner_input = base_input.model_copy(update={"intent_summary": f"safe-intent-{token_count}"})
        return M9PlannerEngine(OpenAICompatibleM9Provider(planner), provider_mode="live").plan(planner_input)

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(invoke, (11, 29)))

    assert [item.observation.usage.prompt_tokens for item in decisions if item.observation.usage] == [11, 29]
    assert all(item.observation.duration_ms is not None for item in decisions)


def test_openai_usage_rejects_malformed_counts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("M12_USAGE_KEY", "credential-canary")
    _authority, planner_input, _previous = _compiled_m9()

    def transport(**_kwargs):
        return {
            "choices": [{"message": {"content": json.dumps(_valid_plan_payload(planner_input))}}],
            "usage": {"prompt_tokens": -1},
        }

    planner = OpenAICompatiblePlanner(
        endpoint="https://provider.invalid/v1",
        model="m12-model",
        api_key_env="M12_USAGE_KEY",
        transport=transport,
    )
    decision = M9PlannerEngine(OpenAICompatibleM9Provider(planner), provider_mode="live").plan(planner_input)
    assert decision.disposition is M9PlannerDisposition.REJECTED
    assert decision.codes == (M9PlannerCode.PROVIDER_FAILURE,)
    assert decision.observation.usage is None
