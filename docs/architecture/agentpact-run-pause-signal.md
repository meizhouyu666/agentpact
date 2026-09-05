# AgentPact RunPauseSignal

`enterprise.agent_runs.pause_signal.RunPauseSignal` is the platform-neutral
contract for a run that pauses without becoming a terminal failure. It carries
the stable `reason_code`, run/task/step/checkpoint references, optional canonical
`InputRequest`, already-redacted prompt metadata, allowed platform actions, a
resume policy, external-effect state, and an optional expiry timestamp.

`AWAITING_INPUT` is strictly pre-effect and uses input-submission recovery. Once
an external effect may have started, the signal must not offer input recovery or
replay; use `NEEDS_HUMAN` for takeover, ambiguity, or result-probe handling.
`InputRequest` status remains diagnostic and never promotes observed values into
canonical inputs. The model forbids extra fields, so adapter/vendor payloads
stay outside the AgentPact boundary.

This is an additive contract only: it does not alter BrowserLoop state,
AgentRunService execution, persistence, migrations, or domain fixtures.

## Stripe explicit composition

`enterprise.domains.stripe_payment.m10_runtime` exposes the adapter-local
`missing_stripe_inputs` and `build_stripe_input_pause_signal` helpers. An
explicit caller that accepts a partially filled `business_inputs` mapping may
run this preflight before constructing the typed `PackRunRequest`; missing
`payment_intent_id` or `amount_minor` produces a redacted `AWAITING_INPUT`
signal with Pack semantic slots and a stable checkpoint identity. The generic
`PackRuntimeAdapter` protocol is intentionally unchanged because its typed
request currently carries a dictionary and has no pause return channel.

The hosted live browser path is not rewired to replay input: it currently
hard-codes Stripe test card data and crosses the external-effect boundary
through Permit/Attempt. A future live UI integration must collect any needed
semantic values before that boundary; after it, outcomes remain `UNKNOWN` and
are resolved only by the authoritative Stripe probe.
