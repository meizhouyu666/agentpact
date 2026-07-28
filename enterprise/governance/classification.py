"""Data classification and redaction primitives for Phase 2 model egress.

The functions are deliberately deterministic.  They are used before any future
LLM-facing semantic analysis and avoid plain SHA-256 fingerprints for sensitive
values.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from enum import StrEnum
from typing import Any, Mapping

from pydantic import BaseModel, Field


class DataClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    PII = "pii"
    FINANCIAL = "financial"
    CREDENTIAL = "credential"
    OTP = "otp"
    RESTRICTED = "restricted"


class ModelEgressPolicy(BaseModel):
    """Allowlist policy for data sent to a model endpoint."""

    model_id: str
    region: str
    allowed_classifications: set[DataClassification] = Field(
        default_factory=lambda: {DataClassification.PUBLIC, DataClassification.INTERNAL}
    )

    def allows(self, classification: DataClassification) -> bool:
        return classification in self.allowed_classifications


_CREDENTIAL_RE = re.compile(r"(password|passwd|pwd|secret|token)", re.IGNORECASE)
_OTP_RE = re.compile(r"\b\d{6}\b")
_PHONE_RE = re.compile(r"\b1[3-9]\d{9}\b")
_CARD_RE = re.compile(r"\b[3-6]\d{3}(?:[ -]?\d{4}){3}\b")


def classify_value(field_name: str, value: str | None) -> DataClassification:
    """Classify a value conservatively using field names and high-signal patterns."""

    name = field_name.lower()
    text = value or ""
    if _CREDENTIAL_RE.search(name):
        return DataClassification.CREDENTIAL
    if _OTP_RE.fullmatch(text):
        return DataClassification.OTP
    if _PHONE_RE.search(text):
        return DataClassification.PII
    if _CARD_RE.search(text):
        return DataClassification.FINANCIAL
    if any(
        token in name
        for token in (
            "account",
            "amount",
            "beneficiary",
            "card",
            "金额",
            "币种",
            "收款",
            "收款方",
            "账户",
            "账号",
            "银行卡",
            "受益人",
            "对象版本",
        )
    ):
        return DataClassification.FINANCIAL
    return DataClassification.INTERNAL


def redact_for_egress(value: str, classification: DataClassification) -> str:
    """Return a safe placeholder for values that policy does not allow to leave."""

    if classification in {DataClassification.CREDENTIAL, DataClassification.OTP}:
        return "[REDACTED_SECRET]"
    if classification == DataClassification.PII:
        return "[REDACTED_PII]"
    if classification in {DataClassification.FINANCIAL, DataClassification.RESTRICTED}:
        return "[REDACTED_FINANCIAL]"
    return value


def hmac_fingerprint(value: str | bytes, secret: str | bytes) -> str:
    """Create a rotatable keyed integrity fingerprint for sensitive values."""

    raw_value = value.encode("utf-8") if isinstance(value, str) else value
    raw_secret = secret.encode("utf-8") if isinstance(secret, str) else secret
    if not raw_secret:
        raise ValueError("HMAC secret must not be empty")
    return hmac.new(raw_secret, raw_value, hashlib.sha256).hexdigest()


def action_fingerprint(
    *,
    task_id: str,
    step_id: str,
    action_payload: Mapping[str, Any],
    observation_hash: str,
    secret: str | bytes,
) -> str:
    """Create the versioned HMAC binding used to authorize a browser action.

    The action can contain sensitive form values. They are only used as HMAC
    input; this function never returns or persists the source payload.
    """

    binding = {
        "schema": "phase2-action-fingerprint-v1",
        "task_id": task_id,
        "step_id": step_id,
        "observation_hash": observation_hash,
        "action": _normalize_for_json(dict(action_payload)),
    }
    canonical = json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hmac_fingerprint(canonical, secret)


def _normalize_for_json(value: Any) -> Any:
    """Make equivalent action payloads serialize identically before signing."""

    if isinstance(value, Mapping):
        return {str(key): _normalize_for_json(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(_normalize_for_json(item) for item in value)
    if isinstance(value, (list, tuple)):
        return [_normalize_for_json(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    return value
