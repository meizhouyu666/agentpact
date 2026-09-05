from __future__ import annotations

import pytest
from pydantic import ValidationError

from enterprise.governance.input_contracts import (
    AdapterRequirement,
    FieldBinding,
    InputRequest,
    InputSensitivity,
    InputSlotSpec,
    InputSlotStatus,
    InputSource,
    InputTargetKind,
)


def _slot(name: str = "amount", *, sensitivity: InputSensitivity = InputSensitivity.PUBLIC) -> InputSlotSpec:
    return InputSlotSpec(slot_name=name, target_kind=InputTargetKind.NUMBER, sensitivity=sensitivity)


def test_valid_contract_construction_and_fake_adapter_boundary() -> None:
    slot = _slot()
    binding = FieldBinding(slot_name="amount", adapter_field="amount_minor", target_kind=InputTargetKind.NUMBER, adapter_id="fake-v1")
    request = InputRequest(
        request_id="req-1",
        pack_id="payments",
        pack_version="1.0",
        slots=(slot,),
        adapter_requirements=(AdapterRequirement(requirement_name="merchant_context"),),
        bindings=(binding,),
        values={"amount": 2500},
        status={"amount": InputSlotStatus.READY},
    )
    assert request.values == {"amount": 2500}
    assert request.slot_status["amount"] is InputSlotStatus.READY
    assert request.field_bindings[0].version == "v1"


def test_structured_status_does_not_promote_observed_values() -> None:
    request = InputRequest(
        request_id="req-2",
        slots=(_slot("reference"),),
        values={},
        status={"reference": InputSlotStatus.READY},
    )
    assert request.status["reference"] is InputSlotStatus.READY
    assert request.values == {}


def test_sensitive_slots_reject_model_source() -> None:
    with pytest.raises(ValueError, match="cannot allow model"):
        InputSlotSpec(
            slot_name="card_number",
            target_kind=InputTargetKind.TEXT,
            sensitivity=InputSensitivity.SENSITIVE,
            allowed_sources=(InputSource.USER, InputSource.MODEL),
        )


def test_sensitive_adapter_requirements_reject_model_source() -> None:
    with pytest.raises(ValueError, match="Sensitive adapter requirements"):
        AdapterRequirement(
            requirement_name="secret-token",
            sensitivity=InputSensitivity.SECRET,
            source=InputSource.MODEL,
        )


def test_boundary_invariants() -> None:
    with pytest.raises(ValueError, match="unique"):
        InputRequest(request_id="req-3", slots=(_slot(), _slot()))
    with pytest.raises(ValueError, match="declared slots"):
        InputRequest(request_id="req-4", slots=(_slot(),), values={"other": "x"})
    with pytest.raises(ValueError, match="only before"):
        InputRequest(request_id="req-5", recovery=True, external_effect_started=True)
    with pytest.raises(ValidationError):
        FieldBinding(slot_name="x", adapter_field="y", target_kind="nope", adapter_id="fake")


def test_one_pack_can_have_multiple_adapters_without_vendor_imports() -> None:
    slots = (_slot("amount"),)
    first = FieldBinding(slot_name="amount", adapter_field="amount_minor", target_kind=InputTargetKind.NUMBER, adapter_id="fake-a")
    second = FieldBinding(slot_name="amount", adapter_field="amount", target_kind=InputTargetKind.NUMBER, adapter_id="fake-b")
    assert InputRequest(request_id="req-6", pack_id="payments", slots=slots, bindings=(first,)).bindings[0].adapter_id == "fake-a"
    assert InputRequest(request_id="req-7", pack_id="payments", slots=slots, bindings=(second,)).bindings[0].adapter_id == "fake-b"
