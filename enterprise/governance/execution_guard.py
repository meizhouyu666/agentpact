"""Integrity checks required before a permit can reach the browser executor."""

from __future__ import annotations

import hmac
from typing import Any

from .audit import observation_hash
from .classification import action_fingerprint
from .contracts import ExecutionAuthorization


class ExecutionAuthorizationError(ValueError):
    pass


def verify_execution_authorization(
    *,
    authorization: ExecutionAuthorization,
    task_id: str,
    step_id: str,
    action: Any,
    page_url: str,
    page_html: str,
    hmac_secret: str | bytes | None,
) -> None:
    """Verify that a permit reference is bound to this exact browser action.

    No value supplied by an upstream governor is trusted as-is: the public
    execution boundary derives both bindings again from the current action and
    the page it is about to operate on.
    """

    if not hmac_secret:
        raise ExecutionAuthorizationError("Governed execution requires GOVERNANCE_AUDIT_HMAC_SECRET")
    if not authorization.idempotency_key:
        raise ExecutionAuthorizationError("Governed execution requires an idempotency key")

    current_observation_hash = observation_hash(url=page_url, html=page_html, secret=hmac_secret)
    payload = action.model_dump(mode="json", exclude_none=True)
    current_action_fingerprint = action_fingerprint(
        task_id=task_id,
        step_id=step_id,
        action_payload=payload,
        observation_hash=current_observation_hash,
        secret=hmac_secret,
    )
    if not hmac.compare_digest(authorization.observation_hash, current_observation_hash):
        raise ExecutionAuthorizationError("Execution authorization does not match the current page observation")
    if not hmac.compare_digest(authorization.action_fingerprint, current_action_fingerprint):
        raise ExecutionAuthorizationError("Execution authorization does not match the current action")
