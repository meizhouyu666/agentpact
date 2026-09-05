"""Recorded-first composition seam for the Stripe browser benchmark.

The runner in this module is intentionally a composition boundary, not a
browser or Stripe client.  It accepts already-produced recorded outcomes and
persists only redacted protocol artefacts.  A future Playwright implementation
can implement :class:`BrowserBenchmarkRunner` without changing this contract.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, model_validator

from enterprise.evaluation.quantitative_benchmark import Arm, BenchmarkCaseResult, PairedBenchmarkCaseResult

from .protocol import (
    ExecutionProfile,
    StripeBenchmarkManifest,
    build_paired_benchmark_case_result,
)

_SECRET_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
_REDACTED = "[REDACTED_SECRET]"


def _default_profile() -> ExecutionProfile:
    return ExecutionProfile(
        profile_id="headless-default",
        model_version="recorded",
        browser_runtime_version="protocol-only",
        provider_mode="recorded",
        headless=True,
    )


class StripeBenchmarkRunnerConfig(ExecutionProfile):
    """Validated, non-production configuration for one execution arm."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    profile_id: str = "headless-default"
    model_version: str = "recorded"
    browser_runtime_version: str = "protocol-only"
    arm: Arm = "G"
    benchmark_version: str = Field(default="stripe-payment.v1", min_length=1, max_length=128)
    corpus_version: str = Field(default="stripe.payment.testmode.v1", min_length=1, max_length=128)
    pack_id: str = Field(default="stripe.payment", min_length=1, max_length=256)
    pack_version: str = Field(default="0.1.0-draft.1", min_length=1, max_length=128)
    stripe_mode: str = "test"
    provider_mode: str = "recorded"
    production_eligible: bool = False
    enforce: bool = False
    secret_env_key: str | None = None

    @model_validator(mode="after")
    def validate_boundary(self) -> "StripeBenchmarkRunnerConfig":
        if self.stripe_mode != "test":
            raise ValueError("Stripe benchmark runner accepts test mode only")
        if self.production_eligible:
            raise ValueError("Stripe benchmark runner is not production eligible")
        if self.enforce:
            raise ValueError("benchmark runner composition cannot enforce")
        if self.provider_mode != "recorded":
            raise ValueError("benchmark runner composition is recorded-first")
        if self.secret_env_key is not None and not _SECRET_KEY_RE.fullmatch(self.secret_env_key):
            raise ValueError("secret_env_key must be an environment-variable key")
        return self

    @classmethod
    def headless_default(cls, **overrides: Any) -> "StripeBenchmarkRunnerConfig":
        return cls.model_validate(overrides, strict=True)


# Discoverable short alias for callers that do not need the Stripe prefix.
RunnerConfig = StripeBenchmarkRunnerConfig


@runtime_checkable
class EnvironmentSecretProvider(Protocol):
    """Read a secret by key; values never belong in config or artefacts."""

    def read(self, env_key: str) -> str: ...


class ProcessEnvironmentSecretProvider:
    def read(self, env_key: str) -> str:
        if not _SECRET_KEY_RE.fullmatch(env_key):
            raise ValueError("secret environment key is invalid")
        value = os.environ.get(env_key)
        if not value:
            raise ValueError(f"missing secret environment variable: {env_key}")
        return value


def inject_stripe_test_secret(
    config: StripeBenchmarkRunnerConfig,
    provider: EnvironmentSecretProvider | None = None,
) -> str | None:
    """Resolve an optional ``sk_test_*`` key at the process boundary only.

    The returned value is for immediate client construction by a future live
    adapter; this module never stores, serializes, logs, or submits it.
    """

    if config.secret_env_key is None:
        return None
    source = provider or ProcessEnvironmentSecretProvider()
    return validate_stripe_test_secret(source.read(config.secret_env_key))


def validate_stripe_test_secret(secret: str) -> str:
    """Validate an injected Stripe key without logging, storing, or returning metadata."""

    if not isinstance(secret, str) or not secret.startswith("sk_test_") or len(secret) <= len("sk_test_"):
        raise ValueError("Stripe benchmark accepts only sk_test_* credentials")
    return secret


def redact_secrets(value: Any, secrets: Sequence[str] = ()) -> Any:
    """Return a JSON-shaped deep copy with secret values removed."""

    known = tuple(secret for secret in secrets if secret)
    if isinstance(value, Mapping):
        return {str(key): redact_secrets(item, known) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_secrets(item, known) for item in value]
    if isinstance(value, str):
        redacted = value
        for secret in known:
            redacted = redacted.replace(secret, _REDACTED)
        return redacted
    return deepcopy(value)


@runtime_checkable
class ArtifactSink(Protocol):
    def put_artifact(self, artifact_id: str, payload: Mapping[str, Any]) -> str: ...


@runtime_checkable
class ResultSink(Protocol):
    def put_result(self, result: PairedBenchmarkCaseResult) -> str: ...


class InMemoryArtifactSink:
    """Small deterministic sink useful for offline tests and local composition."""

    def __init__(self, *, secrets: Sequence[str] = ()) -> None:
        self._secrets = tuple(secrets)
        self.artifacts: dict[str, dict[str, Any]] = {}

    def put_artifact(self, artifact_id: str, payload: Mapping[str, Any]) -> str:
        clean = redact_secrets(payload, self._secrets)
        json.dumps(clean, ensure_ascii=True, sort_keys=True)
        self.artifacts[artifact_id] = clean
        return artifact_id


class InMemoryResultSink:
    def __init__(self, *, secrets: Sequence[str] = ()) -> None:
        self._artifact_sink = InMemoryArtifactSink(secrets=secrets)

    @property
    def results(self) -> dict[str, dict[str, Any]]:
        return self._artifact_sink.artifacts

    def put_result(self, result: PairedBenchmarkCaseResult) -> str:
        artifact_id = f"{result.pair_id}:{result.case_id}:{result.arm}"
        return self._artifact_sink.put_artifact(artifact_id, result.model_dump(mode="json"))


@runtime_checkable
class BrowserBenchmarkRunner(Protocol):
    def run(self, manifest: StripeBenchmarkManifest) -> PairedBenchmarkCaseResult: ...


class StripeRecordedBenchmarkRunner:
    """One-arm recorded runner; no browser, network, or enforce path is wired."""

    def __init__(self, config: StripeBenchmarkRunnerConfig, *, result_sink: ResultSink | None = None) -> None:
        self.config = config
        self.result_sink = result_sink

    def record(
        self,
        manifest: StripeBenchmarkManifest,
        outcome: BenchmarkCaseResult | PairedBenchmarkCaseResult | Mapping[str, Any],
    ) -> PairedBenchmarkCaseResult:
        if manifest.profile.provider_mode != self.config.provider_mode:
            raise ValueError("manifest provider mode does not match runner config")
        if manifest.profile.headless != self.config.headless:
            raise ValueError("manifest headless profile does not match runner config")
        result = build_paired_benchmark_case_result(manifest, self.config.arm, outcome)
        if result.arm != self.config.arm:
            raise ValueError("recorded outcome crossed benchmark arm boundary")
        if self.result_sink is not None:
            self.result_sink.put_result(result)
        return result

    def run(self, manifest: StripeBenchmarkManifest) -> PairedBenchmarkCaseResult:
        raise NotImplementedError("recorded seam requires a supplied fixture outcome; no execution is performed")


def compose_stripe_benchmark_runner(
    config: StripeBenchmarkRunnerConfig | None = None,
    *,
    result_sink: ResultSink | None = None,
) -> StripeRecordedBenchmarkRunner:
    """Compose a safe recorded runner.  ``enforce`` and live Stripe are impossible."""

    resolved = config or StripeBenchmarkRunnerConfig.headless_default()
    return StripeRecordedBenchmarkRunner(resolved, result_sink=result_sink)


__all__ = [
    "ArtifactSink",
    "BrowserBenchmarkRunner",
    "EnvironmentSecretProvider",
    "InMemoryArtifactSink",
    "InMemoryResultSink",
    "ProcessEnvironmentSecretProvider",
    "ResultSink",
    "RunnerConfig",
    "StripeBenchmarkRunnerConfig",
    "StripeRecordedBenchmarkRunner",
    "compose_stripe_benchmark_runner",
    "inject_stripe_test_secret",
    "redact_secrets",
    "validate_stripe_test_secret",
]
