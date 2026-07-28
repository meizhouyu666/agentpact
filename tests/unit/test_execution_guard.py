"""Tests for binding a governed execution to the exact action and page."""

import pytest

from enterprise.governance.audit import observation_hash
from enterprise.governance.classification import action_fingerprint
from enterprise.governance.contracts import ExecutionAuthorization, ExecutionEffect
from enterprise.governance.execution_guard import ExecutionAuthorizationError, verify_execution_authorization


class FakeAction:
    def __init__(self, text="100"):
        self.text = text

    def model_dump(self, **_kwargs):
        return {"action_type": "click", "element_id": "element_1", "text": self.text}


def _authorization(action: FakeAction, *, url="https://bank.example/pay", html="<button>Pay</button>"):
    secret = "test-key"
    observation = observation_hash(url=url, html=html, secret=secret)
    fingerprint = action_fingerprint(
        task_id="task_1",
        step_id="step_1",
        action_payload=action.model_dump(),
        observation_hash=observation,
        secret=secret,
    )
    return ExecutionAuthorization(
        permit_id="permit_1",
        action_fingerprint=fingerprint,
        observation_hash=observation,
        idempotency_key="payment:request_1",
        effect=ExecutionEffect.EXTERNAL_WRITE,
    )


def test_execution_guard_accepts_the_exact_authorized_binding():
    action = FakeAction()
    verify_execution_authorization(
        authorization=_authorization(action),
        task_id="task_1",
        step_id="step_1",
        action=action,
        page_url="https://bank.example/pay",
        page_html="<button>Pay</button>",
        hmac_secret="test-key",
    )


def test_execution_guard_rejects_page_or_action_drift():
    action = FakeAction()
    authorization = _authorization(action)

    with pytest.raises(ExecutionAuthorizationError, match="current page"):
        verify_execution_authorization(
            authorization=authorization,
            task_id="task_1",
            step_id="step_1",
            action=action,
            page_url="https://bank.example/pay",
            page_html="<button>Changed payee</button>",
            hmac_secret="test-key",
        )
    with pytest.raises(ExecutionAuthorizationError, match="current action"):
        verify_execution_authorization(
            authorization=authorization,
            task_id="task_1",
            step_id="step_1",
            action=FakeAction(text="200"),
            page_url="https://bank.example/pay",
            page_html="<button>Pay</button>",
            hmac_secret="test-key",
        )


def test_execution_guard_requires_a_configured_hmac_secret():
    action = FakeAction()

    with pytest.raises(ExecutionAuthorizationError, match="HMAC"):
        verify_execution_authorization(
            authorization=_authorization(action),
            task_id="task_1",
            step_id="step_1",
            action=action,
            page_url="https://bank.example/pay",
            page_html="<button>Pay</button>",
            hmac_secret=None,
        )
