# AgentPact Pack Runtime and Persisted Browser Execution Plan

## Decision

AgentPact depends on the Domain Pack mechanism for trusted business semantics,
but no platform or execution-kernel module may depend on a concrete Pack such
as `synthetic.payment` or `stripe.payment`. Synthetic Packs are test/reference
implementations only.

The required dependency direction is:

```text
Agent Run                 -> generic Pack runtime contracts
Browser operation loop   -> generic action/policy/verifier ports
Persisted executor       -> generic Permit/Attempt governance services
Concrete Domain Packs    -> implementations of those contracts and ports
```

Boot composition may import concrete Packs to register them. Core Agent Run,
browser-loop, and governance modules must not import concrete Pack modules.

## Current Problem

The AgentPact browser loop is mostly generic, but the active Agent Run vertical
slice is still a `synthetic.payment` application:

- Agent Run imports synthetic constants, M8 journal types, M10 exceptions, and
  the concrete prepared-run model.
- Agent Run selects `synthetic.payment@1.0.0` directly rather than dispatching
  from a trusted installed-Pack binding.
- `PackRuntimeAdapter` represents lifecycle inputs and outputs as `object` and
  `**trusted_inputs`, forcing the platform to inspect concrete implementation
  types.
- Synthetic M10 combines business facts, planning, approval, browser
  mechanics, Permit issuance, Attempt persistence, and result probing.
- The state-changing submit action still depends on Skyvern `ActionHandler`.

Passing a synthetic E2E test does not prove that these boundaries are generic.

## Ownership Boundaries

### Agent Run

Agent Run owns:

- public run identity and lifecycle state;
- orchestration of admitted work steps;
- approval pause, resume, cancellation, and command idempotency;
- durable run journal and public projections;
- dispatch to an installed, version-pinned Pack runtime adapter;
- scheduling authoritative result-probe recovery.

Agent Run does not own:

- payment facts or another domain schema;
- DOM selectors and browser actions;
- Pack-specific approval policy;
- interpretation of a remote business result.

### Domain Pack Runtime

A concrete Pack owns:

- validation and canonicalization of business inputs;
- capability-to-business-operation mapping;
- deterministic action proposals for known applications;
- business risk/effect classification and approval requirements;
- business result-probe implementation and evidence interpretation;
- Pack-specific state transitions and business evidence validation.

A Pack must not consume a Permit or maintain a competing generic Attempt state
machine.

### Browser Operation Loop

The browser loop owns:

- observe, decide, authorize, act, reobserve, verify, and terminate ordering;
- observation/action integrity bindings;
- bounded retries before an external-effect boundary;
- selection between a matching deterministic Pack action provider and the
  policy-approved model provider;
- redacted correlated events.

It does not own SQLAlchemy transactions or domain result semantics.

### Persisted Browser Executor

The persisted executor owns one already-authorized browser action:

- no-side-effect preflight and freshness checks;
- one-time Permit consumption;
- crash-safe Attempt registration and state transitions;
- exactly one invocation of the injected browser runtime;
- preservation of the exact Attempt checkpoint for result probing;
- fail-closed recovery of ambiguous or abandoned execution.

It does not decide which action to take or whether business success occurred.

## Typed Pack Runtime Lifecycle

Replace the opaque core lifecycle methods with explicit, frozen contracts. The
exact class names may follow repository conventions, but the platform must be
able to understand the following data without importing a concrete Pack.

### Prepared Run Reference

Contains only platform-safe identity:

- run ID;
- tenant ID;
- request ID;
- Pack ID and immutable Pack version;
- admission ID and contract ID where available;
- provider mode;
- opaque Pack-owned payload or reconstruction reference.

The Pack remains responsible for validating its private payload. Agent Run
must not `isinstance` it against a concrete prepared-run class.

### Admission Result

Contains:

- the prepared run reference;
- the durable admission identity;
- initial generic advance result;
- no concrete business object.

### Advance Result

Use a closed status set:

```text
COMPLETED
AWAITING_APPROVAL
PENDING_RESULT_PROBE
FAILED
```

It carries only the fields legal for the selected status:

- generic approval request specification;
- exact execution checkpoint;
- result-probe reference;
- stable reason code;
- step/run correlation.

Pack-specific exceptions must not be used as normal lifecycle control flow.
Planning or validation failures need a generic code-bearing error boundary.

### Approval Request Specification

Contains generic fields required to persist and route approval:

- intent/fingerprint reference;
- requested approval route;
- source department/business line scope;
- risk/effect classification;
- expiration and reason code;
- redacted description.

It must not contain synthetic challenge objects.

### Execution Checkpoint

Contains exact immutable correlation:

- Permit ID;
- Attempt ID;
- task and step IDs;
- action fingerprint;
- observation hash;
- idempotency-key digest, not the secret/raw key where disclosure is unsafe;
- execution effect;
- result-probe reference;
- Attempt status.

### Probe Result

Use a closed result set:

```text
CONFIRMED
NOT_CONFIRMED
INCONCLUSIVE
```

It must bind to the exact execution checkpoint and carry only redacted evidence
references plus a stable reason code.

## Pack Selection

Pack selection must come from trusted request/configuration or an installed
Pack binding. A compatibility default may exist only at the API/composition
edge and must be explicit. `enterprise/agent_runs` must not contain hard-coded
Pack IDs or import concrete Pack implementations.

The runtime registry must validate the exact Pack ID, version, capability set,
and adapter identity at boot. Restoring a run must use the version pinned in
its durable admission rather than the current default.

## Generic and Pack-Guided Browser Modes

The browser loop supports two decision sources through the same typed
`ActionDecision` contract:

1. Pack-guided mode for known applications. A matching immutable Pack adapter
   may return a deterministic action or completion claim.
2. Model-guided mode for unknown or less structured pages. The model receives
   only the policy-approved observation projection.

Both modes pass through the same policy and execution boundary. A Domain Pack
is not required for reading, scrolling, ordinary navigation, or other policy-
allowed non-impacting actions.

High-impact external writes such as payment, submission, deletion, or approval
must have trusted capability authority and an authoritative result-probe
contract. If business success cannot be independently established, execution
fails closed. A successful Playwright call is transport evidence, not business
success.

## Persisted External-Write Protocol

For an already-authorized external write, ordering is mandatory:

```text
1. Validate action/authorization/profile bindings.
2. Perform pure page/selector/freshness preflight.
3. Transaction A:
   - lock and consume the one-time Permit;
   - create an AUTHORIZED Attempt;
   - commit.
4. Transaction B:
   - mark the exact Attempt EXECUTING;
   - commit.
5. Invoke the browser side effect exactly once.
6. Persist the Attempt as UNKNOWN.
7. Return PENDING_RESULT_PROBE with the exact checkpoint.
8. Allow only the authoritative result probe to produce CONFIRMED or FAILED.
```

The executor must compose the existing Permit and Attempt services. It must not
duplicate their state machine in the browser-loop package.

Once Permit consumption or Attempt creation begins, the generic loop must not
replay an external write. Observation staleness discovered before that boundary
may cause fresh observation, decision, policy evaluation, and authorization.

## Crash Windows and Recovery

| Crash window | Durable evidence | Required recovery |
| --- | --- | --- |
| Before Permit consumption | Permit remains issued | Reobserve; expire or replace old authority |
| After AUTHORIZED commit, before EXECUTING | Attempt proves browser was not called | Fail/abandon without executing; require fresh authority for another action |
| After EXECUTING commit, before/during browser call | Effect may have started | Move or retain as UNKNOWN; invoke result probe; never replay |
| Browser returns, before UNKNOWN commit | Attempt remains EXECUTING | Recovery scan treats it as ambiguous and probes |
| UNKNOWN committed, before Task/Step suspension | Exact Attempt is recoverable | Repair orchestration state to pending result probe |
| During result-probe transaction | Prior state remains authoritative | Retry the same probe idempotently |

Abandoned `EXECUTING` attempts require explicit ownership/lease semantics or a
defensible single-worker recovery rule. No implementation may treat an
`EXECUTING` row as safely retryable.

## Approval Resume

Approval authorizes business intent, not a stale DOM action:

1. Initial policy evaluation returns `AWAITING_APPROVAL` and persists no
   replayable selector or old action object.
2. Approval is durably decided.
3. Resume restores the exact admitted Pack/version and claims the run.
4. The browser is reacquired and freshly observed.
5. The Pack/model proposes a new action from that observation.
6. Policy revalidates current business facts and issues a fresh Permit.
7. Only then may the persisted executor cross the effect boundary.

## Result-Probe Rules

A probe may resolve only the exact UNKNOWN Attempt. Evidence must bind:

- Pack and immutable version;
- capability and work order;
- task, step, contract, Permit, and Attempt;
- action fingerprint and observation hash;
- idempotency-key digest;
- target business resource and expected version;
- probe reference and evidence timestamp/signature.

Identical repeated evidence is idempotent. Substituted or conflicting evidence
is rejected. `INCONCLUSIVE` leaves the Attempt and run pending; it does not
permit action replay.

## Migration Stages

### Stage 1: Generic Pack and Agent Run Contracts

- Introduce the typed lifecycle contracts above.
- Move genuinely generic M8 run journal/checkpoint types out of the synthetic
  package instead of copying the state machine.
- Remove concrete Domain Pack imports and type checks from Agent Run core.
- Dispatch by trusted, version-pinned Pack binding.
- Return typed approval/advance/probe outcomes instead of pack-specific
  exceptions and dictionaries.
- Add static dependency tests.

Acceptance:

- `enterprise/agent_runs` and `enterprise/browser_loop` import no module under
  `enterprise.domains.synthetic_payment` or `enterprise.domains.stripe_payment`;
- generic contracts contain no payment facts, challenge IDs, beneficiary
  fields, synthetic step-role literals, or hard-coded Pack IDs;
- existing recorded behavior remains compatible at the API edge.

### Stage 2: AgentPact Persisted Browser Executor

- Add a generic coordinator behind a browser-loop port.
- Reuse authorization guard, Permit, Attempt, and execution-profile services.
- Preserve an exact execution checkpoint through result and error contracts.
- Add abandoned-EXECUTING recovery.
- Ensure external-write retries stop at the durable boundary.

Acceptance:

- preflight failure creates no Attempt and consumes no Permit;
- the Attempt is committed EXECUTING before browser invocation;
- a browser success, failure, or timeout cannot cause automatic write replay;
- recovery can find the exact ambiguous Attempt without relying on task-only
  lookup.

### Stage 3: Migrate Synthetic Submit

- Generate an AgentPact `BrowserAction` from a fresh AgentPact observation.
- Reevaluate after approval and issue fresh authority.
- Execute through the persisted executor and enter pending result probe.
- Resolve through exact existing synthetic business evidence.
- Remove M10 use of Skyvern `ActionHandler`, `ClickAction`,
  `NativeActionHandlerOutcome`, and `PostActionControl`.

Skyvern's browser manager may temporarily remain only to supply the Playwright
page. Browser lifecycle extraction is a later stage.

### Stage 4: Prove Non-Synthetic Reuse

- Run a second, non-synthetic implementation or isolated conformance fixture
  through the same generic Pack and persisted-executor contracts.
- Prefer adapting existing Stripe contracts where this does not enable unsafe
  live execution or require network credentials.
- Keep any small conformance-only Pack out of the product registry.
- Require two non-identical adapters/fixtures to pass the same conformance
  suite.

Acceptance:

- the second implementation imports no synthetic code or constants;
- Stripe live execution remains fail-closed unless its durable recovery path is
  genuinely complete;
- passing synthetic tests alone is not accepted as architecture evidence.

## Fault-Injection Matrix

Tests must cover:

- preflight failure before Permit consumption;
- one-time Permit consumption;
- committed EXECUTING state before browser invocation;
- crash after AUTHORIZED and before EXECUTING;
- crash or timeout during browser action;
- crash after browser return and before orchestration suspension;
- UNKNOWN replay rejection;
- exact successful, failed, and inconclusive probe outcomes;
- idempotent repeated probe evidence;
- rejected substituted Permit, Attempt, action fingerprint, observation,
  resource, and evidence;
- rejected high-impact write without an authoritative probe;
- model-guided non-impacting operation without a concrete Pack.

## Non-Goals

- Broad removal of the Skyvern product shell.
- Replacement of browser/session lifecycle ownership.
- Activation of production Stripe writes or global enforce mode.
- Reimplementation of existing Permit/Attempt services.
- Frontend redesign.
- Repository-wide formatting or unrelated lint cleanup.

## Final Verification

Each stage requires focused tests and a coherent commit. Before review:

- run Ruff against all changed Python files;
- run browser-loop, Pack runtime, Agent Run, Permit/Attempt, and synthetic
  M7/M10 tests;
- run the complete unit/integration suite;
- run all E2E tests when the environment permits;
- run `git diff --check`;
- report exact remaining Skyvern imports and why each remains.

The branch must remain isolated for review and must not be merged into `main`
automatically.
