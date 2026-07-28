# Phase 2.2 execution-entry inventory

## Current Audit Observation

In `audit` mode, `ForgeAgent` observes the already parsed candidate Actions
before the unchanged execution loop. It records redacted candidates plus opaque
page, element, and screenshot fingerprints. This is evidence collection only:
it does not create a Contract or PolicyDecision, request a Permit, call a model,
or change the candidate Action list. The exact replay format and interface-only
boundary are recorded in `foundation-closure.md`.

## Enforce boundary

The future enforce path is the main Agent call to ActionHandler.handle_action.
It requires an issued, matching, one-time ExecutionPermit represented by an
`ExecutionAuthorization`, plus an `ExecutionProfile` selected by the trusted
governance path.

`ActionHandler.handle_action` rejects every call without
`ExecutionAuthorization` whenever enforce is active. Authorization and profile
must be supplied together even when the mode is forced in a test. This
fail-closed guard also covers CUA and cached actions that reach the public
handler. Their family-specific evidence and freshness contracts are now sealed,
but no live caller can supply the dormant governed context.

## Known bypasses and execution entry points

All six tracked families now have a machine-checked future disposition and a
named closure regression. This per-family seal is not production readiness:
current callers can still reach Phase 1 browser behavior in `off/audit`, no live
governance path issues and passes an `ExecutionAuthorization`, and configuration
still rejects `enforce`.

| Path | Current behavior | Current enforce disposition | Required Task 5 disposition |
|---|---|---|---|
| Main Agent | `ForgeAgent` calls `ActionHandler.handle_action` for normal, extract, and verification actions. | Public handler has the future authorization boundary. | First permit-sealed target. |
| ActionHandler-internal locator/coordinate fallback | Typed action handlers can use Playwright locators, labels, coordinates, and JavaScript after entering the public handler. | **Sealed at the public handler boundary.** A validated profile defines the weakest permitted fallback; profile policy is checked before Permit consumption, and a durable Attempt enters `EXECUTING` before the browser call. | Retain profile-context and ordering regressions. |
| `SkyvernPage` Page method proxies | `click`, input, upload, select, navigation, and inherited `Page` methods can directly proxy to Playwright. Its `__getattribute__` delegation exposes additional underlying `Page` methods. | **Sealed for governed scripts.** Shared script rejection runs before user code and before a `ScriptSkyvernPage` can be created; direct embedding callers remain tracked separately. | Retain shared rejection; do not add one guard per Page proxy. |
| `ScriptSkyvernPage` overrides | The script adapter records calls but can call concrete handlers directly, including `complete`; other inherited `SkyvernPage` proxy methods remain available. | **Sealed for governed scripts.** Constructor, `create`, and `create_scraped_page` reject enforce mode before browser access. | Retain the three adapter-boundary checks and their ordering regression. |
| `RealSkyvernPageAi` helpers | AI helpers call concrete handlers or direct locators. They currently call `assert_script_execution_is_not_governed()`. | Local guard exists, but it covers only these helpers. It is defense in depth, not evidence that script execution is sealed. | Retain the local guard after the shared entry-point rejection exists. |
| Script service and direct script launchers | `execute_script -> run_script` imports and invokes user code; the HTTP script route, background executor, CLI, and workflow script loading are callers. `ScriptSkyvernPage.create` is the browser-proxy construction path. | **Sealed for governed scripts.** Both service functions reject enforce mode before database/script loading or user-code import. All known launchers converge on one of these guarded functions. | Retain the shared helper and caller-convergence inventory. |
| CUA/UI-TARS | Engine-specific responses become typed coordinate/key/text actions before the main execution loop. | **Sealed at the dormant governed handler boundary.** CUA requires exact, fresh engine evidence bound to the Action and observation at Permit issuance and again before Attempt authorization/Permit consumption. | Retain freshness, substitution, profile, and authorization regressions. |
| Cached/speculative actions | Cached plans and speculative LLM responses can supply a prior `ScrapedPage` or action response to the main loop. | **Sealed for future enforce.** Speculative state is discarded, speculative generation is disabled, and legacy cached Actions return empty before database retrieval, forcing the normal fresh perception and policy path. | Retain the shared mode guard and ordering regression. |
| SDK/direct integrations | Generated sync/async script clients, API, workflow, CLI, embedding callers, and the `/sdk/actions` route can invoke script or AI page actions without traversing the normal UI route. | **Sealed by route-or-reject proof.** Known script clients reach the HTTP rejection/shared guarded service; `/sdk/actions` reaches the guarded `create_scraped_page` boundary before an AI page adapter is created. | Retain the complete caller-convergence inventory and cross-layer regression. |

### Machine-checked pre-enforce closure contract

The fixture `tests/fixtures/execution_entrypoint_inventory.json` is the
canonical closure contract for these six independently tracked families.  The
table below mirrors the machine-checked identifiers.  It records the required
future disposition and current closure state. All six rows are `sealed` and
`enforce_eligible=true`. Per-family eligibility does not enable global
`GOVERNANCE_MODE=enforce` or satisfy the business/runtime gates.

| Matrix ID | Status | Required disposition | Guard owner | Closure regression |
|---|---|---|---|---|
| `handler_locator_coordinate_javascript` | `sealed` | `route_through_public_handler` | `action_handler_public_boundary` | `test_handler_mechanisms_require_profile_authorization_and_attempt` |
| `skyvern_page_script_proxy` | `sealed` | `reject_governed_script` | `shared_governed_script_rejection` | `test_governed_script_page_creation_is_rejected` |
| `shared_script_launchers` | `sealed` | `reject_governed_script` | `shared_governed_script_rejection` | `test_all_governed_script_launchers_share_rejection` |
| `cua_ui_tars` | `sealed` | `route_through_public_handler` | `cua_engine_adapter_and_action_handler_boundary` | `test_cua_requires_fresh_evidence_profile_and_authorization` |
| `cached_speculative_actions` | `sealed` | `fresh_reobserve` | `forge_agent_observation_coordinator` | `test_cached_actions_require_fresh_observation_and_authorization` |
| `sdk_direct_script_clients` | `sealed` | `inventory_only` | `sdk_and_script_caller_inventory` | `test_direct_clients_reach_handler_or_shared_script_rejection` |

The control sets are intentionally stricter than source reachability:

- locator, coordinate, and JavaScript mechanisms require an ExecutionProfile,
  matching authorization, and durable ExecutionAttempt boundary;
- governed script proxies and launchers must share one rejection before either
  adapter construction or user-code invocation;
- CUA/UI-TARS requires fresh observation, engine evidence, ExecutionProfile,
  and authorization;
- cached/speculative actions require fresh observation and policy evaluation;
- SDK/direct clients remain inventory-only in behavior, while the closure test
  proves every known caller reaches either a fixed HTTP rejection or the shared
  script rejection before browser access.

### Task 5 handler-mechanism sealing

The dormant governed branch of `ActionHandler.handle_action(...)` now treats
`ExecutionAuthorization` and `ExecutionProfile` as one required context. The
authorization carries the deterministic `ExecutionEffect`; Permit issuance
persists that effect and the complete profile in its existing decision payload.
The handler verifies the current Action and page binding, evaluates the profile,
and only then asks the Attempt service to compare the supplied context with the
persisted Permit context before consumption. A mismatch cannot consume the
Permit. The handler then creates an `AUTHORIZED` Attempt and commits `EXECUTING`
before entering browser code.

The validated profile is bound to the current asynchronous attempt. Its
mechanism is the weakest fallback the attempt may reach in the existing chain:

```text
locator -> label -> coordinate -> JavaScript
```

A label profile may use locator and label but cannot reach coordinate. A
JavaScript profile may traverse the full chain. CUA coordinate is deliberately
independent. `fallback_rank` is fixed to this order, so mechanism and audit rank
cannot disagree. Coordinate, JavaScript, and CUA profiles remain ineligible for an
`external_write`, so a weak fallback cannot silently cross an external commit
boundary. Handler success or exception still becomes `UNKNOWN` pending a
business result probe.

This per-family seal does not connect ForgeAgent, issue a live Permit, or enable
enforce. Normal `off` and `audit` callers provide neither object and retain the
Phase 1 behavior.

### Task 5 CUA, reuse, and direct-client sealing

`CUAExecutionEvidence` binds the engine identity, Action fingerprint,
observation hash, evidence references, and capture time. Permit issuance stores
the exact evidence; Attempt authorization rejects missing, stale, detached, or
substituted evidence before consuming the Permit. A non-CUA profile cannot use
CUA evidence. The current ForgeAgent still supplies no Permit or governed
profile, so this does not authorize current CUA/UI-TARS execution.

The shared `precomputed_action_reuse_is_allowed()` guard preserves existing
`off/audit` behavior. In future enforce, ForgeAgent discards a popped
`SpeculativePlan`, builds a new recorded step prompt from a fresh scrape, and
does not generate the next speculative plan. The legacy cached-action path
returns no Actions before querying stored plans. The resulting fresh candidate
still needs the ordinary policy decision and handler authorization.

Generated script clients converge on the script HTTP route; its current fixed
rejection precedes its executor call. The background executor converges on the
guarded script service. The direct `/sdk/actions` path calls guarded
`ScriptSkyvernPage.create_scraped_page` before constructing
`RealSkyvernPageAi`. No additional browser implementation was introduced.

### Task 5 decision: reject governed scripts at shared entry points

The implemented solution does **not** add an enforce guard to each
`SkyvernPage.click`/fill/complete proxy. Instead, Task 5 rejects governed
or enforce-mode script execution as a whole at the shared script-run
wrapper/service entrance and at `ScriptSkyvernPage` initialization or
creation. The minimal currently known locations are:

```text
script_service.execute_script(...) -> script_service.run_script(...)
                                   -> user run_workflow(...)
ScriptSkyvernPage.create(...) / ScriptSkyvernPage.__init__(...)
```

This affects direct script execution, background execution, CLI execution,
and workflow script loading. The guard must use one shared helper so a caller
cannot reach user code before the rejection. The service functions call it
before database/script loading and user-code import; the adapter calls it before
construction or browser-state acquisition. `off` and `audit` behavior is
unchanged. `RealSkyvernPageAi`'s existing local guard remains defense in depth.

Every row is sealed and regression-tested, but `GOVERNANCE_MODE=enforce` remains rejected at configuration load because entry sealing is only one prerequisite. It cannot silently fall back to the Phase 1 execution path. Use `audit` for evidence collection; enable enforce only after the remaining business and runtime gates are implemented, approved, and tested.

## Required sealing gates before enforce can be enabled

1. One state-changing browser action per observation, followed by re-observation.
2. Permit validation and an `ExecutionAttempt` transition at the public handler boundary.
3. Recovery for `EXECUTING -> CONFIRMED | UNKNOWN | FAILED`; an interruption must never replay an unknown commit.
4. Every listed bypass is either routed through the same boundary or disabled for governed tasks.
5. Governed scripts are rejected at their shared service/adapter entry points;
   individual `SkyvernPage` proxies are not treated as sealed merely because a
   subset of `RealSkyvernPageAi` helpers has a local guard.

## Execution-attempt recovery contract

The persistence service now defines the required lifecycle for the future public-handler integration:

```text
issued permit
  -> consume + persist AUTHORIZED attempt
  -> persist EXECUTING immediately before Playwright
  -> CONFIRMED | FAILED | UNKNOWN
```

`UNKNOWN` and any prior `(task_id, idempotency_key)` are recovery-only states: the next worker must perform a business result probe, never replay the browser action automatically. The handler integration must commit each boundary state before continuing to the next side effect.

## Current public-handler contract

`ActionHandler.handle_action(..., execution_authorization=...)` now exposes the controlled path. The authorization contains a permit reference, action fingerprint, observation hash, and idempotency key. Before Playwright is reached, the handler independently rebuilds the HMAC bindings from the supplied typed action and the current `ScrapedPage`; any action or page drift is rejected.

The standard handler does not yet have a business-specific result probe, so it records a completed browser call as `UNKNOWN` rather than treating transport success as a confirmed business commit. A recovery worker must later resolve it with evidence through `resolve_unknown_execution_attempt`. Existing callers omit `execution_authorization` and retain Phase 1 behavior while `GOVERNANCE_MODE=enforce` remains unavailable.

## One-observation commit rule

`build_governance_batch_plan(...)` analyzes all typed actions proposed from one `ScrapedPage` and gives them one shared HMAC-bound observation. The plan rejects two `EXTERNAL_WRITE` candidates before any permit can be issued. This rule applies to business commits such as payment and deletion; normal navigation and form preparation still require their own action-level policy, but do not authorize an external commit from a stale snapshot.

## Interface-Only Identity And Separation Of Duties

When a `TaskExtension` exists, `TaskContract` snapshots its requester, department, business line, and risk context rather than relying on a request-scoped `TenantContext` during recovery. Approval requests persist the requester identity, and the transactional decision service rejects requester self-approval. Deterministic policy also denies an expired task contract and any operation outside a non-empty `allowed_operations` set.

These are persistence and policy interfaces, not inputs to the current audit
observer. Audit mode does not create a TaskContract or render this policy
decision; only a future authorized orchestration path may consume them.

## Interface-Only Approval Pause And Safe Resume Contract

The approval-pause orchestrator persists `PendingAction`, `ApprovalRequest`, their binding, and the native `Task/Step -> PENDING_APPROVAL` transition in one caller-owned database transaction. A decision changes only the persisted approval and pending-action states. After approval, `begin_reobservation_after_approval(...)` first invalidates the old action payload and only then changes native state to `RESUMING`.

`RESUMING` is intentionally not browser execution: a recovery scheduler must acquire the task, re-scrape the page, build a new governance plan, and issue a new permit. Redis publication is post-commit notification only and is never the recovery source of truth.

`prepare_approved_pauses_for_reobservation(...)` is the database recovery scan. It locks approved pending actions with `SKIP LOCKED`, invalidates the old action, and records `RESUMING` before any scheduler handoff. A scheduler crash after this commit is safe: a later scan can discover the native `RESUMING` task and continue from a fresh page observation rather than replaying the stored payload.

## Recovery scheduler

`ENABLE_GOVERNANCE_RECOVERY_SCHEDULER=false` is the safe default. When explicitly enabled, the FastAPI lifespan starts the recovery scheduler, which performs this durable sequence:

```text
APPROVED pending action
-> transaction: invalidate old action + Task/Step RESUMING
-> commit
-> claim Task RUNNING while Step remains RESUMING
-> ForgeAgent.execute_step(existing step)
-> Step RUNNING -> fresh scrape -> new action plan
```

The scheduler always performs the durable transition to `RESUMING`, but it only starts `ForgeAgent.execute_step` when `ENABLE_GOVERNANCE_RECOVERY_EXECUTION=true`. That second switch requires sealed `enforce` mode and is therefore unavailable in the current release. This prevents a recovered task from reaching known script-generation bypasses before their permit boundary is sealed.

If a worker dies between claim and Agent startup, `Task=RUNNING, Step=RESUMING` is a durable pre-browser handoff and is eligible for another claim. Once the Agent changes the step to `RUNNING`, ordinary execution-attempt recovery controls take over. The scheduler accepts no stored action payload and has no path that can replay it.

## Migration and rollout

Skyvern core and enterprise migrations have separate Alembic heads. Deploy with `alembic upgrade heads`, then keep `GOVERNANCE_MODE=off` or `audit`. `enforce` and recovery execution remain configuration-rejected until the complete Governor-to-permit issuance path is enabled and every inventory row has an equivalent regression test.
