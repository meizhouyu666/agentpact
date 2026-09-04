# AgentPact Phase 2 Master Status

> Status: authoritative working status
> Updated: 2026-09-04 (`P1-3 Stripe explicit composition`)
> Scope: Phase 2 governance foundation, audit hardening, and the controlled path to future enforce

This document is the single operational entry point for Phase 2 work. Read it
before changing Phase 2 code or documentation. It resolves the distinction
between architecture direction, interface-only foundations, live runtime
behavior, and work that needs a separate approval.

## 1. Current Runtime Boundary

The current code has two deliberately separate execution surfaces:

- `enterprise/browser_loop` owns the browser operation loop, direct Playwright
  page runtime, `PlaywrightBrowserSessionFactory`, and the persisted
  `Permit`/`Attempt` executor. `SkyvernScraperRuntimeAdapter` is a temporary
  compatibility adapter for scraper observation only; it does not make Skyvern
  the owner of AgentPact browser execution.
- `stripe.payment` has an explicit governed test-mode hosted Checkout adapter.
  With an injected `StripeHostedCheckoutFlow`, durable session factory, and
  `sk_test_*` credentials, the hosted submit consumes a one-time
  `ExecutionPermit`, persists an `ExecutionAttempt` before the browser call,
  leaves uncertain outcomes in `UNKNOWN`, and resolves them only through the
  independent PaymentIntent Probe. The adapter and its Permit/Attempt/UNKNOWN/
  probe regression tests are implemented, but this is still a test-mode
  candidate, not a production Pack.

The Pack-owned `compose_stripe_agent_run_service` composition root now binds
the Stripe adapter to the generic `AgentRunService` for explicit recorded or
live use. Live construction requires `sk_test_*` and an explicitly injected
hosted flow. It does not populate the formal application's default registry,
change `production_eligible=false`, or enable `enforce`.

Generic `AgentRunService`, routes, and Pack runtime contracts are available as
composable interfaces. Formal `skyvern/forge/api_app.py` startup now mounts an
app-scoped generic Agent Run service through the formal composition root. The
default registry is empty, so no Pack is executable until a caller explicitly
injects a validated registry, target URL, and adapter. The Synthetic Agent Run
composition exists only in test fixtures. `GOVERNANCE_MODE=enforce` remains
configuration-rejected, and no generic ForgeAgent-to-AgentPact production path
is enabled.

### Legacy Phase 2 audit boundary

The legacy Skyvern-connected Phase 2 path remains audit observation:

```text
Skyvern Task / Step
-> page perception and typed Action candidates
-> audit-only candidate event
-> unchanged Phase 1 ActionHandler execution
```

`GOVERNANCE_MODE=enforce` is configuration-rejected. No current work may make
it available, silently degrade it to Phase 1 behavior, or add a tenant,
workflow, or environment bypass.

The following production-facing actions require an explicit future approval and
remain forbidden:

- connecting generic CapabilityGrant, BusinessPlan, or ExecutionWorkOrder to
  ForgeAgent;
- enabling global `enforce`, approval-recovery execution, or a browser retry
  driven by the legacy Phase 2 path;
- installing or declaring a production Pack without adopter-owned facts,
  credentials, probes, controls, and scoped approval;
- treating a synthetic policy outcome as a live authorization decision.

Legacy Skyvern remains the executor for its existing product paths. AgentPact's
owned browser loop is the explicit execution boundary for composed callers, and
constrained planning contracts and domain logic must not create another browser
automation loop.

The isolated synthetic application may also persist an audit-only admission
aggregate and redacted pending outbox when an allowlisted repository is
explicitly injected. It does not insert a Skyvern `tasks` row, publish work, or
call a browser/business transition.

## 2. Current State

| Area | State | Meaning |
|---|---|---|
| Audit candidate capture | Active, audit-only | Persists redacted typed Action candidates and opaque evidence references after parsing, without changing execution. |
| Audit policy analyzer | Offline only | Synthetic regression oracle; it does not decide live Actions. |
| Capability / Grant | Interface only | Independent authorization dimensions and business/workload/tenant/revocation bindings are deterministic, but no runtime Planner input or authorization path uses them. |
| CapabilityRequest / Grant projection | Interface only | UI/chat share one strict request schema and disclosure-safe active projection; neither has a live route or Planner caller. |
| BusinessPlan / WorkOrder | Interface only | Request/input/resource/version binding and per-step Work Order validation exist, but no ForgeAgent adapter is connected. |
| Governed Task admission | Synthetic persistence, audit-only | `ent_007` atomically stores an allowlisted non-runnable aggregate plus redacted pending outbox; only the injected synthetic API calls it and no Skyvern Task is published. |
| L0--L4 recovery | Interface only | Classification and replay fixtures exist; Skyvern keeps its existing recovery behavior. |
| Legacy observation / fallback policy | Dormant handler integration | Legacy Skyvern observation evidence remains audit-only; ExecutionProfile constrains the future governed ActionHandler branch without changing normal `off/audit` execution. AgentPact-owned runtime observation is tracked separately below. |
| Execution-entry sealing | 6 of 6 known families sealed | Handler/CUA mechanisms require profile, authorization, fresh evidence, and Attempt ordering; governed scripts reject enforce; cached/speculative state is not reused in enforce; SDK/direct callers have route-or-reject proof. |
| Governed dry-run | Offline only | Validates Contract, Grant, BusinessPlan, WorkOrder, Domain-Pack business binding, observation, and typed Action policy into redacted evidence; it has no executor callback and cannot issue a Permit. |
| Domain Pack Contract Catalog | Offline reference only | Holds immutable SDK contracts outside the active `DomainPackRegistry`; no external or domain-specific contract is installed, discoverable, or executable. |
| Pack SDK and static Conformance Kit | Offline complete | M2 provides immutable SDK authoring shapes, deterministic versioned reports, invalid-Pack fixtures, complete fact/evidence binding checks, and static runtime-import exclusion. It has no runtime caller. |
| Synthetic browser audit collector | Active, audit-only | Reads the isolated synthetic page's semantic DOM and screenshot, then emits a redacted test manifest; it never executes an Action. |
| PendingAction / approval state | Persistence foundation | Models, state services, and round-history tests exist; no browser execution path invokes them. |
| Permit / Attempt / UNKNOWN | Active in explicit Stripe composition; dormant for generic legacy paths | `PersistedBrowserExecutor` persists Permit/Attempt ordering, leaves external writes `UNKNOWN`, and exposes the exact checkpoint for an independent probe. Generic ForgeAgent and the formal app's empty default registry do not call this path. |
| M4 governed browser proof | Accepted test evidence | One process-isolated synthetic ActionHandler path proves durable `EXECUTING -> UNKNOWN -> CONFIRMED`, independent probe confirmation, one commit, no replay, and complete cleanup. It is not runtime wiring. |
| M5 developer/release experience | Implemented for review; now DONE/PASS locally | One cross-platform Python CLI, canonical release-report schema, Ubuntu CI, manual/release Windows smoke, public guide, and attribution are independently accepted at HEAD `d1c2587b2b03ae107429e1cd131dd5bc5082c390`; final review SHA-256 `d73196352e8f06512d69fc92a86127f19e370c11ffaf31f67183a39c55153707`. Publication has not occurred. |
| AgentPact browser operation loop | Active runtime (explicit composition) | AgentPact-owned loop, direct Playwright runtime, session factory, and persisted executor; `SkyvernScraperRuntimeAdapter` remains a temporary scraper compatibility layer. |
| Stripe test-mode hosted Checkout | Active runtime (explicit governed test-mode composition) | `live_browser.py` can create a real Stripe test Checkout Session, drive `checkout.stripe.com` with the 4242 test card, persist Permit/Attempt and `UNKNOWN`, and resolve through the independent PaymentIntent Probe. It requires injected durable composition and `sk_test_*`, is not a production Pack, and is never part of default tests. |
| Enforce | Disabled | Must remain disabled until all future gates in Section 7 are met. |

### Milestone state matrix

The following labels describe implementation state, not product readiness:

| Milestone | State | Boundary |
|---|---|---|
| M1 Contract boundary | Offline | Source-free catalog and validation only |
| M2 Pack SDK | Offline | Type and conformance contracts only |
| M3 Reference Pack | Offline | `synthetic.payment` recorded reference only |
| M4 Governed browser proof | Offline | Loopback Chromium evidence, not live Stripe |
| M5 Developer experience | Offline | Local/release checks and reports |
| M6 Constrained runtime | Interface-only | Trusted compilation and bindings; no generic production caller |
| M7 Native Agent loop | Interface-only | Native execution contracts remain isolated |
| M8 Sequential Agent loop | Interface-only | Journal/recovery contracts; no automatic UNKNOWN replay |
| M9 Model-safe Planner | Offline | Deterministic proposal validation and fixtures |
| M10 Agent Run | Mounted, fail-closed at formal app boundary | The app-scoped generic service is mounted with an empty default registry. Synthetic composition remains test-only; Stripe remains an explicit Pack composition and is not installed by formal startup. |
| M11 Operations workbench | Interface-only | Safe projections and provider composition contracts |
| M12 Evaluation and traces | Offline | Recorded evaluation and redacted, non-authoritative traces |

## 3. Completed Foundation Work

- Added additive governance migrations through `ent_007`.
- Added one-active-approval semantics: a Task/Step can retain terminal approval
  history while only one `pending` or `approved` action may be active.
- Added repeated approval-round and competing-pause regression coverage.
- Added redacted audit candidate storage and replay payload version
  `phase2-audit-candidate-v1`.
- Added synthetic Action, observation, fallback, and L0--L4 fault fixtures.
- Added explicit inventory of browser side-effect entry points and unsealed
  paths, with a machine-checked future disposition, guard owner, control set,
  closure regression, and enforce eligibility for each family.
- Sealed the two governed-script families with one shared enforce-mode
  rejection at service and adapter boundaries; `off` and `audit` behavior is
  unchanged.
- Sealed the dormant ActionHandler mechanism family with Permit-bound
  effect/profile validation, durable Attempt ordering, and bounded internal
  fallback context; ForgeAgent remains unconnected.
- Added a versioned governed dry-run report that proves the offline governance
  contracts compose without introducing a browser execution path.
- Added independent RBAC-to-Capability dimensions, one strict UI/chat
  `CapabilityRequest`, a disclosure-safe Grant projection, deterministic Plan
  validation, and an audit-only atomic governed Task-admission protocol.
- Added `ent_007`, a tenant/capability-allowlisted SQLAlchemy admission
  repository, a redacted pending outbox, and one injected synthetic API caller;
  the persisted aggregate is not a runnable Skyvern Task.
- Kept `enforce` configuration-rejected and approval recovery execution disabled.

These achievements are foundations, not evidence that enforce is implemented.

## 4. Completed Work Packages: Audit Hardening and Pre-Enforce Closure

The following work-package narrative records the earlier legacy Phase 2
audit/offline closure. Its statements about the absence of runtime callers apply
to the legacy Skyvern/ForgeAgent path; they do not retract the AgentPact-owned
browser loop or the explicitly composed Stripe test-mode adapter described in
Section 1 and the current-state matrix above.

For the legacy Phase 2 workstream, this remains the only approved direction
before a production business boundary is selected. It may improve evidence
quality, observability, testability, and data safety, but may not alter the
legacy Action selection or policy outcomes. Explicitly composed test-mode
adapters are tracked in the current-status sections above.

### P0: Finish Audit Safety and Persistence Closure

1. Completed: runtime element evidence references are re-keyed with
   `GOVERNANCE_AUDIT_HMAC_SECRET` before persistence. Skyvern's unkeyed
   `id_to_element_hash` is used only as HMAC input.
2. Completed: regression coverage proves a runtime-supplied element fingerprint
   cannot bypass HMAC re-keying and is bound to its page observation.
3. Completed: `ent_006` performs a read-only duplicate-active PendingAction
   preflight before creating its PostgreSQL partial unique index. The manual,
   non-destructive resolution procedure is in `pending-action-migration-runbook.md`.
4. Completed: ran `alembic upgrade heads` against a disposable PostgreSQL
   14.23 instance at `127.0.0.1:15432`. Both `a86c9fdba6b3` and `ent_006` are
   current; the duplicate-active PendingAction query returned no rows, and
   PostgreSQL created the conditional unique index
   `uq_pending_action_active_step`. No approval history was modified.

### P1: Make Audit Evidence Usable (Completed After Review Remediation)

1. Completed: define a read-only audit query and replay/report contract for stored
   `phase2-audit-candidate-v1` events.
2. Completed: emit aggregate audit completeness metrics without creating business intent,
   policy, or risk decisions.
3. Completed: extend synthetic audit fixtures for script generation, CUA/UI-TARS,
   speculative/cached plans, locator/coordinate fallback, page drift, and
   multi-Action observations.
4. Completed: the egress shadow scan receives the existing engine-appropriate
   text input, scans screenshots and DOM fields/attributes, and records only
   redacted findings. It does not block or change the existing Skyvern model
   request.

P1 review remediation is complete: replay skips invalid historical payloads and
exposes only their opaque event IDs/count, while egress shadow selects existing
engine-specific prompt text and parses DOM field/attribute values. Findings are
redacted; the observer still does not block or change a Skyvern request.

### Browser-backed synthetic audit (Completed 2026-07-23)

The isolated `synthetic.payment` console now exposes stable semantic DOM
attributes (`data-governance-field`, `data-governance-action`, `data-testid`,
and `aria-label`). `enterprise/governance/browser_audit.py` can open that page
with Playwright and produce a `phase2-browser-audit-v1` manifest containing
HMAC-bound observation and screenshot fingerprints, stable field/action
references (the page URL and semantic DOM names are HMAC-bound), readiness, and
deterministic audit policy decisions.

Raw HTML, screenshot bytes, form values, browser sessions, and credentials are
discarded before the manifest is returned. The collector does not click, submit,
issue permits, call ActionHandler, or connect the synthetic harness to Skyvern.
The manifest is synthetic test evidence, not a live audit-row replacement or
production authorization.

### P2: Freeze Future Interfaces (Completed)

1. Specify and test CapabilityGrant expiry semantics.
2. Specify the trusted creation-time identity snapshot for native Task,
   workflow Task, and template Task. Do not create TaskContracts from audit
   observation as a shortcut.
3. Version the serialization and invalidation rules for Contract, Grant,
   BusinessPlan, WorkOrder, Observation, and RecoveryDecision.

P2 is contract and test work only. It does not authorize a ForgeAgent adapter.

### Phase 2.2 Pre-Enforce Closure (Six Entry Families Sealed; Enforce Still Disabled)

The offline `run_governed_dry_run(...)` boundary now composes an audit-mode
TaskContract, active CapabilityGrant, BusinessPlan, BusinessPlanStep,
ExecutionWorkOrder, immutable page observation, and typed Action candidates.
It validates tenant, principal, department, business line, structured data
scope, expiry, plan/work-order bindings, operation bounds, and the one-external-
write-per-observation rule. It then emits `phase2-governed-dry-run-v1` evidence
containing only HMAC references, the observation hash, action fingerprints,
and deterministic PolicyDecisions.

When the BusinessPlanStep declares inputs or an expected transition, each
candidate must also be resolved by a trusted, versioned Domain Pack
`BusinessSemanticResolver`. Its protocol receives only the current Action,
ActionIntent, immutable observation, element evidence, and page HTML; it cannot
receive the Plan, Grant, WorkOrder, or Contract. The dry-run compares the
resolver's canonical facts and proposed transition with the Plan, checks
capability and confidence, and rejects missing or mismatched bindings. Every
canonical leaf must also declare a `fact_sources` path into the current
ActionIntent target facts. The dry-run independently resolves those paths and
requires the source value to equal the canonical value; preloading Plan values
into a resolver therefore cannot authorize a different observed target. Raw
facts, source paths, extractor references, and evidence references are not
retained; the report contains an HMAC over the resolver result and the actual
target facts, Action fingerprint, and observation. This contract proves
fail-closed composition but does not provide or authenticate a production
Domain Pack.

The API has no execution-adapter argument and reports
`execution_skipped=true`, `execution_adapter_called=false`, and
`runtime_wiring_eligible=false`. It accepts only `mode=audit`; an enforce
TaskContract is rejected. It imports neither Skyvern nor Playwright and does
not issue a Permit, create an ExecutionAttempt, call ActionHandler, or invoke a
business result probe. A successful dry-run is therefore **not** runtime
wiring or authorization to execute.

Only known Skyvern typed-action values are accepted. Unsupported values are
rejected before policy/report construction and are not echoed in the error.
`UNKNOWN`, `LOADING`, `TRANSITIONING`, `BLOCKED`, or readiness confidence below
`0.60` cannot return `allow`. TaskContract expiry uses the same exclusive
boundary everywhere: `now >= expires_at` is expired.

The execution-entry fixture records a closure contract for all six known
families: required disposition, guard owner, required controls, closure test,
and per-family enforce eligibility. `handler_locator_coordinate_javascript`,
`skyvern_page_script_proxy`, `shared_script_launchers`, `cua_ui_tars`,
`cached_speculative_actions`, and `sdk_direct_script_clients` are now `sealed`
and `enforce_eligible=true`. Global enforce remains disabled.

The source-marker inventory now also pins the HTTP script route, background
executor, CLI launcher, generated sync/async script clients, and the direct
`/sdk/actions` AI-page route. The script route, executor, CLI, and workflow
initializer converge on the sealed shared boundaries. The SDK/direct closure
test proves each known caller reaches a fixed HTTP rejection, guarded service,
or guarded page-adapter boundary before browser access.

CUA Permit issuance and Attempt authorization now require exact, fresh engine
evidence bound to the Action and observation. Future enforce discards reused
speculative state, disables speculative generation, and returns no legacy
cached Actions before database retrieval. These controls do not connect
ForgeAgent to Permit issuance and do not change `off/audit` execution.

The pre-enforce synthetic benchmark adds eleven cases covering allow/read,
approval/high, external-write/critical, transitioning/needs-human, action
drift, page drift, multi-external-write rejection, cached/speculative stale
observation, CUA evidence requirements, governed-script rejection, and
SDK/direct route-or-reject closure. The
governed-script case requires a sealed launcher row; cached/speculative and CUA
cases assert the now-implemented future controls while global enforce remains
unavailable.

## 5. Current Audit Evidence Contract

The live audit hook may persist only the following classes of data:

- redacted typed Action payload;
- HMAC-bound page observation reference;
- HMAC-bound screenshot and element evidence references;
- task, step, organization, event type, timestamp, and payload schema version.

It must not persist raw HTML, screenshot bytes, raw element content, business
intent, policy decision, permit, contract, approval decision, or execution
attempt. It must not issue an extra model request.

Audit write failure is non-blocking for Phase 1 execution, but must be logged
and included in audit-completeness metrics. It must never be represented as a
complete audit record.

Replay and completeness reporting treat a row as invalid when its persisted
observation hash does not match the versioned payload evidence, or when its
action/observation fingerprint is missing. Invalid history exposes only opaque
event IDs and aggregate counts.

## 6. Readiness Workstream Status

| Workstream | Current status | Next allowed action |
|---|---|---|
| Capability and authorization | Interface foundation complete | Keep offline contracts stable; runtime wiring requires separate approval. |
| Capability request, planning, and admission | Synthetic persistence foundation | Keep request/Grant/Plan/WorkOrder bindings stable; no production caller, outbox publisher, or Skyvern Task publication. |
| L0--L4 recovery | Offline policy foundation | Expand synthetic fault corpus only. |
| Semantic observation and evidence | Audit hardening complete; AgentPact runtime observation also implemented | Extend offline evidence fixtures and data-quality checks; concrete runtime callers still require explicit Pack composition and are absent from the formal default registry. |
| Fallback policy | Six known families sealed | Keep all closure regressions stable and extend the inventory when a new side-effect caller is discovered. |
| Evaluation, audit, replay | Synthetic reference harness complete | Extend replay and benchmark coverage; synthetic results are not compliance evidence. |
| Domain Pack SDK / Contract Catalog | M3 reference conformance complete; M4 accepted as test evidence | Keep `synthetic.payment` outside the active registry/runtime and preserve the accepted no-replay proof. |
| `stripe.payment` hosted Checkout adapter | Explicit governed test mode only | Run only through explicit hosted-flow composition or the manual smoke command with a test key; preserve durable Permit/Attempt, redacted evidence, independent PaymentIntent reads, and no-replay UNKNOWN handling. It remains a test-mode candidate and not a production Pack. |
| External production Domain Pack | None | Installation or runtime use requires adopter-supplied Q1--Q10, conformance evidence, and a separate scoped approval. |
| Real enforce wiring | Deferred | Requires every Section 7 gate and a separate approval. |

“Foundation” never means “wired into live execution.” A task can only be
marked runtime-complete when its acceptance test proves the required live path
and the task's authorization boundary permits that work.

### Remaining Authorized Work

The following work may continue without opening an enforce or Domain Pack
phase:

1. expand the offline L0--L4 fault corpus and UNKNOWN-stop assertions;
2. expand the fallback and execution-entry inventory/regression matrix for
   locator, coordinate, JavaScript, CUA/UI-TARS, cached/speculative, script, and
   SDK/direct paths when a new side-effect family or caller is discovered;
3. strengthen audit replay compatibility, evidence-quality fixtures, and
   aggregate reporting without changing action execution;
4. strengthen static migration rehearsal and operator documentation.

The currently known, finite gaps in this offline/audit-only package are closed.
Further work in the list above is evidence-driven: add it when a new failure
case, incompatible historical payload, or browser side-effect caller is
discovered. All six known execution-entry sealing slices are complete. M2's
offline Pack SDK and static Conformance Kit are complete after bounded review
remediation. M3's `synthetic.payment` normative offline reference is complete
after bounded review remediation and independent `PASS`; it did not change the
active registry manifest, harness, browser behavior, or runtime wiring. The
exact M3 boundary and evidence are recorded in
`.claude/plans/m3-synthetic-reference-conformance.md`. The separately approved
M4 browser-effect proof is complete and independently accepted at fingerprint
`743d05d191b3d96440161c70c04fd0a6ea5bf940b8b89e2d421ac94be6cb75b4`.
M5 packages that evidence for developers; it does not open a production Pack,
real enforce wiring, deployment, or publication slice.
Any future production Pack or real enforce change requires a separate review and approval.

### Stripe live boundary

The repository now distinguishes three different claims. `app.py` plus
`store.py` is a self-built loopback checkout used only for recorded tests.
`StripeApiResultProbe` is a real Stripe test API read when supplied a
`sk_test_*` key. `live_browser.py` is the explicit real hosted Checkout adapter
and visits `https://checkout.stripe.com/c/...`; with its injected durable
Attempt/Permit session it is a governed test-mode composition, not a production
Pack or a fully mounted platform Agent Run service. The adapter fails closed for
missing or rejected credentials, missing composition, unknown page states,
transport failures, and non-terminal Stripe statuses. It never replays a
Checkout after `UNKNOWN`, and its evidence contains only redacted digests and
state.

The disposable-PostgreSQL `alembic upgrade heads` rehearsal is complete.
A production Domain Pack, real enforce wiring, runtime Planner/WorkOrder
integration, approval recovery execution, and real high-risk browser work
remain outside the current scope.

Disposable PostgreSQL 14.23 rehearsals completed locally without a Windows
service. The initial migration rehearsal on port 15432 verified both heads and
the `ent_006` partial unique index. The admission rehearsal on port 15433 then
applied `ent_007` and proved concurrent duplicate collapse, semantic-conflict
rejection, one aggregate plus one redacted outbox row, and zero Skyvern Task
rows. Both PostgreSQL processes are stopped. The later disposable data and log
remain under `E:/tmp/finrpa-admission-pg-20260725-0300` because automated
cleanup was policy-blocked. This evidence does not authorize a production
Domain Pack or Skyvern enforce wiring.

### Approved Synthetic Payment Sandbox

The user approved a non-production `synthetic.payment` reference Domain Pack
on 2026-07-23. It defines canonical payment facts, a deterministic high/critical
approval policy, fixed sandbox identities, separation of duties, a one-time
permit, an idempotent single submission, and a business result probe.

The isolated harness exercises `AUTHORIZED -> EXECUTING -> CONFIRMED | UNKNOWN
| FAILED`, including commit-then-timeout and inconclusive-probe faults. UNKNOWN
never replays the submission. The local FastAPI console calls only the in-memory
synthetic business system. It does not import ForgeAgent, ActionHandler,
Playwright, or any real payment integration. Its manifest is permanently marked
`production_eligible=false`.

### Accepted M4 and M5 release evidence

M4 exercised one real installed Chromium browser against a loopback-only
synthetic console through Skyvern's public `ActionHandler`. The disposable
database observed `EXECUTING` before the click, browser completion produced
durable `UNKNOWN`, replay under the same idempotency key was rejected before a
second request, and only an independent result probe confirmed exactly one
synthetic commit. Teardown retained no M4 process, port, or temporary root.

AgentPact exposes that proof through `python scripts/finrpa_release.py` with `doctor`,
`conformance`, `demo`, and `report`. Evidence schema
`finrpa.release-report/v1` is canonical, digest-bound, redacted, and stored only
under ignored `artifacts/m5/`. CI runs Ubuntu conformance on Python 3.11-3.13,
the Ubuntu synthetic E2E on Python 3.11, and manual/release Windows smoke. No CI
job receives a production secret or publishes/deploys anything.

## 7. Gates Before a Separate Enforce Decision

No process may start production or Skyvern-integrated enforce work until every
item below is explicitly reviewed and approved for a named production Domain
Pack, tenant, and workflow. The isolated synthetic harness is test evidence for
these gates, not permission to bypass them:

1. Every browser side-effect entry point is sealed or disabled for governed
   tasks, including scripts, CUA/UI-TARS, cached actions, and fallback paths.
2. A complete Domain Pack defines canonical facts, state transitions,
   authorization scope, approval rules, and a BusinessResultProbe.
3. Governor-to-permit-to-ActionHandler wiring is implemented with one
   state-changing action per observation.
4. `AUTHORIZED -> EXECUTING -> CONFIRMED | UNKNOWN | FAILED` has failure
   injection coverage; UNKNOWN never automatically replays.
5. Current authorization, approver separation of duties, permission revocation,
   and task identity snapshots are revalidated at the relevant transitions.
6. DOM, screenshot, Prompt, audit, and fingerprint egress/retention/access
   controls are implemented and tested.
7. Audit evidence and rollback from `enforce -> audit -> off` have been
   demonstrated in an isolated environment.

## 8. Working Protocol for Parallel Processes

1. Read this file, `foundation-closure.md`, and `execution-entrypoints.md`
   before starting Phase 2 work.
2. State the intended boundary in the task handoff: `audit hardening`,
   `interface-only`, `domain-pack`, or `enforce`.
3. A task labelled `audit hardening` must not import Permit, Governor,
   ApprovalPause, or ExecutionAttempt into ForgeAgent or ActionHandler.
4. A task labelled `interface-only` must not add a live caller from Skyvern.
5. Any proposal to change `GOVERNANCE_MODE`, recovery execution, or an
   execution entry point stops for explicit review before code is modified.
6. After each completed task, update this document first: state transition,
   files changed, validation run, remaining risk, and whether the runtime
   boundary changed.

## 9. Related Documents

- `next-stage-proposal.md`: target controlled-orchestration architecture.
- `foundation-closure.md`: exact current runtime and persistence closure.
- `execution-entrypoints.md`: browser side-effect inventory and enforce gates.
- `synthetic-benchmark.md`: synthetic-policy scope and benchmark limitations.
- `synthetic-payment-domain-pack.md`: approved sandbox facts, policy, console, and acceptance evidence.
- `final-product-charter.md`: final open-source product scope, milestones, and change control.
- `framework-first-replan.md`: framework-first rationale and Pack boundaries.
- `.claude/plans/m2-pack-sdk-static-conformance.md`: completed M2 implementation handoff.
- `.claude/plans/m3-synthetic-reference-conformance.md`: approved next offline implementation handoff.

## 10. Review Log

### 2026-07-22: Audit Hardening and Interface Additions

**Result: accepted as audit hardening and interface-only work after remediation.**

Verified properties:

- `enforce` remains configuration-rejected; no Capability, WorkOrder, Permit,
  Approval, Attempt, or ResultProbe path was connected to ForgeAgent or
  ActionHandler;
- runtime element evidence is re-keyed with the governance HMAC before audit
  persistence;
- `ent_006` preflights duplicate active PendingActions before creating its
  PostgreSQL partial unique index;
- CapabilityGrant expiry, trusted creation snapshots, and version envelopes
  remain offline contracts;
- latest focused governance suite: `160 passed`; the only warning is the existing
  pytest `asyncio_mode` configuration warning.

Review findings resolved:

1. Replay now skips invalid historical payloads and exposes only their opaque
   event IDs and count; valid rows remain replayable.
2. The audit hook now selects existing engine-specific text input, and egress
   shadow parses DOM fields/attributes as well as whole artifacts.

Runtime boundary change: **none**.

### 2026-07-23: Synthetic Domain Pack and Browser Audit Additions

**Historical pre-remediation result: accepted only as isolated synthetic
engineering evidence; remediation was required before the browser collector or
synthetic control protocol could be treated as a sealed reference boundary.**

Verified properties:

- `GOVERNANCE_MODE=enforce` remains configuration-rejected;
- the synthetic Domain Pack is not imported by ForgeAgent, ActionHandler, or a
  Skyvern execution path;
- `DomainPackManifest` rejects a synthetic pack marked
  `production_eligible=true`;
- the browser collector source contains no click, fill, submit, Permit, or
  ActionHandler call;
- the synthetic UNKNOWN flow probes canonical state and does not replay the
  submission.

Findings reproduced before remediation:

1. **P1 - authorization expiry is not revalidated at approval or execution.**
   The harness validates its CapabilityGrant only while preparing the
   challenge. A pending challenge can be approved after both the five-minute
   Grant and fifteen-minute TaskContract have expired, and approval then issues
   a fresh Permit. Execution checks object binding and Permit expiry but does
   not check the Contract or current authorization. The review reproduced a
   challenge whose Contract expired at `08:15` becoming `ready` at `08:16` with
   a Permit valid until `08:17`.
2. **P1 - the browser evidence manifest is not fully redacted.** The manifest
   persists the supplied `page_url`, semantic `field_name`, and `element_id`
   verbatim. Query-string credentials and identifiers embedded in semantic DOM
   attributes therefore survive even though raw HTML, screenshot bytes, and
   form values are omitted. The review reproduced a manifest containing a
   query token and identifier-bearing field metadata.
3. **P1 - synthetic-page isolation is conventional rather than enforced.**
   `collect_browser_audit_evidence` accepts any URL, does not restrict the
   origin to the isolated localhost console, does not verify a trusted
   synthetic page marker, and binds evidence to the caller-supplied URL rather
   than the final browser URL after redirects. It must not be treated as a
   trusted general-purpose audit entry point in this state.
4. **P2 - network-idle failure is not represented in readiness.** A
   `networkidle` timeout is swallowed, after which a page that self-declares
   `ready` can still be reported as READY with `0.95` confidence. Evidence must
   preserve the unsettled observation instead of relying only on DOM markers.
5. **P2 - the no-execution browser regression is incomplete.** The E2E test
   inspects only the returned manifest. It does not verify that the synthetic
   audit log remains empty and that no payment/challenge state was created, so
   it would not reliably catch a future accidental click or submit addition.

Validation evidence:

- `57 passed` across the synthetic Domain Pack, API, Capability, WorkOrder,
  Governor, Permit, Attempt, recovery, configuration, status, migration, and
  browser-manifest unit suites;
- the Chromium E2E could not be independently rerun in the restricted review
  environment because Playwright subprocess named-pipe creation was denied;
  the request to rerun outside the sandbox was unavailable due to the approval
  service returning HTTP 503. This is an environment limitation, not a passing
  or failing product assertion;
- the review modified no code, test, migration, configuration, or runtime path.

Runtime boundary change: **none**. The collector remains synthetic/audit-only,
the synthetic harness remains isolated, and the findings do not authorize
production Domain Pack work or Skyvern enforce wiring.

### 2026-07-23: Synthetic Domain Pack and Browser Audit Remediation

**Result: accepted as isolated synthetic engineering evidence after remediation.**

The review findings above were reproduced and fixed without opening a live
execution path:

1. The synthetic harness now stores the grant expiry in the trusted creation
   snapshot and revalidates the current task contract, current operator grant,
   principal, tenant, and scope before both approval and execution. Expired
   authorization invalidates the challenge and cannot issue or consume a permit.
2. Browser manifests now persist only HMAC fingerprints for the page URL,
   semantic field names, and DOM element identifiers. Raw HTML, screenshot
   bytes, form values, and browser sessions remain absent.
3. The collector accepts only the HTTP `127.0.0.1` root of the isolated
   synthetic console, rejects query strings and credentials, verifies the
   `synthetic-payment-console` / `synthetic.payment` page marker, and binds the
   observation to the final URL after navigation. Redirects are rejected.
4. A failed `networkidle` wait is preserved as `TRANSITIONING` readiness with
   reduced confidence; state-changing candidates require human review.
5. The browser E2E test verifies that the synthetic audit log is empty before
   and after collection, so an accidental click or submit is observable.

Validation after remediation:

- `29 passed` across browser-manifest, synthetic Domain Pack, Governor,
  configuration, and status regressions;
- real Chromium E2E: `1 passed`, including trusted-marker, redacted-reference,
  readiness, and no-execution assertions;
- `compileall` and `git diff --check` pass.

Runtime boundary change: **none**. Production enforce remains disabled, and the
synthetic pack remains permanently non-production.

### 2026-07-23: Phase 2.2 Pre-Enforce Closure Handoff for Review

**Result: implementation complete and awaiting an independent review process.**

Implemented properties:

- `enterprise/governance/dry_run.py` adds the audit-only, no-executor
  Contract-to-Action governance-chain validation and redacted versioned report;
- `analysis.py` and `governor.py` accept an optional caller-supplied evaluation
  time and TaskContract so offline decisions are deterministic and scope-aware;
- `execution_entrypoint_inventory.json` and `execution-entrypoints.md` define
  one machine-checked closure contract for each of the six known unsealed
  families;
- `pre_enforce_closure_scenarios.json` and its benchmark test cover ten
  cross-boundary acceptance cases;
- focused dry-run/entrypoint/benchmark coverage and the broader Phase 2 unit
  selection pass. The local `.venv` was completed with the already-declared
  `python-jose[cryptography]` dependency so HTTP-auth-related tests could be
  collected.

Validation evidence:

- `300 passed` across 39 Phase 2 governance, approval, audit, browser,
  capability, permit, recovery, synthetic, WorkOrder, and closure test files;
- `compileall` and `git diff --check` pass;
- no Chromium E2E or PostgreSQL migration rerun was required because this work
  adds no browser collection behavior, database schema, or persistence path.

Files added or materially updated by this work package:

- `enterprise/governance/dry_run.py`, `analysis.py`, and `governor.py`;
- `tests/fixtures/execution_entrypoint_inventory.json` and
  `pre_enforce_closure_scenarios.json`;
- `tests/unit/test_governed_dry_run.py`,
  `test_pre_enforce_closure_benchmark.py`, and
  `test_execution_entrypoint_inventory_fixtures.py`;
- `docs/phase-2/execution-entrypoints.md`, `synthetic-benchmark.md`, and this
  status document.

Runtime boundary change: **none**. `GOVERNANCE_MODE=enforce` remains
configuration-rejected. Capability/Plan/WorkOrder remain disconnected from
ForgeAgent; Governor/Permit/Attempt remain disconnected from ActionHandler;
all execution-entry rows remain unsealed and ineligible for enforce.

### 2026-07-23: Phase 2.2 Pre-Enforce Closure Independent Review

**Result: new dry-run assets remain offline and non-executable, but the review
found two P1 and two P2 closure gaps; they are not yet a complete pre-enforce
semantic boundary.**

Resolved in the preceding remediation and revalidated here:

- synthetic authorization is rechecked at approval and execution;
- browser URL and semantic field/element references are HMAC-bound;
- browser collection is restricted to the synthetic localhost root, trusted
  page marker, and non-redirected final URL;
- network-idle uncertainty is represented as `TRANSITIONING`;
- the Chromium regression checks that the synthetic audit log is unchanged.

Open findings:

1. **P1 - the dry-run validates structural bindings, not business-input
   bindings.** `run_governed_dry_run` validates Contract/Grant/Plan/WorkOrder
   structure and operation names, but does not compare
   `BusinessPlanStep.inputs` or `expected_transition` with the current
   ActionIntent target/extracted facts. A Plan for record A with a page action
   targeting record B still produces `allow` for a read operation. This is
   offline-only today, but it overstates the Contract-to-Action closure needed
   before enforce.
2. **P1 - unknown and low-confidence readiness can still allow actions.**
   `evaluate_audit_policy` handles loading, transitioning, and blocked states,
   but not `PageReadiness.UNKNOWN`; it also does not consume
   `readiness_confidence`. Reproduction: `UNKNOWN` with confidence `0.0`
   returned `allow` for a read candidate.
3. **P2 - arbitrary action types can enter the redacted dry-run report.** The
   public dry-run accepts `list[Any]`; an unknown `action_type` becomes the raw
   operation and is copied into `matched_rules`. A value such as
   `secret-action-type-987` was present in the report. Typed production Actions
   may constrain this, but the offline boundary itself does not.
4. **P2 - expiry comparison is inconsistent.** The dry-run rejects
   `now >= TaskContract.expires_at`, while the shared audit policy uses
   `now > expires_at`. The dry-run is protected by its own precheck, but direct
   future Governor use would differ at the exact expiry instant.

Validation evidence:

- focused review selection: `57 passed` across browser, synthetic,
  dry-run/benchmark, entrypoint, status, and governance-config tests;
- the status-reported broader closure selection remains `300 passed` and was
  not independently rerun in this read-only review;
- no ForgeAgent, ActionHandler, Playwright, Permit, ExecutionAttempt, or
  production Domain Pack path is imported or called by `dry_run.py`;
- `GOVERNANCE_MODE=enforce` remains configuration-rejected; all six execution
  entry families remain `unsealed` and `enforce_eligible=false`.

Runtime boundary change: **none**. These findings concern offline evidence and
future closure contracts only; no real browser or high-risk action was run.

### 2026-07-23: Phase 2.2 Pre-Enforce Closure Review Remediation

**Result: both P1 and both P2 findings are remediated in the offline boundary;
independent re-review is still required before changing its review status.**

Remediation:

1. A Plan step with business inputs or an expected transition now requires a
   per-candidate `CandidateBusinessBinding`. Missing evidence, capability drift,
   low confidence, input mismatch, or transition mismatch fails closed. The
   report retains only an HMAC binding over the current Action fingerprint,
   observation hash, canonical facts, and adapter evidence references. A named
   production Domain Pack is still required to own and authenticate a real
   semantic adapter.
2. `UNKNOWN` readiness and readiness confidence below `0.60` now return
   `NEEDS_HUMAN`; loading, transitioning, and blocked behavior remains
   fail-closed.
3. The analyzer normalizes only the declared Skyvern typed-action vocabulary,
   while dry-run rejects unsupported values before a report exists. The
   rejected raw value is not echoed.
4. Shared policy and dry-run expiry now both treat `now >= expires_at` as
   expired.

Regression evidence covers missing/mismatched business bindings, HMAC-only
binding output, UNKNOWN and low-confidence readiness, unsupported action types,
and exact-instant expiry. The broader selected Phase 2 suite reports
`306 passed` across the same 39 files; focused `ruff check`, `compileall`, and
`git diff --check` pass.

Runtime boundary change: **none**. The binding is an offline contract, not a
ForgeAgent integration. `GOVERNANCE_MODE=enforce` remains configuration-
rejected, no Permit or ExecutionAttempt is issued, and all six execution-entry
families remain unsealed.

### 2026-07-23: Phase 2.2 Dry-Run Remediation Re-Review

**Result: three of the four reported gaps are closed; one P1 remains open, so
the dry-run must stay in independent-review status.**

Confirmed fixed:

- missing business bindings now fail closed when absent, mismatched, below the
  confidence threshold, or attached to the wrong capability;
- `UNKNOWN` and low-confidence readiness now route to `NEEDS_HUMAN`;
- unsupported typed Action values are rejected before a report is emitted;
- shared policy and dry-run use the same exclusive expiry boundary;
- the dry-run remains executor-free and all six entry families remain unsealed.

Remaining P1:

1. **CandidateBusinessBinding is still not cryptographically or structurally
   bound to the observed Action's business target.** The binding compares its
   `observed_inputs` and `proposed_transition` to the Plan step and HMACs the
   Action fingerprint/observation, but it never derives or compares those
   canonical facts from the current ActionIntent, element evidence, or page
   observation. A Plan for `record-A` with an Action/page targeting `record-B`
   still returned `allow` when supplied a binding that merely repeated the Plan
   facts. The adapter evidence reference is caller-supplied and opaque, so it
   is not proof of target correspondence. This is offline-only now, but it
   leaves the claimed Plan-to-Action semantic closure incomplete.

Validation evidence:

- focused dry-run/benchmark/entrypoint/governor/status selection: `49 passed`;
- the residual mismatch was reproduced directly with the current implementation
  (`record-A` Plan, `record-B` page/action, Plan-matching binding -> `allow`);
- no code, browser, Permit, ExecutionAttempt, ForgeAgent, ActionHandler, or
  configuration path was changed by this review; `GOVERNANCE_MODE=enforce`
  remains rejected.

Runtime boundary change: **none**. The previous remediation is not promoted to
fully closed until a trusted Domain-Pack semantic adapter contract binds the
canonical facts to the actual candidate target and regression coverage proves
that mismatch is rejected.

### 2026-07-23: Phase 2.2 Semantic Target Binding Remediation

**Result: the residual P1 is remediated in the offline boundary; independent
re-review is still required.**

`CandidateBusinessBinding` is no longer accepted as a caller-supplied list.
For fact-bearing Plan steps, `semantic_resolver` is mandatory and must satisfy
the `BusinessSemanticResolver` protocol. The protocol deliberately excludes
Plan, Grant, WorkOrder, and Contract inputs. `analyze_action` exposes stable
non-governance `data-*` element facts to the resolver; the resolver derives
canonical facts from the current ActionIntent and evidence, and the dry-run
compares those facts against the Plan. The HMAC evidence also covers the
actual target facts, so the report cannot be detached from the observed
candidate.

Regression coverage now reproduces `record-A` Plan plus `record-B` element
evidence and rejects it even when the resolver is otherwise valid. It also
checks that the resolver protocol cannot receive authorization or planning
objects. The focused dry-run/governance selection passes; the full selected
Phase 2 count is recorded in the remediation activity entry below.

Runtime boundary change: **none**. No browser, Permit, ExecutionAttempt,
ForgeAgent, ActionHandler, or production Domain Pack path is connected.

### 2026-07-23: Phase 2.2 Semantic Target Independent Re-Review and Offline Acceptance

**Result: accepted as an offline pre-enforce contract after one additional P1
was reproduced and remediated. This is not runtime sealing or enforce
authorization.**

The re-review reproduced a remaining provenance bypass in the submitted
resolver design: a caller could construct a resolver preloaded with the Plan's
canonical values. Because the previous implementation only included actual
target facts in the HMAC, without comparing them to the binding, a `record-A`
binding could still be emitted for a `record-B` Action target.

The remediation adds a complete `fact_sources` map to each transient
`CandidateBusinessBinding`. The dry-run independently resolves every declared
source from the current ActionIntent target facts and requires exact equality
with every canonical input and transition leaf. Missing, extra, unresolved, or
value-mismatched sources fail closed. A regression now preloads the resolver
with the Plan's `record-A/open/reviewed` values while the current target exposes
`record-B/closed/archived`; the dry-run rejects it.

The same bounded package strengthened audit evidence quality by rejecting
replay rows detached from their persisted observation or missing an action
fingerprint. It also expanded static entrypoint evidence for the HTTP script
route, background executor, CLI, generated clients, main ActionHandler caller,
`RealSkyvernPageAi`, and `/sdk/actions` route. No entry was sealed.

Validation evidence:

- focused dry-run, audit, inventory, and status selection: `44 passed`;
- all unit tests: `762 passed` with one existing FastAPI deprecation warning;
- read-only synthetic Chromium audit E2E: `1 passed`;
- focused `ruff check`, `compileall`, JSON parsing, and `git diff --check`
  passed.

Runtime boundary change: **none**. `GOVERNANCE_MODE=enforce` remains
configuration-rejected; no live Planner, WorkOrder, Governor, Permit, Attempt,
approval recovery, ActionHandler, Playwright action, or production Domain Pack
path was added. All six execution-entry families remain `unsealed` and
`enforce_eligible=false`.

### 2026-07-23: Task 5 Shared Governed-Script Sealing

**Result: accepted for the two script execution-entry families; global enforce
remains disabled.**

`script_service.execute_script(...)` and `script_service.run_script(...)` now
call `assert_script_execution_is_not_governed()` before database/script loading,
background dispatch, dynamic module import, or user `run_workflow(...)` code.
`ScriptSkyvernPage.__init__(...)`, `create(...)`, and
`create_scraped_page(...)` call the same helper before adapter construction or
browser-state acquisition. `RealSkyvernPageAi` retains its method-level checks
as defense in depth.

The HTTP script route, background executor, CLI, workflow initializer, and SDK
action route are pinned to those shared boundaries by source-marker and AST
ordering regressions. The matrix closure-test names now resolve to real test
functions. The `skyvern_page_script_proxy` and `shared_script_launchers` rows
are `sealed` and `enforce_eligible=true`; the other four rows remain
`unsealed` and `enforce_eligible=false`.

Validation evidence:

- focused config, entrypoint, benchmark, inventory, and status selection:
  `40 passed`;
- all unit tests: `765 passed` with one existing FastAPI deprecation warning;
- read-only synthetic Chromium audit E2E: `1 passed`;
- shared helper behavior is dynamically tested for `off`, `audit`, and forced
  `enforce`; entry ordering is AST-checked because the migration-focused local
  `.venv` does not contain Skyvern's `pyotp` runtime dependency.

Runtime boundary change: **none for supported configuration**. `off` and
`audit` preserve Phase 1 script behavior, while normal configuration still
rejects `GOVERNANCE_MODE=enforce`. No Permit, ExecutionAttempt, ActionHandler,
approval recovery, browser action, or production Domain Pack wiring was added.

### 2026-07-23: Task 5 Governed ActionHandler Mechanism Sealing

**Result: accepted for `handler_locator_coordinate_javascript`; three of six
execution-entry families are now sealed, while global enforce remains
disabled.**

`ActionHandler.handle_action(...)` now accepts `ExecutionAuthorization` and
`ExecutionProfile` only as a complete pair. The authorization carries the
deterministic `ExecutionEffect`. Permit issuance checks the profile policy and
stores the effect and full profile in the existing JSON decision payload. The
Attempt service rejects a downgraded effect or substituted profile before
Permit consumption.

The handler verifies the current Action/page HMAC binding and profile policy
before creating an `AUTHORIZED` Attempt. It commits `EXECUTING` before browser
access, binds the validated profile to the current asynchronous attempt, and
then permits only the configured fallback prefix. `fallback_rank` is validated
against the fixed mechanism order:

```text
locator -> label -> coordinate -> JavaScript
```

Coordinate/JavaScript/CUA profiles remain policy-ineligible for
`external_write`. A profile mismatch cannot consume the Permit. Handler success
or exception still transitions the Attempt to `UNKNOWN` pending an independent
business result probe.

Validation evidence:

- focused Permit, Attempt, profile, handler-boundary, inventory, benchmark, and
  status selection: `59 passed`;
- all unit tests: `772 passed` with one existing FastAPI deprecation warning;
- read-only synthetic Chromium audit E2E: `1 passed`;
- focused `ruff check`, `compileall`, JSON parsing, and `git diff --check`
  passed.

Runtime boundary change: **dormant governed handler path only**. Existing
`off/audit` callers provide neither authorization nor profile and preserve Phase
1 behavior. ForgeAgent does not issue or pass a Permit, CUA and cached actions
are not newly authorized, approval recovery is not connected, no production
Domain Pack was added, and normal configuration still rejects
`GOVERNANCE_MODE=enforce`.

### 2026-07-24: Remaining Execution-Entry Family Sealing

**Result: implementation complete for the last three known families; awaiting
independent review. Global enforce remains disabled.**

The bounded slice sealed these families without adding a live governance
caller:

1. `cua_ui_tars`: `CUAExecutionEvidence` binds engine identity, Action,
   observation, opaque evidence references, and capture time. Permit issuance
   persists the exact evidence. Attempt authorization rejects missing, stale,
   detached, or substituted evidence before Permit consumption, and the public
   handler requires it for a CUA profile.
2. `cached_speculative_actions`: future enforce discards popped speculative
   state, uses the normal fresh `build_and_record_step_prompt` path, disables
   speculative generation, and returns no legacy cached Actions before a
   database lookup. Existing `off/audit` reuse behavior is unchanged.
3. `sdk_direct_script_clients`: the cross-layer closure test proves generated
   sync/async clients converge on the fixed script HTTP rejection or guarded
   service, while `/sdk/actions` reaches guarded
   `ScriptSkyvernPage.create_scraped_page` before constructing its AI adapter.

The canonical execution-entry fixture now marks all six known families
`sealed` and `enforce_eligible=true`, each with a passing named closure test.
This means the known entry controls are implemented; it does not mean a live
Planner/Governor/Permit path, production Domain Pack, approval recovery, or
enforce configuration is available.

Validation evidence:

- focused closure selection: `65 passed`;
- all unit tests: `781 passed` with one existing FastAPI deprecation warning;
- read-only synthetic Chromium audit E2E: `1 passed`;
- changed-scope Ruff, `compileall`, JSON parsing, both Alembic heads, and
  `git diff --check` passed;
- repository-wide Ruff remains non-clean with 74 pre-existing findings outside
  this slice; none is in a file changed for this sealing goal.

Runtime boundary change: **dormant/enforce-only controls only**. ForgeAgent
still does not issue or pass a Permit, no production Domain Pack was added, no
real high-risk Action was run, and configuration still rejects
`GOVERNANCE_MODE=enforce`.

## 11. Activity Log

| Date | Work item | State | Scope | Notes |
|---|---|---|---|---|
| 2026-07-29 | M5 developer and release experience | DONE/PASS; independently accepted | Added the cross-platform Python CLI, digest-bound redacted reports, platform-aware M4 discovery, Ubuntu conformance/E2E CI, manual/release Windows smoke, README/guide, attribution, and curated two-commit local history. | Focused M4/M5/status contracts `29 passed`; canonical M1-M3 conformance `37 passed`; final real M4 browser E2E `1 passed`; Ruff, diff/whitespace, isolated compileall, report validation, and zero-root cleanup passed. Accepted HEAD: `d1c2587b2b03ae107429e1cd131dd5bc5082c390`; final review SHA-256: `d73196352e8f06512d69fc92a86127f19e370c11ffaf31f67183a39c55153707`; final demo report digest: `38e67ba9bdb12cd7ee6ebb223126e4502fefc858b89ac51d0e22db917c558432`. No full suite, push, tag, publication, deployment, production Pack, runtime wiring, migration, or global enforce occurred. |
| 2026-07-28 | M4 synthetic governed browser proof | Completed and independently accepted | Added one process-isolated real Chromium/PostgreSQL evidence path through Skyvern ActionHandler only. | Durable `EXECUTING -> UNKNOWN -> CONFIRMED`, one synthetic commit, duplicate rejection before replay, independent probe confirmation, and complete cleanup passed at fingerprint `743d05d191b3d96440161c70c04fd0a6ea5bf940b8b89e2d421ac94be6cb75b4`. Runtime remains audit-only and global enforce remains rejected. |
| 2026-07-26 | M3 synthetic reference Pack conformance | Completed after bounded review remediation and independent acceptance | Added the offline `synthetic.payment` SDK-manifest adapter and normative conformance suite; repaired the relative-import guard and reconciled the static evidence documentation. | Focused M3/M2 `32 passed`; adjacent regressions `38 passed`; Ruff, isolated compileall, direct whitespace inspection, report schema/status/digest, and stable four-file hashes passed independent re-review. Runtime remains audit-only, enforce remains configuration-rejected, and M4 is not authorized. |
| 2026-07-26 | M2 Pack SDK and static Conformance Kit | Completed after review remediation and owner acceptance | Closed incomplete fact/evidence binding, undeclared write-probe, read-only result-probe, and relative-import guard gaps inside the frozen repair scope. | Focused `19 passed`; adjacent M1/synthetic/status `27 passed`; governance config `5 passed`; Ruff, compileall, and diff checks passed. Runtime remains audit-only and enforce remains configuration-rejected. |
| 2026-07-25 | M2 Pack SDK and static Conformance Kit | Implemented; independent-review remediation required | Added offline SDK authoring shapes, deterministic static conformance reports, and deliberately invalid synthetic fixtures only. | The runtime boundary remains unchanged, but independent review reproduced two P1 conformance false-acceptance gaps and a P2 static-import regression-test gap. The repair scope is frozen in `.claude/plans/m2-pack-sdk-static-conformance-review.md`; do not begin M3 until it passes re-review. |
| 2026-07-25 | Final framework product Charter and delivery policy | Completed as documentation | Fixed the open-source goal as Governed Browser-Agent Harness + Domain Pack SDK + Conformance Kit; recorded release criteria, milestone gates, ownership model, and change control. | M1 Contract Catalog is complete; M2/M3 remain framework work, M4 synthetic governed browser proof requires separate approval, and no production/runtime/enforce authority changed. |
| 2026-08-28 | Repository simplification: retired unused domain contract draft | Completed | Removed the uninstalled, source-free domain contract prototype, its dedicated test, approval request, and release conformance entry after confirming there were no runtime, registry, API, browser, or persistence callers. | Current conformance remains covered by the generic SDK and synthetic Pack suites; no active runtime boundary changed. |
| 2026-07-25 | Admission/runtime baseline consistency | Started | Reconcile the completed admission/rehearsal facts and the enforce-rejection rationale across foundation docs, proposal, configuration, and static regressions. | Interface-only consistency work; no admission transaction, outbox publication, Skyvern caller, browser/payment effect, recovery execution, production Domain Pack, or enforce availability change. |
| 2026-07-25 | Admission/runtime baseline consistency | Completed after strict cross-layer review | Corrected the foundation to distinguish the sole Skyvern-connected audit path from the isolated synthetic persistence API, recorded the completed `ent_007` rehearsal, removed the resolved Proposal drift note, and replaced the stale entry-sealing rejection rationale while preserving hard `enforce` rejection. | Focused `50 passed`; full unit `817 passed` with one existing FastAPI deprecation warning. Ruff, compileall, Alembic heads, diff/whitespace, forbidden-caller, outbox-consumer, and explicit enforce-rejection checks passed; runtime boundary unchanged. |
| 2026-07-25 | Audit-only governed Task admission persistence | Started | Add an allowlisted SQLAlchemy repository, atomic redacted outbox, additive migration, and one optional synthetic API caller. | No Skyvern Task publication, outbox publisher, browser/business transition, production caller, or enforce activation. |
| 2026-07-25 | Audit-only governed Task admission persistence | Completed after strict cross-layer review | Added `ent_007`, whole-bundle tenant/capability allowlists, duration-preserving semantic idempotency, aggregate-before-outbox transaction ordering, and the optional `POST /api/task-admissions` synthetic caller. | Focused `44 passed`; full unit `816 passed` with one existing FastAPI deprecation warning; Ruff, compileall, Alembic heads, whitespace, forbidden-caller, and diff checks passed. PostgreSQL concurrency produced one admission/outbox and zero Skyvern Tasks; `enforce` remains rejected. |
| 2026-07-25 | Independent authorization, interaction, and admission foundation | Started | Implement the approved synthetic/interface-only RBAC dimensions, shared UI/chat request, strict Plan validation, and audit-only atomic admission protocol. | No production Domain Pack, live task creator, persistence repository, Skyvern state-changing path, or enforce activation. |
| 2026-07-25 | Independent authorization, interaction, and admission foundation | Completed after strict cross-layer review | Added independent role dimensions, identity/revocation-bound Grants, disclosure-safe projections, request/input/resource-bound Plans, all-referenced-Grant admission bundles, exact Contract/probe checks, and tenant/request idempotency. | Focused interface and adjacent regression `83 passed`; full unit `807 passed` with one existing FastAPI deprecation warning. No live caller or runtime boundary changed; `GOVERNANCE_MODE=enforce` remains configuration-rejected. |
| 2026-07-24 | Remaining execution-entry family sealing | Started | Seal CUA/UI-TARS, cached/speculative Actions, and SDK/direct clients with their named closure regressions. | No enforce activation, ForgeAgent-to-Permit wiring, production Domain Pack, approval recovery execution, or real high-risk Action. |
| 2026-07-24 | Remaining execution-entry family sealing | Completed; awaiting independent review | All six known families are now machine-marked sealed; the last three have fresh-evidence, fresh-reobserve, and route-or-reject controls. | Focused `65 passed`; full unit `781 passed`; read-only Chromium E2E `1 passed`; changed-scope static checks passed. Global enforce remains configuration-rejected. |
| 2026-07-23 | Task 5 governed ActionHandler mechanism sealing | Started | Require profile, matching authorization, Permit-bound effect/profile, and durable Attempt ordering for locator/label/coordinate/JavaScript. | No ForgeAgent caller, enforce activation, CUA/cached closure, approval recovery, browser E2E action, or production Domain Pack. |
| 2026-07-23 | Task 5 governed ActionHandler mechanism sealing | Completed | Sealed `handler_locator_coordinate_javascript`; three entry families remain unsealed. | Focused `59 passed`; full unit `772 passed`; read-only Chromium E2E `1 passed`; static checks passed. `off/audit` unchanged and enforce remains configuration-rejected. |
| 2026-07-23 | Task 5 shared governed-script sealing | Started | Reject future enforce-mode scripts at shared service and adapter boundaries. | No enforce activation, Permit/ActionHandler wiring, approval recovery, production Domain Pack, or browser action. |
| 2026-07-23 | Task 5 shared governed-script sealing | Completed | Sealed script proxy and shared launcher families; four execution-entry families remain unsealed. | Focused `40 passed`; full unit `765 passed`; read-only Chromium E2E `1 passed`. `off/audit` unchanged and enforce remains configuration-rejected. |
| 2026-07-23 | Phase 2.2 semantic target independent re-review | Started | Re-review resolver provenance, offline evidence, audit replay integrity, and execution-entry inventory completeness. | Offline/audit/static scope only; no runtime caller, entrypoint guard, browser action, Domain Pack, or enforce change. |
| 2026-07-23 | Phase 2.2 semantic target independent re-review | Completed and accepted offline after remediation | Added canonical `fact_sources` verification, detached-audit-row rejection, and known script/SDK caller source markers. | Focused `44 passed`; full unit `762 passed`; read-only Chromium E2E `1 passed`; static checks passed. Runtime boundary unchanged and all six entry families remain unsealed. |
| 2026-07-23 | Phase 2.2 dry-run remediation review | Started | Review the latest fixes for Plan-input binding, readiness fail-closed behavior, operation redaction, and expiry consistency. | Read-only review followed by documentation recording; no execution path, enforce setting, or high-risk action change. |
| 2026-07-23 | Phase 2.2 dry-run remediation review | Completed with residual P1 | Confirmed readiness, typed-action, expiry, and missing-binding fixes; reproduced a remaining Plan-facts versus actual Action-target mismatch. | Documentation-only change. Focused selection `49 passed`; runtime boundary unchanged and independent-review status retained. |
| 2026-07-23 | Phase 2.2 pre-enforce closure independent review | Started | Review the new executor-free dry-run, closure benchmark, and remediation claims against the master status and existing governance contracts. | Read-only review followed by documentation recording; no code, test, migration, configuration, browser, or runtime behavior change. |
| 2026-07-23 | Phase 2.2 pre-enforce closure independent review | Completed with findings | Recorded two P1 and two P2 gaps: missing Plan-input-to-Action binding, fail-open UNKNOWN/low-confidence readiness, unconstrained operation strings, and expiry-boundary inconsistency. | Documentation-only change. Focused selection `57 passed`; no runtime boundary change and no high-risk action run. |
| 2026-07-23 | Phase 2.2 pre-enforce closure review remediation | Started | Resolve the two P1 and two P2 findings inside the executor-free dry-run and deterministic policy boundary. | Offline contracts/tests/docs only; no Skyvern, ActionHandler, Playwright, Permit, persistence, or enforce wiring. |
| 2026-07-23 | Phase 2.2 pre-enforce closure review remediation | Completed; awaiting re-review | Added fail-closed Domain-Pack business binding, conservative readiness confidence handling, typed-action allowlisting, and consistent exclusive expiry semantics. | `306 passed` across 39 selected Phase 2 files; focused `ruff check`, `compileall`, and `git diff --check` passed. Runtime boundary unchanged and all execution-entry rows remain unsealed. |
| 2026-07-23 | Phase 2.2 semantic target binding remediation | Started | Replace caller-supplied business-fact attestations with a resolver that derives canonical facts only from the current candidate and observation. | Offline contracts/tests/docs only; no execution path, browser, Permit, ActionHandler, or enforce change. |
| 2026-07-23 | Phase 2.2 semantic target binding remediation | Completed; awaiting re-review | Added `BusinessSemanticResolver`, ActionIntent data-attribute facts, actual-target HMAC binding, and a record-A/record-B mismatch regression. | `307 passed` across 39 selected Phase 2 files; focused `ruff check`, `compileall`, and `git diff --check` passed. Residual P1 is remediated offline; all execution-entry rows remain unsealed. |
| 2026-07-22 | Status-document path correction | Started | Read the authoritative master status document and remove only the accidental duplicate created under `docs/phase2`. | No Phase 2 implementation work is included. |
| 2026-07-22 | Status-document path correction | Completed | Removed the accidental duplicate; `docs/phase-2/phase-2-master-status.md` remains the sole authoritative status document. | No runtime or governance boundary changed. |
| 2026-07-22 | Audit Hardening P0 | Started | Re-key audit element evidence, add the duplicate-active PendingAction deployment preflight, and document manual remediation. | No PostgreSQL rehearsal, enforce, recovery execution, or ActionHandler policy wiring. |
| 2026-07-22 | Audit Hardening P0 | Completed for items 1-3 | Updated `enterprise/governance/audit.py`, `ent_006`, audit/constraint tests, and `pending-action-migration-runbook.md`. | `136 passed` focused tests; `alembic heads`, `compileall`, and `git diff --check` passed. Item 4 remains pending explicit disposable-PostgreSQL authorization; runtime boundary unchanged. |
| 2026-07-22 | Allowed-work status review | Started | Read the master status and enumerate only currently authorized next tasks. | Read-only review; no implementation scope opened. |
| 2026-07-22 | Allowed-work status review | Completed | Reported the authorized P1/P2 work and the P0 deployment-rehearsal approval gate. | No runtime boundary, code, migration, or configuration changed. |
| 2026-07-22 | Audit Hardening P1 | Started | Add read-only audit query/replay, completeness metrics, synthetic coverage, and non-blocking local egress shadow findings. | Audit-only boundary; no policy decision, model call, execution change, or enforce work. |
| 2026-07-22 | Audit Hardening P1 | Completed | Added redacted local egress-shadow findings, version-locked replay/query reporting, completeness metrics, and synthetic candidate fixtures. Updated `audit.py`, `egress_shadow.py`, `audit_reporting.py`, audit tests/fixtures, `foundation-closure.md`, and `synthetic-benchmark.md`. | Focused audit/benchmark suite: `28 passed`; only the existing `asyncio_mode` configuration warning. Egress shadow is evidence only, external write-failure counts are report input, and the runtime boundary remains audit-only. |
| 2026-07-22 | Interface and Offline Tests P2 | Started | Specify CapabilityGrant expiry, trusted creation-time identity snapshots, and versioned serialization/invalidation contracts. | Interface/test-only boundary; no Planner, ForgeAgent, browser, permit, approval, recovery, or enforce wiring. |
| 2026-07-22 | Interface and Offline Tests P2 | Completed | Added explicit CapabilityGrant TTL/expiry validation, trusted native/workflow/template creation snapshots, and versioned Contract/Grant/BusinessPlan/WorkOrder/Observation/RecoveryDecision envelopes. Updated interface tests and `versioned-interface-contracts.md`. | `139 passed` non-HTTP focused governance tests and `48 passed` P1/P2 targeted tests; `compileall`, `alembic heads`, and `git diff --check` passed. TaskContract persistence remains unconnected and audit-only; no Planner/ForgeAgent/browser/permit/approval/recovery/enforce wiring changed. |
| 2026-07-22 | Audit Hardening P1 review remediation | Started | Make replay tolerant of invalid stored payloads and complete redacted Prompt/DOM field-level egress shadow observation. | Audit-only boundary; no model call, policy decision, Action selection, browser execution, or enforce work. |
| 2026-07-22 | Audit Hardening P1 review remediation | Completed | Updated `audit_reporting.py` to skip/report invalid historical payload IDs; updated `egress_shadow.py` for parsed DOM field/attribute findings; selected existing engine-specific text input in `ForgeAgent`; added replay, DOM, and prompt-routing regressions; updated foundation and review documentation. | `140 passed` focused governance tests and `24 passed` audit/config tests; `compileall`, `alembic heads`, and `git diff --check` passed. No extra model request, persisted raw artifact, Action change, browser execution change, or enforce activation; disposable PostgreSQL rehearsal remains separately pending. |
| 2026-07-22 | Phase 2 documentation consistency closure | Started | Reconcile completed P1/P2 status, proposal-task status, review findings, and corrupted status prose; add static regression coverage. | Documentation/test-only; no runtime, migration, configuration, or execution change. |
| 2026-07-22 | Phase 2 documentation consistency closure | Completed | Reconciled P1/P2 and proposal-task states, added the remaining-authorized-work list, refreshed review test counts, and added `test_phase2_master_status.py`. | `12 passed` status/entrypoint/config tests; runtime boundary unchanged. |
| 2026-07-22 | Offline L0--L4 fault corpus expansion | Started | Add deterministic failure-injection fixtures and UNKNOWN-stop assertions for offline recovery decisions. | Pure fixtures/tests only; no browser retry, scheduler execution, result-probe call, or runtime recovery wiring. |
| 2026-07-22 | Offline L0--L4 fault corpus expansion | Completed | Expanded `governance_fault_replay.json` across every failure class, UNKNOWN attempt override, permission revocation, and Contract-scope drift; strengthened fixture assertions and benchmark documentation. | `14 passed` recovery/fault/metric tests; pure offline decisions only. |
| 2026-07-22 | Fallback and execution-entry regression matrix | Started | Add static inventory assertions for locator, coordinate, JavaScript, CUA/UI-TARS, cached/speculative, script, and SDK/direct paths. | Inventory/tests only; no shared script guard, ExecutionProfile runtime consumption, permit wiring, or browser behavior change. |
| 2026-07-22 | Fallback and execution-entry regression matrix | Completed | Added `execution_entrypoint_inventory.json` and source/document matrix tests covering handler fallbacks, script proxies/launchers, CUA/UI-TARS, cached/speculative actions, and SDK clients. | `14 passed` entrypoint/profile tests; every listed family remains documented as unsealed or inventory-only. |
| 2026-07-22 | Audit-only replay and evidence acceptance | Started | Strengthen replay-page bounds/counts and redacted evidence-format regression without changing audit capture or action execution. | Audit/read-only tests and pure contracts only; no model, policy, permit, or browser behavior. |
| 2026-07-22 | Audit-only replay and evidence acceptance | Completed | Added bounded replay pages, scanned/replayable/invalid counts, and stricter redacted DOM evidence assertions. | `21 passed` audit/replay/metric tests; audit capture and Phase 1 execution remain unchanged. |
| 2026-07-22 | Static migration rehearsal asset closure | Started | Strengthen additive-chain, destructive-operation, downgrade, and operator-runbook checks without connecting to a database. | Static source/tests/docs only; no `alembic upgrade heads`, database write, cleanup, or migration execution. |
| 2026-07-22 | Static migration rehearsal asset closure | Completed | Added additive-upgrade, preflight-order, no-row-repair, and runbook-authority assertions; documented the static acceptance checklist. | `8 passed` migration/approval tests; `alembic heads` reports `a86c9fdba6b3` and `ent_006`; no database migration executed. |
| 2026-07-22 | Remaining authorized foundation closure validation | Started | Run the full non-HTTP governance regression and static health checks across the completed documentation, fault, entrypoint, audit, and migration assets. | Validation/documentation only; runtime boundary remains audit-only. |
| 2026-07-22 | Remaining authorized foundation closure validation | Completed | Validated documentation consistency, full failure-class coverage, the six-family unsealed entrypoint matrix, bounded/redacted audit replay, and static additive migration assets. | `160 passed`; `compileall`, `alembic heads`, and `git diff --check` passed. Remaining work requires explicit PostgreSQL rehearsal approval, business facts for Task 7, or separate Task 8/enforce authorization; runtime boundary unchanged. |
| 2026-07-22 | PostgreSQL rehearsal handoff | Superseded | Replaced the unavailable-environment handoff with a local portable PostgreSQL instance. | The database is now available; the remaining blocker is the current Python/Alembic dependency environment. Runtime boundary unchanged. |
| 2026-07-22 | Disposable PostgreSQL environment | Completed | Provisioned a portable PostgreSQL 14.23 instance without registering a Windows service; created the local `skyvern` rehearsal database and verified an application connection on `127.0.0.1:15432`. | Database is localhost-only and disposable under `E:/tmp`; `GOVERNANCE_MODE` remains off. `alembic upgrade heads` is still pending because the current Python shell lacks a usable Alembic CLI/dependency environment; no migration or enforce path was run. |
| 2026-07-22 | PostgreSQL migration rehearsal | Completed | Created a project-local Python 3.11 `.venv`, installed the required migration dependencies, and ran `alembic upgrade heads` against the disposable PostgreSQL 14.23 database. | `alembic current` reports `a86c9fdba6b3` and `ent_006`; the `uq_pending_action_active_step` partial unique index exists; duplicate active PendingActions: 0 rows. `GOVERNANCE_MODE` remained off and no enforce/runtime execution path changed. |
| 2026-07-23 | Synthetic payment Domain Pack | Started | Implement the explicitly approved non-production payment Domain Pack, deterministic result probe, fault-injectable business store, and isolated enforce harness. | Synthetic tenant and accounts only; no ForgeAgent, ActionHandler, Playwright, production business facts, or global enforce activation. |
| 2026-07-23 | Synthetic payment Domain Pack | Completed | Added common Domain Pack/result-probe contracts, `synthetic.payment` canonical facts and policy, fixed sandbox accounts, one-time permit/attempt orchestration, a FastAPI console, and UNKNOWN-safe fault injection. | `55 passed` across synthetic, Capability, WorkOrder, Governor, Permit, Attempt, recovery, governance-config, status, and migration tests; live HTTP smoke flow confirmed. Manifest is not production eligible; `GOVERNANCE_MODE=enforce` remains configuration-rejected and no Skyvern execution path changed. |
| 2026-07-23 | Synthetic browser audit perception | Started | Add stable semantic DOM attributes and a browser-backed, screenshot-fingerprint-only collector for the isolated console. | Audit hardening only; no button click, ActionHandler, Permit, business result call, or enforce change. |
| 2026-07-23 | Synthetic browser audit perception | Completed | Added `enterprise/governance/browser_audit.py`, semantic DOM/action attributes, redacted manifest contracts, unit coverage, and a real Chromium smoke test. | Browser smoke discovered 14 semantic fields and 5 action affordances; focused browser suite `6 passed`; raw HTML/screenshot/form values are absent from the manifest. Runtime boundary remains audit-only and `GOVERNANCE_MODE=enforce` remains rejected. |
| 2026-07-23 | Synthetic Domain Pack and browser-audit review | Started | Review the newly added synthetic payment harness, browser evidence collector, documentation claims, and focused regression evidence. | Read-only review followed by documentation recording only; no code, test, migration, configuration, browser action, or runtime behavior change. |
| 2026-07-23 | Synthetic Domain Pack and browser-audit review | Completed with findings | Added the formal review result, three P1 findings, two P2 findings, reproduced expiry/redaction evidence, and validation limitations to Section 10. | Documentation-only change. `57 passed`; Chromium rerun remained environment-blocked. Runtime boundary unchanged; remediation is required before treating the additions as a sealed reference boundary. |
| 2026-07-23 | Synthetic Domain Pack and browser-audit remediation | Completed | Revalidated authorization at approval/execution, HMAC-bound URL and semantic references, enforced localhost/page-marker isolation, preserved network-idle uncertainty, and asserted no audit events during browser collection. | `29 passed` focused regressions plus real Chromium E2E `1 passed`; `compileall` and `git diff --check` passed. Runtime boundary unchanged; enforce remains rejected. |
| 2026-07-23 | Phase 2.2 pre-enforce closure | Started | Compose the existing governance interfaces in an executor-free dry-run, make all six execution-entry closure requirements machine-checkable, and extend synthetic acceptance evidence. | Audit/offline only; no ForgeAgent, ActionHandler, Playwright, Permit issuance, persistence, Domain Pack, or enforce wiring. |
| 2026-07-23 | Phase 2.2 pre-enforce closure | Completed; awaiting independent review | Added `phase2-governed-dry-run-v1`, structured closure contracts for six unsealed entry families, and ten pre-enforce benchmark cases; updated closure and benchmark documentation. | `300 passed` across 39 selected Phase 2 files; `compileall` and `git diff --check` passed. Every entry remains unsealed, dry-run remains non-executable, and runtime boundary is unchanged. |
