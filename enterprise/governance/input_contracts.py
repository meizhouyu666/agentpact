"""Generic AgentPact input contracts shared by Packs and their adapters."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, JsonValue


class InputSource(StrEnum):
    USER = "user"
    SYSTEM = "system"
    ADAPTER = "adapter"
    OBSERVED = "observed"
    MODEL = "model"
    REJECT_MODEL = "reject_model"


class InputSensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"
    SECRET = "secret"


class InputSlotStatus(StrEnum):
    MISSING = "missing"
    INVALID = "invalid"
    READY = "ready"


class InputTargetKind(StrEnum):
    TEXT = "text"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    URL = "url"
    SELECT = "select"
    IDENTIFIER = "identifier"
    CUSTOM = "custom"


class InputSlotSpec(BaseModel):
    """A semantic business slot; adapter field names belong in FieldBinding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slot_name: str = Field(min_length=1, validation_alias=AliasChoices("slot_name", "name"))
    target_kind: InputTargetKind
    source: InputSource = InputSource.USER
    sensitivity: InputSensitivity = InputSensitivity.PUBLIC
    required: bool = True
    allowed_sources: tuple[InputSource, ...] = (
        InputSource.USER,
        InputSource.SYSTEM,
        InputSource.ADAPTER,
    )

    def model_post_init(self, __context: Any) -> None:
        if not self.allowed_sources or len(self.allowed_sources) != len(set(self.allowed_sources)):
            raise ValueError("Input slot allowed sources must be non-empty and unique")
        if self.source not in self.allowed_sources:
            raise ValueError("Input slot source must be allowed")
        if self.sensitivity in {InputSensitivity.SENSITIVE, InputSensitivity.SECRET} and InputSource.MODEL in self.allowed_sources:
            raise ValueError("Sensitive input slots cannot allow model as a source")

    @property
    def name(self) -> str:
        return self.slot_name


class AdapterRequirement(BaseModel):
    """An adapter-specific extra requirement, distinct from business slots."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_name: str = Field(min_length=1, validation_alias=AliasChoices("requirement_name", "name"))
    target_kind: InputTargetKind = InputTargetKind.CUSTOM
    required: bool = True
    source: InputSource = InputSource.ADAPTER
    sensitivity: InputSensitivity = InputSensitivity.INTERNAL
    description: str = ""

    def model_post_init(self, __context: Any) -> None:
        if self.sensitivity in {InputSensitivity.SENSITIVE, InputSensitivity.SECRET} and self.source is InputSource.MODEL:
            raise ValueError("Sensitive adapter requirements cannot use model as a source")


class FieldBinding(BaseModel):
    """Versioned mapping from a semantic slot to an adapter-owned field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_version: str = Field(default="v1", min_length=1, validation_alias=AliasChoices("binding_version", "version"))
    slot_name: str = Field(min_length=1, validation_alias=AliasChoices("slot_name", "slot"))
    adapter_field: str = Field(min_length=1, validation_alias=AliasChoices("adapter_field", "field"))
    target_kind: InputTargetKind
    adapter_id: str = Field(min_length=1)
    source: InputSource = InputSource.ADAPTER
    sensitivity: InputSensitivity = InputSensitivity.PUBLIC

    def model_post_init(self, __context: Any) -> None:
        if self.sensitivity in {InputSensitivity.SENSITIVE, InputSensitivity.SECRET} and self.source is InputSource.MODEL:
            raise ValueError("Sensitive field bindings cannot use model as a source")

    @property
    def version(self) -> str:
        return self.binding_version

    @property
    def field(self) -> str:
        return self.adapter_field


class InputRequest(BaseModel):
    """Canonical inputs and adapter status; recovery is pre-effect only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1)
    pack_id: str = ""
    pack_version: str = ""
    slots: tuple[InputSlotSpec, ...] = ()
    adapter_requirements: tuple[AdapterRequirement, ...] = ()
    bindings: tuple[FieldBinding, ...] = Field(default=())
    values: dict[str, JsonValue] = Field(default_factory=dict)
    status: dict[str, InputSlotStatus] = Field(default_factory=dict)
    recovery: bool = False
    recovery_mode: Literal["pre_effect_only"] = "pre_effect_only"
    external_effect_started: bool = False

    def model_post_init(self, __context: Any) -> None:
        slot_names = tuple(slot.slot_name for slot in self.slots)
        if len(slot_names) != len(set(slot_names)):
            raise ValueError("Input request slot names must be unique")
        binding_slots = tuple(binding.slot_name for binding in self.bindings)
        if len(binding_slots) != len(set(binding_slots)):
            raise ValueError("Input request bindings must be unique per slot")
        if set(self.values) - set(slot_names) or set(self.status) - set(slot_names):
            raise ValueError("Input values and statuses must reference declared slots")
        if self.recovery and self.external_effect_started:
            raise ValueError("Input recovery is permitted only before an external effect")

    @property
    def field_bindings(self) -> tuple[FieldBinding, ...]:
        return self.bindings

    @property
    def slot_status(self) -> dict[str, InputSlotStatus]:
        return dict(self.status)


# Short aliases make the contract vocabulary convenient without duplicating models.
Source = InputSource
Sensitivity = InputSensitivity
InputStatus = InputSlotStatus
TargetKind = InputTargetKind
