"""Provider-neutral, model-safe single-step planning boundary."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from enterprise.governance.capabilities import CapabilityGrantSet, CapabilityRegistry


class ModelSafeCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str
    capability_version: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    expected_transition: dict[str, Any] = Field(default_factory=dict)
    non_sensitive_constraints: tuple[str, ...] = ("single_step", "trusted_server_compilation")


class ModelSafePlannerInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    natural_language_request: str = Field(min_length=1)
    capabilities: tuple[ModelSafeCapability, ...] = Field(min_length=1)


class PlannerProposal(BaseModel):
    """Closed untrusted schema; trusted governance fields cannot be supplied."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str = Field(min_length=1)
    business_inputs: dict[str, Any]


class PlannerOutputError(ValueError):
    pass


class PlannerProviderError(RuntimeError):
    pass


class ConstrainedPlanner(Protocol):
    def propose(self, planner_input: ModelSafePlannerInput) -> object:
        """Return untrusted structured data for the shared strict parser."""


class DeterministicPlanner:
    """Credential-free Planner used by tests and the canonical synthetic demo."""

    def __init__(self, business_inputs: Mapping[str, Any]) -> None:
        self._business_inputs = dict(business_inputs)

    def propose(self, planner_input: ModelSafePlannerInput) -> object:
        if len(planner_input.capabilities) != 1:
            raise PlannerOutputError("Deterministic Planner requires exactly one projected Capability")
        return {
            "capability_id": planner_input.capabilities[0].capability_id,
            "business_inputs": dict(self._business_inputs),
        }


class PlannerTransport(Protocol):
    def __call__(self, *, endpoint: str, api_key: str, payload: dict[str, Any]) -> object: ...


def httpx_openai_compatible_transport(*, endpoint: str, api_key: str, payload: dict[str, Any]) -> object:
    """Optional real transport. Credentials remain caller-supplied and environment-backed."""

    try:
        response = httpx.post(
            endpoint,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=30.0,
        )
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise PlannerProviderError("OpenAI-compatible Planner request failed") from exc


class OpenAICompatiblePlanner:
    """OpenAI-compatible adapter with an injected, mockable transport."""

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        transport: PlannerTransport | None = None,
        api_key_env: str = "OPENAI_API_KEY",
    ) -> None:
        self._endpoint = endpoint.rstrip("/") + "/chat/completions"
        self._model = model
        self._transport = transport or httpx_openai_compatible_transport
        self._api_key_env = api_key_env

    def propose(self, planner_input: ModelSafePlannerInput) -> object:
        return self.propose_structured(
            planner_input,
            response_model=PlannerProposal,
            schema_name="constrained_planner_proposal",
            system_prompt=(
                "Select exactly one supplied capability and return only JSON matching the response schema. "
                "Never invent governance or browser fields."
            ),
        )

    def propose_structured(
        self,
        planner_input: BaseModel,
        *,
        response_model: type[BaseModel],
        schema_name: str,
        system_prompt: str,
    ) -> object:
        """Invoke the same injected strict-JSON transport for a model-safe closed schema."""

        api_key = os.environ.get(self._api_key_env)
        if not api_key:
            raise PlannerProviderError(f"Planner credential environment variable is not set: {self._api_key_env}")
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": json.dumps(planner_input.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": response_model.model_json_schema(),
                },
            },
        }
        try:
            response = self._transport(endpoint=self._endpoint, api_key=api_key, payload=payload)
            return _extract_openai_content(response)
        except PlannerProviderError:
            raise
        except Exception as exc:
            raise PlannerProviderError("OpenAI-compatible Planner transport failed") from exc


def build_model_safe_projection(
    *,
    grants: CapabilityGrantSet,
    registry: CapabilityRegistry,
    now,
) -> tuple[ModelSafeCapability, ...]:
    """Project only currently executable capabilities without trusted identifiers."""

    projected: dict[str, ModelSafeCapability] = {}
    for grant in grants.executable_grants(now=now):
        definition = registry.require(grant.capability_id)
        if definition.version != grant.capability_version:
            raise ValueError("Executable Grant version does not match the active registry")
        projected[definition.capability_id] = ModelSafeCapability(
            capability_id=definition.capability_id,
            capability_version=definition.version,
            description=definition.display_name,
            input_schema=definition.input_schema,
            expected_transition=definition.state_transition,
        )
    if not projected:
        raise ValueError("No installed executable Capability is available to the Planner")
    return tuple(projected[key] for key in sorted(projected))


def parse_planner_proposal(value: object) -> PlannerProposal:
    """Parse every provider through the same closed proposal schema."""

    try:
        parsed = json.loads(value) if isinstance(value, str) else value
        return PlannerProposal.model_validate(parsed)
    except (TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
        raise PlannerOutputError("Planner output does not match the closed proposal schema") from exc


def require_projected_capability(
    proposal: PlannerProposal,
    projection: tuple[ModelSafeCapability, ...],
) -> ModelSafeCapability:
    for capability in projection:
        if capability.capability_id == proposal.capability_id:
            return capability
    raise PlannerOutputError("Planner selected a Capability outside its projection")


def _extract_openai_content(response: object) -> object:
    try:
        if not isinstance(response, Mapping):
            raise TypeError
        choices = response["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise TypeError
        message = choices[0]["message"]
        content = message["content"]
        if not isinstance(content, str):
            raise TypeError
        return content
    except (KeyError, IndexError, TypeError) as exc:
        raise PlannerProviderError("OpenAI-compatible Planner response was malformed") from exc
