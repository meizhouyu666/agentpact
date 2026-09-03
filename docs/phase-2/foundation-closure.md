# Phase 2 Foundation Closure

## Current calibration (`d835bb5`)

This closure document describes the legacy Skyvern/ForgeAgent audit and offline
foundation boundary. It does not retract the AgentPact-owned browser loop,
`PlaywrightBrowserSessionFactory`, or `PersistedBrowserExecutor` used by
explicitly composed callers such as the Stripe test-mode adapter. The formal
application still does not mount a fully composed `AgentRunService`, and the
Synthetic Agent Run composition remains test-fixture-only.

## Current Runtime Boundary

The only Skyvern-connected Phase 2 runtime write in this legacy foundation
occurs when
`GOVERNANCE_MODE=audit`. After Skyvern has already produced typed Action
candidates, `ForgeAgent` records one
`action_candidate` event per candidate and then continues through the unchanged
Phase 1 execution loop.

The event contains only:

- a redacted candidate Action payload;
- an HMAC-bound page observation reference;
- an optional element fingerprint and screenshot fingerprints.
- redacted local egress-shadow findings for the already-observed DOM, existing
  Action-extraction prompt, and screenshot presence.

It does not persist raw HTML, screenshots, or element content. It does not
create a TaskContract, ActionIntent, PolicyDecision, Permit, PendingAction, or
ExecutionAttempt. Audit code makes no model call and never calls a governor or
policy evaluator. Audit-write failures are logged and do not alter the Action
list or ActionHandler call. The egress shadow is not a live model-egress policy
decision: it does not inspect a future endpoint configuration and never blocks,
redacts, or changes Skyvern's existing request. It scans the existing
extract-action prompt for normal engines and the existing navigation-goal text
used by CUA/UI-TARS engines; screenshots remain a separately redacted binary
finding. It does not manufacture a prompt or send another request.

The read-only completeness report accepts a count from audit-write log
aggregation, so a failed write lowers replay completeness rather than appearing
complete. It does not retry execution or create a policy outcome.

Separately, the isolated synthetic application may persist one audit-only
governed Task-admission aggregate plus a redacted pending outbox row when an
allowlisted SQLAlchemy repository is explicitly injected. The default endpoint
returns unavailable without that repository. The aggregate is not a Skyvern
`tasks` row, the outbox has no publisher, and admission does not call a browser,
the synthetic payment transition, or any Skyvern Task creator.

`GOVERNANCE_MODE=enforce` remains configuration-rejected. Approval recovery
execution remains disabled and is not started or exercised by this closure.
The future enforce branch now rejects script execution at both shared service
entries and before `ScriptSkyvernPage` construction/browser-state acquisition.
This seals the script-proxy and shared-launcher families without changing
`off` or `audit`. The dormant governed ActionHandler branch also seals its
internal locator/label/coordinate/JavaScript family. CUA evidence binding,
enforce-only cached/speculative reuse rejection, and direct-client convergence
proof now seal the other three tracked execution-entry families.

The governed ActionHandler branch requires matching authorization and profile.
Permit issuance stores the deterministic effect and profile in the existing
decision payload; Attempt authorization rejects a substituted profile or
downgraded effect without consuming the Permit. The handler checks policy,
persists `AUTHORIZED` and `EXECUTING` before browser access, and restricts
internal fallbacks to the profile's maximum mechanism. It has no live caller
because ForgeAgent remains unconnected and enforce remains configuration-
rejected.

CUA engine evidence is bound to the engine, Action fingerprint, observation,
opaque evidence references, and a 30-second freshness window. Permit issuance
persists that evidence, and Attempt authorization revalidates exact equality
and freshness before Permit consumption. Future enforce also discards popped
speculative state, disables speculative generation, and returns no legacy
cached Actions before database retrieval. Generated/direct clients have a
cross-layer route-or-reject regression covering the script HTTP route, shared
service guard, and `/sdk/actions` adapter guard. These are dormant or
enforce-only controls for the legacy path; no generic production caller or
production Pack was connected. The explicit Stripe test-mode composition is
documented above and uses its own AgentPact-owned persisted boundary.

## PendingAction Data Closure

PendingAction is not connected to the browser execution path. Its model and
`ent_006` migration now agree that a Task/Step may retain any number of
terminal approval rounds, while exactly one `pending` or `approved` round may
exist concurrently. The regression flow covers approval, old-action
invalidation for re-observation, a fresh second round, and a competing pause
request. No scheduler or browser action participates in those tests.

## Interface-Only Foundations

The following are contracts and offline tests only. They are not runtime
dependencies of the audit observer or the browser execution path:

- CapabilityResolver and CapabilityGrant;
- BusinessPlan and ExecutionWorkOrder;
- TrustedTaskCreationSnapshot and versioned interface serialization;
- observation-evidence policy (ExecutionProfile remains dormant on the legacy
  ActionHandler branch; AgentPact-owned composed runtimes consume it at their
  explicit boundary);
- L0--L4 recovery decisions;
- permit, execution-attempt, approval-pause, and result-probe models outside
  explicit composed runtimes such as Stripe test mode.

The synthetic policy analyzer remains an offline regression oracle. Its example
operations and outcomes are not emitted by the runtime audit event and do not
define a production Domain Pack or authorization rule.

The explicit `stripe.payment` hosted Checkout adapter is a separate test-mode
composition: it uses AgentPact's browser loop and persisted Permit/Attempt
boundary, leaves uncertain writes in `UNKNOWN`, and resolves them through the
independent Stripe PaymentIntent Probe. It is not a production Pack and is not
mounted by formal application startup.

CapabilityGrant expiry is now deterministic at the interface boundary: every
grant has a positive resolver TTL, is active only before its exclusive
expires_at timestamp, and must be checked with an explicit trusted now value
when a future Planner validates a plan or Work Order. This does not connect a
grant to Skyvern.

Native, workflow, and template Task creation each have a required trusted
provenance shape. The unconnected persistence helper accepts only that snapshot
and audit mode; neither audit observation nor page evidence can create a Task
contract. Versioned envelopes exist for Contract, Grant, BusinessPlan,
WorkOrder, Observation, and RecoveryDecision. Their invalidation keys are pure
data-contract checks and carry no execution authority.

## Replay And Fixtures

`phase2-audit-candidate-v1` is the replay payload format stored in the audit
event `payload` column:

```json
{
  "schema_version": "phase2-audit-candidate-v1",
  "candidate_action": {"action_type": "click", "element_id": "..."},
  "evidence_refs": {
    "observation_hash": "hmac...",
    "element_id": "...",
    "element_fingerprint": "hmac...",
    "screenshot_fingerprints": ["hmac..."]
  }
}
```

The audit-reporting module reads only stored action-candidate audit rows and
validates the versioned payload for replay. Invalid historical payloads do not
fail the page: they are skipped and reported only by event ID. Its aggregate
reports replayable payloads, invalid payloads, distinct opaque observations,
and externally counted write failures; they contain no business intent, risk,
policy, or execution decision.

The audit-observation fixture covers SPA, iframe, overlay, post-submit timeout,
script-generation-shaped, CUA coordinate, cached-plan, locator/coordinate
fallback, page-drift, and multiple-candidate observations. The fixture
assertions cover only candidate capture, prompt/DOM field redaction, and opaque
evidence references. The governance-fault replay fixture is a pure-function failure
corpus; replay never invokes a browser or retry worker.

## Migration Rehearsal

`tests/unit/test_phase2_migration_rehearsal.py` checks the additive Phase 2
migration order through `ent_007`, including the active PendingAction partial
unique index and the governed admission/outbox tables. Before the index is
created, `ent_006` runs a read-only duplicate-active-row preflight and aborts
rather than modifying approval history. See `pending-action-migration-runbook.md`
for the operator procedure.

Two explicitly authorized disposable PostgreSQL 14.23 rehearsals were also
completed. The first applied both heads through `ent_006` and verified the
partial unique index with no duplicate active PendingActions. The second applied
`ent_007` and proved concurrent duplicate collapse, semantic-conflict rejection,
one admission plus one redacted pending outbox row, and zero Skyvern Task rows.
Both PostgreSQL processes are stopped. These rehearsals do not authorize a
production Domain Pack, outbox publication, runtime admission wiring, or
`GOVERNANCE_MODE=enforce`.
