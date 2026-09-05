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
