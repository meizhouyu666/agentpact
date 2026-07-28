# Enforce Readiness Proposal: Governed Payment Operations

> Status: **Draft / Discussion**
>
> Date: 2026-07-25
>
> Scope: next-stage architecture plus the explicitly approved 2026-07-25
> synthetic/interface and audit-only admission-persistence slices. This
> document does not authorize a production Domain Pack, Skyvern-integrated
> enforcement, approval recovery execution, or enabling GOVERNANCE_MODE=enforce.

The approved narrow slice covers independent authorization dimensions, the
unified UI/chat CapabilityRequest and Grant projection, strict offline Plan
validation, an audit-only atomic Task-admission protocol, versioned interfaces,
and tests. The follow-on slice adds a tenant/capability-allowlisted SQLAlchemy
repository, redacted pending outbox, and one isolated synthetic API caller that
does not publish a Skyvern Task. It creates no production caller, browser path,
or enforce availability. The full A--F design remains a future external-Pack
adoption architecture. The current project plan is Framework-First: Harness,
Pack SDK, and Conformance Kit, as recorded in `framework-first-replan.md`.

## 1. Purpose and decision record

FinRPA is evolving from enterprise browser automation into a governed Agent
Harness for financial operations. The goal is not to make a fixed-script payment
product, nor to let an unconstrained model decide financial operations. The
goal is to allow an Agent to reason about page variation and task execution
inside a deterministic business, authorization, and evidence boundary.

The following direction is confirmed for design:

1. Payment Operations is the reference domain used to validate the Pack SDK and
   Conformance Kit. FinRPA is not delivering an in-repository production
   payment Pack.
2. The platform remains a single-browser-executor architecture: Skyvern is the
   only component that may perceive and operate browser pages.
3. The existing organization x department x business-line x role model is the
   authority root. A model never infers identity, scope, or policy from a user
   query.
4. The product will support both a dynamic capability UI and natural-language
   requests. Both normalize into the same authorized capability request.
5. Audit, authorization, approval, permit consumption, attempt state, and
   recovery admission are Harness responsibilities, not Agent decisions.

This document is a design artifact. It must remain Draft / Discussion until a
review explicitly approves an implementation slice. An approval changes this
document first; the master status records only work that has subsequently
occurred.

## 2. Authoritative baseline

The Phase 2 master status remains the authority for runtime fact. At the time
of this proposal:

- audit observation is the only active Phase 2 runtime behavior;
- all six known browser side-effect entry families are marked sealed in the
  latest closure matrix, but global enforcement is still configuration-rejected;
- ForgeAgent does not create or pass Capability Grants, Plans, Work Orders,
  Permits, Execution Authorizations, or Result Probes to live execution;
- the governed ActionHandler branch is dormant and records a browser call as
  UNKNOWN pending an independent business result probe;
- the only payment Pack is synthetic.payment. Its manifest is permanently
  non-production and it is not connected to Skyvern or a real payment system;
- the latest recorded validation is 65 focused tests, 781 unit tests, and one
  read-only Chromium audit E2E test.

The active runtime is therefore:

~~~
Skyvern Task / Step
-> typed Action candidates
-> redacted audit candidate event
-> unchanged Phase 1 browser execution
~~~

Notably, this document does not reinterpret the sealed-entry result as
enforcement readiness. Entry sealing is one prerequisite, not the live
authorization path and not business approval.

### Baseline documentation reconciliation

The foundation-closure document now agrees with the master status that the
disposable PostgreSQL rehearsals were completed through `ent_007`. It also
distinguishes the only Skyvern-connected audit observation path from the
isolated synthetic admission-persistence API. The master status remains the
runtime source of truth; this reconciliation adds no runtime authority.

## 3. Product boundary

### 3.1 What this product is

FinRPA should become a Financial Operations Agent Harness:

~~~
trusted identity and scope
-> authorized business capabilities
-> constrained Agent plan
-> Work Order for Skyvern
-> deterministic governance boundary
-> business-result confirmation
~~~

The Agent is free to interpret a request, inspect an allowed page, choose a
permitted sequence of browser skills, obtain missing inputs, and propose a
replan. It is not free to invent a business operation, broaden data scope,
approve itself, consume a permit, or assert that an external financial effect
occurred.

### 3.2 What this product is not

- A second browser Agent, Planner-controlled Playwright loop, or multi-Agent
  swarm.
- A collection of hard-coded bank-specific scripts.
- A system in which the chat model is the authority for roles, thresholds,
  approval rules, canonical business facts, or result confirmation.
- A claim that synthetic payment evidence constitutes production compliance
  evidence.

## 4. Four authority layers

| Layer | Deterministic responsibility | Explicitly outside its responsibility |
|---|---|---|
| RBAC and tenancy | Establish principal, organization, department, business-line, special permissions, and visible resource scope. | Infer business intent from text or a page. |
| Domain Pack and Capability Registry | Define registered business capabilities, state transitions, input contracts, policy references, and result-probe references. | Locate DOM elements or call Playwright. |
| Planner and Coordinator | Choose and order only granted capabilities; create bounded Plans and Work Orders; request clarification or a permitted replan. | Grant itself permission or execute browser actions. |
| Governance Harness | Validate policy, issue and consume permits, pause for approval, persist attempt state, enforce UNKNOWN handling, and preserve audit evidence. | Replace domain-owned facts or declare business success without a probe. |
| Skyvern | Page perception, typed Action selection, browser interaction, and page-local L0--L2 recovery. | Expand business scope or treat browser success as business success. |

RBAC answers who may access a scoped resource. A Capability answers which
business transition may be requested in that scope. Policy and a one-time
Permit answer whether this specific transition may execute now.

## 5. Payment Operations as the reference domain

Payment Operations is selected as the reference domain because it has a
coherent lifecycle and exposes the core Harness problems: scoped data access,
preparation versus commitment, separation of duties, approval, external side
effects, and result confirmation.

This selection does not define a real payment system, production data fields,
amount limits, approval thresholds, or a bank integration. Those are future
Pack-adopter inputs and must not be invented by framework implementation.

### 5.1 Required design artifacts before any external Pack installation

An external Pack adopter and the platform owner must jointly provide:

1. a glossary of payment objects and their authoritative system of record;
2. canonical facts required for every proposed state-changing capability;
3. the allowed state-transition graph and invalid/terminal states;
4. ownership and scope rules for organization, department, business line, and
   service principal;
5. approval routing and separation-of-duties rules;
6. business result-probe evidence for each externally visible commitment;
7. data classification, retention, access, and model-egress rules;
8. the first supported external-system boundary and a non-production test
   environment.

Until these inputs exist, Payment Operations remains a reference namespace and
synthetic/conformance design target only. They do not block framework SDK or
Conformance Kit authoring.

### 5.2 Capability, template, adapter, and Pack are different

| Change | Correct extension mechanism | Example class |
|---|---|---|
| A page or external system differs while business facts remain the same | Integration Adapter / Profile | Different bank or ERP UI |
| The same transitions need a different common sequence | Workflow Template | Single-item versus batch preparation |
| Roles, scopes, thresholds, or approval paths differ | Versioned Policy Configuration | Tenant-specific delegation |
| Objects, lifecycle, result proof, and risk semantics differ | New Domain Pack | Payment Operations versus lending operations |

The project should not create a new Pack for each bank, screen, button, or
workflow variation. A Pack is justified only when its business ontology and
result semantics cannot safely share the existing Pack.

### 5.3 Pack expansion strategy

The roadmap is framework first, then reference validation, then external Pack
adoption:

~~~
Pack SDK and Contract Catalog
-> synthetic.payment reference Pack and browser/system fixtures
-> reusable Conformance Kit and invalid-Pack rejection cases
-> external adopter Pack integration through templates/adapters/policies
-> additional independently owned Domain Packs
~~~

No in-repository production-candidate Pack is a project milestone. Each future
external Pack must independently pass the Conformance Kit and enter audit-only,
rehearsal, and later tenant/workflow/Pack-scoped rollout; maturity is never
inherited from `synthetic.payment` or another adopter's Pack.

## 6. User-entry design

The product uses two inputs with one backend contract:

1. a dynamic capability UI that lists only the current principal's
   discoverable/requestable capabilities; and
2. a natural-language request that the Agent maps to a member of the same
   Capability Grant set.

~~~
user query or UI selection
-> trusted scope and installed Pack resolution
-> Capability Grant set
-> constrained intent matching and parameter completion
-> plan preview, clarification, or explicit user confirmation
~~~

The UI is not a second workflow engine. The Agent cannot use a user query to
request an unregistered capability. Ambiguous intent, absent required scope,
or a request that exceeds the available grant must produce clarification or a
safe alternative, never an implicit downgrade that changes user intent.

## 7. Target control flow

~~~mermaid
flowchart TB
    U[Query or capability UI]
    I[Trusted identity and tenant scope]
    R[RBAC + Capability Resolver]
    G[Versioned Capability Grant]
    P[Constrained Planner]
    W[Execution Work Order]

    subgraph S[Skyvern: only browser executor]
      O[Fresh observation]
      A[Typed action candidate]
      H[ActionHandler]
      O --> A --> H
    end

    V[Governance Harness: policy, approval, permit, attempt]
    B[Domain-owned Result Probe]
    T[Harness-owned Task state]

    U --> I --> R --> G --> P --> W --> O
    A --> V --> H
    H --> B
    B --> T
    T --> P
~~~

The following rules are required in the target design:

- a Plan references an active, matching Grant rather than an operation name
  created by the model;
- a Work Order is a browser-execution constraint, not a browser action list;
- one observation authorizes at most one state-changing action unless a Pack
  explicitly declares an auditable atomic/read-only chain;
- any Action that may cause an external side effect enters EXECUTING durably
  before the state-changing browser transport, not before read-only observation;
- browser transport success is UNKNOWN until the Domain Pack's result probe
  confirms a business fact;
- approval or recovery invalidates the old action and requires fresh
  observation, current authorization, and a fresh permit;
- rollback is directional: enforce to audit to off. It preserves evidence,
  quarantines existing governed Tasks from Phase 1 fall-through, and never
  replays an UNKNOWN external effect.

## 8. Design work packages before enforcement

All packages below are architecture/design work until separately approved.
Their ordering is deliberate.

The dependency order is not purely alphabetical: A (domain), B
(authorization), and E (data/evidence) are co-foundations; C consumes their
disclosure and Grant contracts; D consumes A--C plus E; F freezes and validates
all of them. Finishing an earlier written section grants no authority to
implement it while a required downstream safety contract or named owner is
unresolved.

### A. Payment Domain Pack contract

Define the Pack boundary, ownership, canonical-fact placeholders, state-machine
notation, capability catalog, result-probe contract, and policy reference
format. Do not fill production values without business-owner approval.

Acceptance: reviewers can distinguish a Pack, a workflow template, an adapter,
and a policy configuration; each proposed state-changing capability has named
business-owner inputs and an unresolved-result path.

#### A.1 Contract-freeze boundary

This package freezes the shape of a future Payment Operations Pack, not its
production values. The provisional design namespace is `payment.operations`;
it is not an installed Pack ID, does not make a Pack production-eligible, and
must not appear in a live Capability Registry before a separately recorded
approval.

The current offline `DomainPackManifest`, `CapabilityDefinition`,
`BusinessSemanticResolver`, and `BusinessResultProbe` interfaces are reference
foundations. A future implementation may extend them additively, but this
design does not imply that the fields below already exist in code or are wired
to Skyvern.

The following values remain business-owner inputs and may not be supplied by
the platform team:

- the payment-object glossary and authoritative system of record;
- canonical fields, validation rules, lifecycle states, and terminal states;
- which operations are externally committing, reversible, or prohibited;
- approval routing, thresholds, delegation, and separation of duties;
- what authoritative evidence proves each transition happened or did not
  happen;
- retention, classification, and model-egress treatment for each field.

#### A.2 Ownership model

| Owner role | Owns | Must not delegate to the Agent | Named owner |
|---|---|---|---|
| Payment business owner | Glossary, canonical facts, lifecycle, transition meaning, commit preconditions, and business success evidence. | State meaning, monetary rules, or acceptance of a payment result. | `TBD-business-owner` |
| Tenant policy owner | Role-to-operation policy, approval routing, delegation, limits, and break-glass policy. | Permission, threshold, or approver inference from a query or page. | `TBD-policy-owner` |
| Platform owner | Manifest schema, version validation, registry lifecycle, deterministic contract checks, and evidence envelopes. | Domain facts or a declaration that an external effect succeeded. | `TBD-platform-owner` |
| Integration owner | Adapter/Profile implementation, authoritative read transport, health behavior, and environment isolation. | Reinterpreting the Pack lifecycle or weakening an UNKNOWN result. | `TBD-integration-owner` |
| Data-control owner | Field classification, provider/region allowlists, retention, access policy, and evidence-access audit. | Treating an unclassified field as safe by default. | `TBD-data-owner` |

No Pack may move beyond design review while any required owner is `TBD`. One
person may hold more than one role only when the tenant's separation-of-duties
policy explicitly permits it; the payment requester and business approver
roles remain distinct regardless of administrative ownership.

#### A.3 Pack identity and immutable version bundle

A reviewed Pack version is an immutable bundle of references:

~~~text
Pack identity and ontology version
-> capability definitions and transition catalog
-> canonical-fact schemas
-> semantic resolver contracts
-> policy references
-> Work Order template references
-> result-probe contracts
-> evidence and data-classification references
~~~

The bundle must declare at least:

| Field | Meaning |
|---|---|
| `pack_id`, `pack_version` | Stable Pack identity and immutable semantic version. |
| `lifecycle_state` | Registry state such as design, reviewed, installed-audit, rehearsal-eligible, or retired; it is not `GOVERNANCE_MODE`. |
| `owner_refs` | Named owner records for the roles in A.2. |
| `ontology_ref` | Versioned glossary and payment-object definitions. |
| `canonical_fact_schema_refs` | Per-object and per-capability authoritative fact schemas. |
| `state_machine_ref` | Versioned transition graph and terminal-state declarations. |
| `capability_refs` | Versioned business capabilities; never page buttons or selectors. |
| `policy_refs` | Access, risk, approval, delegation, and data-control policies. |
| `semantic_resolver_refs` | Pure current-observation-to-canonical-fact contracts. |
| `work_order_template_refs` | Browser constraint templates, kept separate from business semantics. |
| `result_probe_refs` | Side-effect-free business-result contracts for state-changing capabilities. |

A TaskContract, Grant, Plan, approval record, Permit, and Attempt must retain
the exact applicable versions rather than resolve mutable `latest` aliases at
execution or recovery time. A change to lifecycle meaning, canonical fact
semantics, transition meaning, or result interpretation requires a new Pack
version. Adapter, workflow-template, and tenant-policy revisions remain
independently versioned when the Pack ontology is unchanged.

#### A.4 Canonical-fact envelope

The platform may standardize the envelope below, while the business owner must
provide the domain-owned body:

| Envelope field | Source and rule |
|---|---|
| `object_type` | Registered Pack ontology, never model-created text. |
| `resource_id` | Trusted scope or authoritative-system lookup. |
| `resource_version` | Authoritative optimistic version, revision, or equivalent freshness token. |
| `tenant_scope_ref` | Trusted organization/department/business-line scope. |
| `authoritative_system_ref` | Registered Integration Adapter/Profile reference. |
| `lifecycle_state` | Value from the reviewed state machine only. |
| `captured_at` | Trusted observation/probe time. |
| `facts_hash` | Keyed binding over the normalized fact snapshot; raw facts follow Package E controls. |
| `domain_facts` | Business-owner-defined, typed facts needed by exactly one capability or transition. |

Every state-changing capability must list the exact canonical facts used for
authorization, approval binding, pre-execution recheck, and result probing.
Page text, coordinates, model conclusions, and Plan values are not canonical
facts by themselves. A `BusinessSemanticResolver` may derive candidate facts
from the current observation for comparison, but cannot turn them into an
authoritative business record or receive the Plan/Grant as a source of truth.

#### A.5 State-machine notation

The production state graph remains owner-supplied. The Pack must express each
transition with this notation before review:

~~~text
transition_id
  capability_ref
  from_states[] -> to_state
  effect: read | internal_write | external_write
  canonical_fact_refs[]
  commit_precondition_refs[]
  access_policy_ref
  risk_policy_ref
  approval_policy_ref | none
  semantic_resolver_ref
  result_probe_ref
  unresolved_disposition: remain_unknown
  atomicity_unit
  partial_result_semantics
  reversibility: owner_declared
~~~

`UNKNOWN` is an ExecutionAttempt outcome, not a payment lifecycle state. If a
browser call becomes ambiguous, the Pack must not advance the business object
to the declared `to_state`. Only authoritative probe evidence may establish
the state that actually exists.

The graph must reject undeclared edges and identify initial, invalid, and
terminal states. Optional cancellation, amendment, reversal, recall, or
approval transitions do not exist merely because another payment system has
them; each requires explicit business-owner declaration and result evidence.
Compensation/reversal is never an automatic technical rollback of the original
Action; it is a new declared business transition with its own authority,
approval, Permit, Attempt, and result proof.

#### A.6 Provisional capability slots

The catalog below defines review slots, not production capability IDs. A slot
is removed when the business owner says the underlying transition does not
exist; it is never silently filled from the synthetic Pack.

| Slot | Proposed effect | Required business-owner input | Required unresolved path |
|---|---|---|---|
| Read/discover a payment object | `read` | Discoverable fields, scope rules, authoritative read source, and freshness semantics. | Missing, stale, conflicting, or denied data returns clarification/deny/UNKNOWN as applicable; never a guessed record. |
| Prepare or update a non-committed object | `internal_write` or owner-declared stronger effect | Editable canonical fields, valid source states, target state, concurrency token, invalid states, and persistence proof. | Transport success without authoritative version/state proof remains UNKNOWN; no blind retry after an ambiguous write. |
| Request an externally visible commitment | `external_write` | Exact commit fields, source and target states, preconditions, approval/SoD policy, idempotency semantics, and authoritative commitment proof. | EXECUTING followed by probe; inconclusive evidence remains UNKNOWN and is never replayed. |
| Amend, cancel, recall, reverse, or externally approve | Undeclared until reviewed | Evidence that the operation exists, who owns it, allowed states, reversibility, SoD, and result proof. | Capability remains absent until all inputs are approved. |

Harness approval is not an Agent business capability. It is a deterministic
disposition and durable pause/resume control. If an external system also has a
business approval transition, that transition requires its own capability,
different-principal rules, Work Order, Permit, and result probe; it must not be
conflated with approval to let the Harness execute an Action.

Every eventual `CapabilityDefinition` must include a stable ID and version,
typed input schema, transition reference, effect class, required canonical
facts, access/risk/approval policy references, semantic resolver reference,
Work Order template reference, result-probe reference, reauthorization
triggers, and whether a declared read-only chain may share an observation.

A batch template does not make multiple external commits atomic. Unless the
Pack and authoritative system define a genuinely atomic batch operation with a
single idempotency/result contract, each item requires its own fact binding,
observation, Permit, Attempt, and result. A partial/mixed batch result remains
UNKNOWN at the aggregate level unless the Pack explicitly models and proves
each per-item terminal result; it is never flattened into overall success or
failure by the Planner.

#### A.7 Result-probe contract

A result probe is a side-effect-free read of business state. It is logically
independent from the browser transport result and returns exactly one of:

- `CONFIRMED`: authoritative evidence proves the declared transition and
  idempotency binding;
- `NOT_CONFIRMED`: authoritative evidence positively proves that the declared
  effect did not occur; absence caused by lag, scope denial, or transport
  failure is not sufficient;
- `UNKNOWN`: evidence is unavailable, stale, conflicting, unauthorized, or
  inconclusive.

Evidence of a partial or different transition is `UNKNOWN` for the declared
transition unless the reviewed state machine/result contract explicitly
represents that exact partial state. It cannot be treated as
`NOT_CONFIRMED` merely because the full expected result is absent.

The probe input must bind the Pack/capability/transition versions, tenant and
resource scope, Attempt ID, idempotency key, expected prior version, and
expected transition. Its output must bind the probe version, authoritative
source, observed resource/version/state, checked time, evidence references,
facts hash plus fingerprint scheme/key version, status, and redacted reasons.
Raw sensitive facts are governed by Package E and are not automatically copied
into Attempt or audit records.

An authoritative API, database read model, or independently governed event
source is preferred. If an external system exposes only a browser UI, the
Domain Pack still may not call Playwright. It may request a fresh, read-only
Skyvern observation through a separately designed adapter path, but DOM or
visual evidence alone defaults to UNKNOWN unless the business and data owners
explicitly approve it as authoritative evidence. Probe retries may repeat only
the read; they can never replay the original business Action.

#### A.8 Reference formats and extension boundaries

References are opaque, immutable identifiers validated by their owning
registries. The design format is:

~~~text
policy://<owner-scope>/<pack-id>/<policy-kind>/<policy-id>@<version>
probe://<pack-id>/<probe-id>@<version>
resolver://<pack-id>/<resolver-id>@<version>
template://<pack-id>/<template-id>@<version>
adapter://<external-system>/<profile-id>@<version>
~~~

They contain no threshold, credential, region secret, or mutable `latest`
value. A tenant policy can vary approval routing without forking the Pack; an
Adapter/Profile can vary page or external-system behavior without changing
business semantics; a workflow template can vary sequencing without defining
new transitions. A new Pack version is required only when the ontology,
canonical facts, lifecycle, risk meaning, or result semantics change.

#### A.9 Package A review evidence and exit criteria

Package A is reviewable when all of the following are true:

1. every A.2 role has a named accountable owner;
2. the glossary, authoritative system, and state graph are supplied or
   explicitly deferred with no affected capability made installable;
3. each state-changing capability has typed canonical facts, commit
   preconditions, effect classification, approval reference, semantic resolver
   reference, and result probe;
4. every probe defines CONFIRMED, NOT_CONFIRMED, and UNKNOWN evidence without
   using browser transport success as business proof;
5. approval control and any external business-approval transition are kept
   separate;
6. Pack, policy, template, adapter, resolver, and probe versions can change
   independently without resolving mutable aliases during a Task;
7. unresolved capabilities are absent or fail closed, and no artifact changes
   the current off/audit runtime boundary.

Meeting these criteria approves only the contract for the named Pack version.
It does not install the Pack, create a production capability, authorize a
synthetic Skyvern rehearsal, or open runtime/enforce work.

### B. RBAC-to-Capability authorization design

Map the existing organization/department/business-line/role context to a
deterministic, multi-axis capability decision. Define treatment of service
principals, revocation, trusted snapshots, administrator/break-glass access,
and reauthorization. The current role ordering (`none < read < operate <
approve`) may remain a legacy UI or coarse-access concept, but it is not a
valid payment-operation authorization algorithm: an approver must not inherit
execution authority merely because `approve` sorts above `operate`.

#### B.1 Independent authorization dimensions

Every `(principal, tenant scope, resource scope, capability, transition)`
decision evaluates these dimensions independently:

| Dimension | Meaning | Must not imply |
|---|---|---|
| `discover` | The capability may be named or shown to this principal. | Record access or authority to request it. |
| `read_record` | The principal may retrieve the declared fields of a scoped business object. | Discovery of other objects or any state transition. |
| `request_transition` | The principal may ask the Harness to evaluate a specific transition. | Authority to execute it or approve it. |
| `execute_transition` | The principal may be the actor for an authorized transition. | Authority to request or adjudicate its approval. |
| `request_approval` | The principal may submit the bound transition for approval. | Authority to approve that request. |
| `adjudicate_approval` | The principal may approve/reject a bound request in an eligible approval role. | Record-wide read or authority to execute the transition. |

No dimension is obtained by numeric comparison with another dimension. A role
mapping is an explicit set of allowed dimensions plus scope and conditions.
The same person may hold multiple enterprise roles, but the effective decision
is still constrained by transition-specific separation of duties, requester /
executor / approver identity rules, and current tenant policy. Denial in any
mandatory check is final; the Agent cannot combine partial grants, roles, or
department memberships to manufacture authority.

Discovery is disclosure-controlled. A denied capability is absent from the
dynamic UI and Planner projection unless policy explicitly permits a generic
"not available" explanation. Read access is field- and purpose-scoped through
Package E; it is not an entitlement to expose every canonical fact to a model.

#### B.2 Deterministic authorization input and decision

The resolver consumes only trusted or registry-owned inputs:

~~~text
authenticated principal + principal type + credential/session assurance
tenant / organization / department / business-line memberships
requested resource scope and authoritative ownership facts
Pack / capability / transition immutable versions
requested authorization dimension and declared purpose
current role, delegation, SoD, risk, and data-policy versions
current time, revocation epoch, and relevant object version/state
~~~

Query text, DOM text, model-selected roles, user-entered tenant identifiers,
and a previous Task snapshot are never authority inputs. For one requested
dimension, the authorization output is a typed `ALLOW`, `DENY`, or
`NEEDS_CLARIFICATION` decision with resolved scope, policy/version references,
conditions, decision time, expiry, revocation epoch, and redacted reason codes.
Separately, a risk/policy decision may return `REQUIRE_APPROVAL` after the
principal is allowed to request the transition. Authority to
`request_approval` and a policy requirement to obtain approval are therefore
not the same result, and neither is execution authority.

For an executable request, the resolver may issue an immutable Capability
Grant containing at least:

| Grant field | Binding rule |
|---|---|
| principal and principal type | Exact authenticated human or service identity; no display-name binding. |
| tenant and resource scope | Intersection of trusted memberships, ownership policy, and requested scope. |
| Pack/capability/transition versions | Exact reviewed versions; no mutable aliases. |
| allowed dimensions | Explicit set from B.1, never a minimum permission level. |
| input and field projection | Allowed typed inputs and field-level disclosure references. |
| policy and SoD references | Versions evaluated when the Grant was issued. |
| validity and revocation epoch | `issued_at`, `not_before`, `expires_at`, and identity/policy revocation markers. |
| conditions | Object state/version, assurance, purpose, approval, and other deterministic predicates. |

A Grant is evidence of a past decision and a bounded Planner input. It is not a
bearer Permit and cannot authorize a browser Action. A later policy change,
role removal, scope loss, credential disablement, delegation expiry, object
version change, or conflicting duty assignment invalidates it.

Interactive Agent execution binds two identities: the initiating/business
principal whose capability and SoD are evaluated, and the service/workload
principal allowed to operate the Harness/Skyvern path on that principal's
behalf. Neither identity substitutes for the other. The Grant and TaskContract
retain both, policy states which principal is the business requester/executor
for SoD, and the workload identity receives only the machine capabilities and
tenant routes needed for the bound Work Order.

#### B.3 Snapshot and reauthorization semantics

Task admission creates a trusted creation snapshot from authenticated context
and the resolver decision in the same admission transaction described in
Package D. The snapshot binds identity, scope, Grant, Pack/capability/policy
versions, revocation epochs, and a hash of applicable canonical facts. It is
immutable audit evidence; it never freezes authority for the life of a Task.

Current authorization is re-evaluated at minimum at:

1. Task admission and Plan validation;
2. approval-request creation;
3. each approval adjudication, including current approver eligibility and SoD;
4. Permit issuance;
5. Permit consumption immediately before an Attempt becomes `AUTHORIZED`;
6. transition from `AUTHORIZED` to `EXECUTING`, including fact/version,
   expiry, revocation-epoch, and worker/lease bindings;
7. resume after approval, recovery, or lease loss, and continuation after an
   `UNKNOWN` investigation has authoritatively resolved the external result;
8. any scope, principal, policy, Pack, capability, resource-version, or
   revocation-epoch mismatch.

Reauthorization uses the current authoritative identity and policy state while
retaining the older versions as evidence. If current policy is incompatible
with the snapshotted Contract, the system invalidates the affected Grant,
Plan, approval disposition, Permit, and pending Action and returns to bounded
replanning or terminal denial; it does not silently execute under the older
policy. When it is compatible, the decision evidence binds both the original
snapshotted references and the exact current versions/epochs re-evaluated.
Reauthorization around `UNKNOWN` never authorizes replay of the original
Action. Revocation propagation has an owner-defined maximum latency, but a
cache outage, stale epoch, or inability to prove freshness fails closed for
state-changing work.

#### B.4 Service principals, administrators, and break-glass

A service principal has a stable workload identity, credential assurance,
explicit tenant/scope assignment, purpose, allowed capabilities/dimensions,
credential expiry, and revocation epoch. It receives no permissions from a
human display role and cannot participate as a business approver unless the
business and policy owners explicitly define a non-human approval role. A
service principal may never satisfy both sides of a requester/approver or
executor/approver SoD rule for the same bound transition.

Platform or tenant administration is not payment authority. Administrators may
manage registries or investigate evidence only through separately authorized
administrative capabilities. "Superuser" status must not bypass tenant scope,
approval, Permit consumption, durable `EXECUTING`, result probing, or UNKNOWN
handling.

Break-glass is absent by default. If later approved, it must be an explicit,
time-limited, purpose-bound capability with strong authentication, a named
incident/reference, independently reviewed activation, tenant notification,
complete evidence, and post-use review. Its policy must state exactly which
authorization dimensions it adds. It cannot allow self-approval, cross-tenant
access, evidence erasure, result assertion, or replay of an ambiguous external
effect.

#### B.5 Package B review evidence and exit criteria

Package B is reviewable only when:

1. an owner-approved role-to-dimension matrix exists for every proposed
   capability and explicitly demonstrates that approvers do not inherit
   execution;
2. tenant/resource scope, field access, service-principal purpose, delegation,
   SoD, and revocation are machine-evaluable without an LLM;
3. the Grant schema and decision reason codes are versioned and disclosure-safe;
4. all reauthorization points have a fail-closed freshness rule and an owner;
5. administrator and any break-glass rules cannot bypass the Harness invariants;
6. snapshot tests prove that creation-time authority does not survive current
   revocation, scope loss, policy incompatibility, or duty conflict.

Meeting these criteria approves an authorization design only. It does not
replace the current resolver, issue a live Grant, or enable enforcement.

### C. Planning and interaction contract

The current free-form Planner and the separate Coordinator/Executor prototype
are not candidates for direct connection to governed execution. The target
Planner produces a proposal over an active Grant projection; it never creates
browser subtasks, selectors, Playwright calls, permissions, or a second
execution loop.

#### C.1 One normalized request for UI and chat

The dynamic UI and natural-language entry normalize into the same
`CapabilityRequest` before planning:

| Field | Rule |
|---|---|
| `request_id`, `submitted_at`, `entry_mode` | Harness-created identity, time, and `ui` / `chat` provenance. |
| `principal_ref`, `session_ref` | Trusted authenticated context, not model or form text. |
| `requested_scope_ref` | Trusted scope selector or resolved reference; never an arbitrary tenant claim. |
| `capability_ref` | Exact member of the active Grant projection. UI supplies an opaque registered ref; chat may only match among supplied refs. |
| `request_kind` | `discover`, `read`, `transition`, `approval_request`, or `approval_adjudication`; validated against the matching Grant dimension and active record. |
| `typed_inputs` | Values validated against the capability input schema with source and sensitivity labels. |
| `resource_refs` | Authoritative IDs or unresolved typed lookup requests; page/model text is not promoted to an ID. |
| `approval_round_ref` | Required only for approval interaction; exact active round, never a model-created or stale display ID. |
| `user_intent_summary` | Untrusted explanatory text retained for clarification/audit, never an authorization field. |
| `grant_ref`, `contract_versions` | Exact resolver and Pack/capability/policy bindings. |

Persistence of request/Plan/Work Order inputs follows Package E. Raw sensitive
values are referenced from an approved encrypted field store or retained only
for the minimum allowed lifetime; audit, fingerprints, previews, and model
artifacts receive only their approved projection/reference and must not gain a
copy simply because the typed schema validated it.

The chat matcher receives only a disclosure-safe `GrantSetProjection`: active
capability references, user-facing descriptions, allowed request kinds, typed
input schemas, and permitted scope/field projections. It must not see denied or
expired capabilities, hidden reason details, raw policy thresholds, unrelated
tenant data, or capabilities from an installed but ungranted Pack.

`approval_request` and `approval_adjudication` are Harness control
interactions, not business capabilities invented by the Planner. They bind an
existing validated Plan/pending approval round and route to the deterministic
approval service in D.4. For chat, the model may match only among the
principal's disclosure-safe eligible approval references and collect a typed
decision/comment; it cannot select the eligible approver, change the bound
facts, or adjudicate on the user's behalf. Material adjudication requires the
authenticated approver's explicit confirmation under the approval policy.
An external system's business-approval operation, if Package A declares one,
remains an ordinary state-changing `transition` capability and is not routed
through this Harness adjudication path.

If no single granted capability matches, multiple materially different matches
remain, a required scope is absent, or requested semantics exceed the Grant,
the outcome is clarification or a typed refusal. The system must not silently
downgrade "commit" to "prepare", reduce an amount, choose another account,
broaden/narrow scope, or substitute a read for the requested transition and
then claim the request was fulfilled.

Submission is idempotent on the trusted request ID and returns a typed envelope
such as `ACCEPTED_FOR_VALIDATION`, `CLARIFICATION_REQUIRED`, `DENIED`,
`CONFLICT_OR_STALE`, or `UNAVAILABLE`, with a correlation/reference and
disclosure-safe reason codes. UI and chat clients display pending/paused/
resuming/unknown states from Harness-owned records; a timeout or lost response
is not success and is reconciled by request ID rather than resubmitted as a new
business request. Client-side loading state, cached Plan text, and optimistic
UI state are never authority or completion evidence.

#### C.2 Structured Planner output and deterministic validation

The Planner returns exactly one typed outcome:

- `CLARIFICATION_REQUIRED` with schema-bound missing/ambiguous fields and
  disclosure-safe options;
- `PLAN_PROPOSAL` with one or more ordered, granted capability invocations;
- `NO_MATCH` when no supplied capability represents the request; or
- `UNSAFE_OR_UNSUPPORTED` when the requested semantics cannot be expressed
  without exceeding the projection.

A `PLAN_PROPOSAL` binds a Plan ID/version, request ID, Grant refs, exact
Pack/capability/transition versions, resolved scope/resource refs, typed inputs
and provenance, canonical-fact requirements, ordered dependencies, expected
effects, approval checkpoints, result-probe refs, and expiry. It may reference
only capability refs in the supplied active projection. It contains no DOM
selectors, coordinates, browser Action list, hidden chain-of-thought, policy
decision, Permit, or claim of business success.

A deterministic Plan Validator, not the model, verifies membership in the
Grant projection, schema validity, scope containment, dependency graph,
effect/approval/probe requirements, version bindings, expiry, and current
authorization. Unknown fields, undeclared transitions, cycles, incompatible
Grants, missing result probes for state-changing work, or scope unions not
represented by one valid Grant fail closed. Only a validated Plan may be used
to derive a TaskContract and bounded Work Orders.

#### C.3 Preview, confirmation, and approval are distinct

Plan preview presents the business capabilities, affected scoped objects,
material typed inputs, expected effects, approval needs, and unresolved-result
behavior in a data-controlled form. User confirmation binds a hash of that
specific Plan version, displayed material fields, principal/session, and an
expiry.

Confirmation proves that the requester accepted the preview. It does not add
a Grant dimension, replace current authorization, satisfy business approval,
consume a Permit, confirm page facts, or prove an external result. A Plan that
does not require confirmation under an owner-approved policy must record that
policy decision rather than synthesize a user confirmation.

#### C.4 Work Order boundary and replanning

For each validated capability invocation, the Harness derives a Work Order
that constrains Skyvern by scope, allowed browser skill/action classes,
observation freshness, canonical-fact comparison requirements, effect class,
and stop conditions. A Work Order is not authored directly by chat and is not
a sequence of browser actions. Skyvern remains responsible for page-local
perception and L0--L2 technical recovery inside those constraints.

L0--L2 recovery may refresh observation, retry a proven read-only operation,
or adapt to page-local drift only within the same Work Order, fact bindings,
and effect boundary. It cannot invoke a new capability, change business input,
select another resource, renew authority, or retry an ambiguous side effect.

A Business Replan is required when capability, scope, resource, material input,
operation order, expected effect, canonical facts, approval path, Pack/policy/
adapter/probe version, or result interpretation would change. It creates a new
Plan version and invalidates affected confirmation, approval disposition,
Permit, Work Order, and pending Action. Completed confirmed steps remain
historical facts; they are neither erased nor replayed. An `UNKNOWN` Attempt
blocks dependent replanning until an owner-authorized investigation/probe
resolves it or terminates the workflow without replay.

Resume after clarification, approval, recovery, or replan always reloads the
current Grant projection, revalidates scope and authorization, and obtains a
fresh observation. Cached Planner text and the prior browser Action are not
resumable authority.

#### C.5 Package C review evidence and exit criteria

Package C is reviewable only when:

1. UI and chat fixtures produce the same normalized request for equivalent
   intent and neither can name a capability outside the active projection;
2. adversarial prompts, hidden/expired Grants, ambiguous matches, and scope
   injection produce clarification/refusal rather than implicit substitution;
3. every Planner outcome and Plan field has a strict schema and deterministic
   validator with reject-by-default unknown-field handling;
4. preview confirmation, business approval, authorization, Permit, and result
   confirmation are represented as distinct records;
5. recovery/Replan tests prove stale Plans, confirmations, approvals, Work
   Orders, Permits, and Actions cannot be reused;
6. the Planner/Coordinator/Executor prototypes cannot become a second browser
   executor or bypass the Package D admission path;
7. duplicate submission, timeout, stale response, denial, pause, resume, and
   UNKNOWN propagate consistently through UI/chat/API/Harness records without
   optimistic success or a second business request.

Meeting these criteria approves an interaction contract only. It does not wire
the Planner, UI, chat, Coordinator, or Executor into live task execution.

### D. Runtime-wiring design

The target runtime has one governed admission boundary and one browser
executor. It does not connect the separate Planner Coordinator/Executor loop or
permit an entry path to create a partially governed Task.

#### D.1 Shared admission boundary

A future `GovernedTaskAdmissionService` (working name) is the sole owner of
admitting a Task that declares a governed Pack/capability. Native Skyvern task
creation, workflow-block task creation, enterprise template routes, SDK/API
task creation, and any direct internal creator must call this service or be
explicitly rejected for governed work. A template route must create a real
admitted Task; returning a generated task ID without admission is not a valid
implementation.

The trusted creation-provenance schema must represent each accepted caller
family without laundering SDK, direct, or future entrypoints into an inaccurate
`native` label. The current native/workflow/template snapshot contract is an
interface foundation; adding an admitted caller requires an additive,
versioned provenance variant plus an inventory test before that caller is
accepted.

The service first resolves the requested operating mode and scope. Until a
separate scoped-enforcement approval exists, `off` and current `audit` behavior
remain as recorded in the master status and `enforce` remains configuration-
rejected. In a future approved scope, the service must reject:

- a governed Pack request that bypasses admission or lacks a reviewed Pack
  lifecycle/version;
- an ungoverned request attempting to reference a governed capability;
- a Task whose scope, principal, Grant, Plan, or versions do not agree;
- a state-changing capability without an explicit approval-policy disposition
  (`required` or reviewed `none`) and a result-probe requirement; or
- an entry path that cannot participate in the atomic admission transaction.

Admission atomically persists the Task, immutable TaskContract, trusted
creation snapshot from B.3, validated Plan reference, initial Work Order
references, mode/scope decision, and an outbox-backed audit event. No Task is
published to a worker before that transaction commits. A partial write is
rolled back and no guessed/reconstructed Contract is accepted later.

The TaskContract binds Task/tenant/principal, exact Pack/capability/transition,
Grant/Plan/policy/template/adapter/resolver/probe versions, requested resource
scope, typed input and canonical-fact hashes, expected effect, confirmation and
approval requirements and any bound confirmation record/hash/expiry, result
semantics, mode, and creation/revocation epochs.
It is owned by the Harness, immutable, and retained as evidence. Replanning
creates a new Contract version linked to its predecessor; it does not mutate
the record under an active Attempt.

#### D.2 One Action admission path

For each Work Order Skyvern performs a fresh observation and proposes a typed
Action candidate. The ActionHandler passes the candidate and observation
binding to the Governance Harness before any state-changing browser transport.
The Harness performs semantic matching, canonical-fact comparison, current
authorization, risk/approval evaluation, effect classification, and selection
of an owner-approved `ExecutionProfile` describing the permitted mechanism and
fallback ceiling. The candidate is either denied, durably paused for approval,
admitted for Permit issuance, or restricted to a declared read-only path.

Read-only and navigation Actions do not inherit authority from being
non-committing. In future enforce they still enter the public governed handler
with a matching authorization/profile or an explicitly designed equivalent
read authorization, fresh observation binding, scope checks, and audit. They
may use a lighter Attempt/result path only when the Pack and handler contract
prove they cannot cross an internal/external-write boundary.

A Permit is single-use and binds TaskContract/Work Order/Plan versions,
principal and scope, capability/transition, observation/action fingerprints,
canonical-fact hash and resource version, effect class, approval round and
disposition, exact ExecutionProfile/fallback rank, required fresh CUA engine
evidence when applicable, policy/revocation epochs, expiry, and an idempotency
key, plus the fingerprint scheme and key version. Coordinate, JavaScript, CUA,
cached, or scripted mechanisms receive no
authority merely because the business capability is allowed; the existing
entrypoint-specific reject/profile constraints remain part of validation. The
Permit is not returned to the Planner or treated as a reusable token.

The idempotency key is derived/validated by the Harness using the
business-owner-defined capability schema and stable tenant/resource/request
bindings; it is never invented or refreshed by the model to evade prior state.
Absence, collision, incompatible reuse, or inability to verify the binding
denies execution. Permit consumption also rejects a revoked/unknown
fingerprint-key version while retaining controlled historical verification.

All authority/expiry checks use trusted server UTC and half-open validity
windows `[not_before, expires_at)`: the exact expiry instant is invalid. Client,
page, model, or worker-local timestamps are evidence only. Any approved clock-
skew tolerance may affect observation freshness policy but cannot extend a
Grant, confirmation, approval, Permit, lease, or egress authorization.

Fallback inside an ActionHandler is permitted only while the previous
mechanism is positively known not to have invoked the side effect. Once a
locator, label, coordinate, JavaScript, CUA, or other mechanism may have
crossed that boundary, an exception or missing response ends the Action as
probe/`UNKNOWN`; the handler cannot try the next fallback under the same or a
new Permit. The weakest permitted profile therefore constrains mechanism
choice but never authorizes duplicate side-effect attempts.

The state-changing call path is ordered as follows:

~~~text
fresh observation + typed Action candidate
-> deterministic Governor and current authorization
-> durable approval disposition when required
-> issue bound one-time Permit
-> atomically recheck current epochs/bindings + consume Permit + create AUTHORIZED Attempt
-> recheck current facts/authority and separately commit EXECUTING with lease/fencing token
-> browser transport outside all database transactions
-> persist transport evidence without declaring business success
-> side-effect-free Result Probe
-> CONFIRMED | FAILED | UNKNOWN
~~~

Permit consumption and creation of the corresponding `AUTHORIZED` Attempt are
one database transaction with uniqueness on Permit and idempotency bindings.
`EXECUTING` is a second short transaction that commits before control crosses
the browser side-effect boundary. The browser call must never run while a
database transaction or row lock remains open. Any implementation that calls
the browser before durable `EXECUTING`, or marks `EXECUTING` only after return,
is rejected.

#### D.3 Result and failure semantics

Browser success means only that the transport/action handler returned. It
defaults to `UNKNOWN` until the exact Package A probe provides authoritative
evidence. Attempt outcomes are:

| Outcome | Minimum evidence | Retry consequence |
|---|---|---|
| `CONFIRMED` | Probe returns `CONFIRMED` for the bound transition/idempotency key. | Dependents may continue after current checks. |
| `FAILED` | Failure occurred before the side-effect boundary, or the authoritative probe positively returns `NOT_CONFIRMED` with evidence that the effect did not occur. | A new attempt may be considered only through policy, fresh observation, authorization, and Permit. |
| `UNKNOWN` | Browser was or may have been invoked and the probe is unavailable, stale, conflicting, denied, or inconclusive. | Stop; probe/investigate only. Never replay the state-changing Action automatically. |

The target state graph must explicitly allow an `AUTHORIZED` Attempt that
fails before `EXECUTING` to close as a pre-boundary `FAILED` (or an equivalently
named terminal safe-abort state), without pretending it reached the browser.
It must keep that transition distinct from `EXECUTING -> FAILED`, which is
legal only with authoritative proof that the external effect did not occur.

A timeout, worker crash, lost response, lease loss, database outage after
`EXECUTING`, model/browser exception, or transport failure is not proof that an
external effect failed. Those paths become `UNKNOWN` unless an authoritative
probe positively establishes `CONFIRMED` or `NOT_CONFIRMED`. Internal errors
before durable `EXECUTING` may become `FAILED`/denied because the browser
boundary was not crossed; the audit record must retain that distinction.

Result writes compare the active Attempt sequence and fencing token. A stale
worker may append quarantined diagnostic evidence but cannot change the
authoritative outcome, consume another Permit, or advance the Task. Probe
retries are read-only, independently rate-limited, and retain every observation
rather than overwriting conflicting evidence.

Where legally and operationally permitted, the probe runs as a narrowly scoped
reconciliation workload identity bound to the tenant, capability, Attempt, and
read-only purpose rather than borrowing the requester's expiring Grant. Loss of
the requester's execution authority still prevents new Actions, but does not by
itself erase the Harness's duty to establish the result of an effect already in
flight. If current reconciliation authority cannot be proven, the outcome
remains `UNKNOWN` and the access failure is audited.

#### D.4 Durable approval, resume, and invalidation

An approval request is a durable, versioned round binding requester, scope,
TaskContract/Plan/Work Order, capability/transition, material input and fact
hashes, risk/policy versions, eligible approver policy, expiry, and redacted
preview evidence. Pausing commits the approval record and Task pause state
together before notifying an approver through an outbox. Notifications are not
the approval source of truth.

Approval adjudication locks the active round, rechecks approver identity,
eligibility, current policy, scope, expiry, SoD, and the bound facts, and records
exactly one effective disposition. Concurrent, duplicate, stale-round, or
self-conflicting adjudications cannot resume execution. Approval does not issue
a Permit inside the adjudication transaction.

Rejection or expiry atomically closes/invalidates the pending Action and moves
the affected Task/Step to the owner-defined stopped/denied or `NEEDS_HUMAN`
state; it never becomes `RESUMING`. An approved disposition remains paused
until the separate resume service commits invalidation of the old Action and
the Task/Step `RESUMING` state. Queue or notification delivery alone cannot
perform either transition.

Resume is owned by a Harness resume/recovery service. It begins only from a
committed eligible disposition, then rechecks current authorization and facts,
invalidates the pre-pause Action and observation, and schedules a fresh Work
Order observation. A new Action and Permit are required. Recovery after worker
loss, lease expiry, or service restart follows that rule only when durable state
proves the side-effect boundary was not crossed. If an Attempt is `EXECUTING`
or the boundary state is uncertain, recovery is probe/reconciliation-only and
cannot schedule a replacement Action. Serialized model output, cached DOM, and
an old Permit are never resumed.

Revocation, scope/role/policy change, Pack retirement, Contract mismatch,
resource-version drift, changed material input, replan, approval expiry, or
lease loss invalidates all affected pending Actions and unconsumed Permits.
Consumed Permits remain immutable evidence. An already `EXECUTING` Attempt is
not declared cancelled: it proceeds to probe and may become `UNKNOWN`.

#### D.5 Transaction, lease, and recovery ownership

| Transition / invariant | Transaction owner | Failure disposition | Recovery owner |
|---|---|---|---|
| request -> Task + snapshot + Contract + audit outbox | Admission service | Atomic rollback; no runnable Task | Entry caller may submit a new request after correction; never reconstruct partial state. |
| candidate -> approval pause/request | Governor/approval orchestration service | Durable pause or deterministic denial | Approval recovery scheduler reconciles outbox/pause state only. |
| approval adjudication | Approval service | No effective disposition on rollback; duplicates/stale rounds rejected | Approval recovery service using the same active-round checks. |
| rejection/expiry -> stopped Task/Step + invalidated pending Action | Approval service/sweeper | Remains durably paused if atomic close fails | Approval recovery service; never resume a rejected/expired round. |
| approved round -> invalidate old Action + `RESUMING` + audit outbox | Resume service | Remains durably approved/paused if atomic transition fails | Resume recovery scan with active-round/CAS checks. |
| committed `RESUMING` -> reauthorization + fresh Work Order observation | Recovery scheduler/Harness coordinator | Remains `RESUMING`; no old payload execution | Idempotent scheduler claim with lease/fence. |
| Permit issue | Permit service | No Permit on rollback; stale authorization denies | Fresh Governor evaluation. |
| current epoch/binding check + Permit consume + `AUTHORIZED` Attempt | Execution-admission service | Atomic rollback leaves Permit unconsumed and no Attempt | Fresh admission may retry only before `EXECUTING`, with same bindings and idempotency checks. |
| current fact/authority check + `AUTHORIZED` -> `EXECUTING` + lease/fence + audit outbox | Attempt service | Safe pre-boundary failure; no browser call unless commit is acknowledged | Attempt recovery service; uncertain commit is reconciled from DB before any action. |
| browser transport | Skyvern ActionHandler, outside DB transaction | Exception/timeout after `EXECUTING` => probe then usually `UNKNOWN` | Probe/reconciliation service, never blind browser retry. |
| probe -> terminal Attempt outcome | Result service with compare-and-set fence | Probe failure/conflict => `UNKNOWN` | Domain-owned probe scheduler/investigation owner. |
| terminal Attempt -> dependent workflow state | Harness coordinator after result commit | Task remains stopped if transition cannot commit | Workflow recovery reloads terminal evidence; does not repeat the browser effect. |

All workers use bounded leases plus monotonically increasing fencing tokens or
equivalent compare-and-set semantics. Unique constraints cover one active
Attempt per action/idempotency binding, one Permit consumption, and one active
approval round. Tenant and immutable capability namespace are included in
repository predicates, uniqueness/fencing keys, and evidence correlation so a
collision or lookup cannot cross tenant boundaries. The database is the state
authority; queue delivery, pub/sub,
and worker memory are hints. State changes and audit publication use a
transactional outbox or equivalent atomic mechanism.

In future scoped enforcement, loss of the persistence, authorization,
revocation, approval, Permit, or audit-outbox authority fails closed before a
new external side effect. A downstream audit-sink outage may be buffered only
while the atomic outbox remains durable, policy-compliant, and below its
approved capacity/age limit; loss or exhaustion of that durable buffer fails
closed. Failure of the result-probe service after `EXECUTING` produces
`UNKNOWN`, not success and not an automatic rollback.

#### D.6 Package D review evidence and exit criteria

Package D is reviewable only when:

1. an inventory test proves every native, workflow, template, SDK/API, script,
   cache, CUA, and direct-action path is routed through or rejected by the
   relevant admission/Action boundary;
2. Task, snapshot, Contract, and initial evidence are atomically created;
3. Permit consumption + `AUTHORIZED` and pre-browser `EXECUTING` ordering are
   proven under duplicate delivery, crash, timeout, and database uncertainty;
4. each state transition has the transaction, lease/fence, failure, audit, and
   recovery owner specified above;
5. approval/recovery always invalidates the prior observation, Action, and
   Permit and performs current authorization;
6. every post-`EXECUTING` ambiguous path stops at probe/`UNKNOWN` with no action
   replay;
7. `off`/`audit` compatibility and future tenant/Pack/workflow-scoped rollout
   can be tested without enabling global enforce.

Meeting these criteria approves a wiring design only. It does not authorize
implementation, migration, connection to Skyvern, or `enforce` configuration.

### E. Data-control and evidence design

Data locality is not data governance. The target controls data before it enters
a prompt, model request, screenshot, DOM artifact, log, metric, trace, queue,
audit event, or support/export path. The current heuristic classification,
local shadow egress decision, redacted Action record, and localhost-only
synthetic collector are foundations or test evidence only; they do not enforce
production model egress.

#### E.1 Classification and policy decision

Classification has two independent axes:

- a sensitivity tier such as `PUBLIC`, `INTERNAL`, `CONFIDENTIAL`, or
  `RESTRICTED`; and
- one or more semantic categories such as `PII`, `FINANCIAL`, `CREDENTIAL`,
  `OTP`, or an owner-defined regulated/business category.

The existing `DataClassification` values are a heuristic category foundation,
not an approved ordinal sensitivity model. A reviewed migration/mapping must
preserve their distinctions rather than collapsing PII, financial, credential,
and OTP into one generic tier. The payment/data owner assigns every canonical
field and derived artifact both axes, purpose, allowed transformations,
retention class, and evidence treatment. This Proposal does not classify or
map real payment fields.

Unknown tier, unknown category, mixed-with-unknown, classifier unavailable, or
unverifiable transformation is treated as `RESTRICTED` plus an unknown category
and denied from model egress by default. Derived data inherits the highest
source tier and union of source categories unless an owner-approved
deterministic transformation policy explicitly changes both. Redaction,
tokenization, hashing, cropping, OCR, summarization, or encryption do not by
themselves change either axis.

`CREDENTIAL` and `OTP` have no model-egress allowlist by default. Any proposed
exception must name the exact field, purpose, deployment, transform, retention,
threat analysis, and security/data-owner approval; enabling another category
or sensitivity tier does not implicitly enable them.

Every egress decision evaluates this immutable tuple:

~~~text
tenant + Pack/capability/purpose + field/artifact tiers and categories
provider + immutable model/deployment version + region
requested transforms + retention/training contract
principal/workload + TaskContract + policy version + decision time
~~~

The outcome is `ALLOW_TRANSFORMED`, `ALLOW_LOCAL_ONLY`, or `DENY`, with exact
field/artifact projections and redacted reason codes. There is no broad
"approved provider" bypass: provider, model class/version, region, purpose,
tenant, tier, every category, and transformation must all match an allowlist
entry. Mutable model aliases are not accepted unless the gateway resolves and
binds an allowlisted immutable deployment/version before evaluating the exact
payload.
"Model class" covers text/vision generation, OCR, embedding, reranking,
moderation/classification, provider-side cache, and any other inference or
hosted preprocessing route; calling a service a utility does not exempt it.
Provider contractual controls must cover no training, approved retention or
zero-retention behavior, subprocessors, region, access, deletion, and incident
handling before any relevant allowlist is reviewable.

#### E.2 Enforcement at collection and model boundaries

Controls execute at both artifact collection and the actual provider request
boundary, not merely as a shadow log. The model gateway accepts a signed,
versioned egress decision bound to the exact payload digest and rejects a
missing, expired, mismatched, unknown, or deny decision. Direct provider SDK,
fallback model, telemetry, retry, and support-export paths must be sealed or
disabled for governed work.

DOM handling performs allowlisted field/region extraction, removes hidden
content, script/style, secrets, credentials, tokens, unrelated tenant/customer
records, and disallowed attributes before serialization. Screenshot handling
uses local, deterministic region selection and masking before encoding; full
raw frames are not sent merely because the visible target is allowed. OCR or
vision-derived text is reclassified before reuse. Prompt construction uses the
minimum approved field projection and labels system, user, page, and retrieved
content by source so untrusted page text cannot alter egress policy.

If safe preprocessing or classification cannot be completed, the model call is
denied. A state-changing Work Order stops before action selection or moves to a
separately authorized human/local-only path; it does not fall back to another
provider, a less controlled model, a raw screenshot, or a larger DOM capture.
Provider/network failure likewise does not weaken policy.

Raw artifacts, transformed payloads, prompt/response content, refusal reasons,
and model-derived facts each have separate storage decisions. Logs, metrics,
traces, exception messages, queue payloads, approval notifications, and audit
events use structured allowlists and stable references rather than accidental
raw object/string serialization.

#### E.3 Fingerprints and key management

HMAC fingerprints bind normalized evidence without treating the digest as
encryption or anonymization. Inputs use a versioned canonical encoding and
domain separation for tenant, artifact type, purpose, Pack/capability version,
and field name so the same value cannot be correlated across unauthorized
domains.

Keys reside in an approved KMS/HSM boundary and are referenced by opaque key
ID/version. Policy defines tenant isolation, service access, generation,
activation, rotation, overlap for verification, revocation, compromise
response, backup, and destruction. Raw keys never appear in configuration,
database rows, logs, or model payloads. Rotation creates new fingerprints for
new evidence while retaining controlled verification of historical evidence;
it never silently rewrites an Attempt. Low-entropy values require additional
domain binding or must not be exposed as reusable fingerprints because HMAC
does not prevent authorized-key enumeration.

#### E.4 Evidence lifecycle and access

An evidence manifest separates metadata, redacted operational events, approval
records, Permit/Attempt state, transformed model artifacts, raw browser
artifacts, and authoritative probe references. For every category the data
owner must specify purpose, class, storage region, encryption, retention start,
retention duration, deletion method, legal-hold behavior, exportability, and
eligible access roles. Evidence retention is not automatically identical to
business-record retention.

The manifest binds schema and canonicalization version, tenant/scope,
correlation sequence, artifact digests and key versions, producer identity,
trusted time, and predecessor/collection integrity where required. Owner-
approved append-only or WORM controls may protect retained categories, but an
HMAC or storage flag alone is not described as non-repudiation or compliance
proof. Integrity verification failures quarantine evidence and cannot be used
to confirm a business result.

Evidence access is a separate capability with least-privilege field projection,
purpose, case/reference, strong authentication where required, and immutable
audit of requester, decision, exact object/field set, time, export destination,
and outcome. Platform administrators do not receive raw evidence by default.
Bulk search/export, support access, cross-region transfer, legal hold, hold
release, deletion, and cryptographic erasure require explicit workflows and
SoD defined by the data/legal owners.

Deletion removes all policy-covered primary, index, cache, derived, and queued
copies subject to documented backup and legal-hold exceptions; it emits a
non-sensitive deletion proof. Export preserves classification labels, scope,
integrity references, and access audit. A legal hold prevents deletion only for
the named scope and does not authorize broader reading or model egress.

#### E.5 Package E review evidence and exit criteria

Package E is reviewable only when:

1. every candidate payment field/artifact and derived form has an owner,
   classification, purpose, transform, destination, retention, and access rule;
2. a matrix identifies the exact provider/model/version/region/purpose tuple
   permitted for each projected class, including contractual controls;
3. enforcement tests prove DOM, screenshot, prompt, response, logs, traces,
   retries, SDK fallbacks, and exports cannot bypass an egress denial;
4. unknown classification, stale policy, unavailable classifier/KMS/gateway,
   payload-digest mismatch, and provider fallback all fail closed;
5. HMAC rotation/compromise tests preserve historical verification without
   exposing keys or allowing unauthorized cross-domain correlation;
6. retention, legal hold, access, export, and deletion paths produce auditable,
   disclosure-safe evidence and cover derived/cached copies;
7. browser and probe evidence can support `CONFIRMED`/`FAILED`/`UNKNOWN`
   semantics without copying raw sensitive facts into every audit record.

Meeting these criteria approves a data-control design only. It does not approve
a provider, payment dataset, model-egress route, retention schedule, or
production evidence claim.

### F. Validation and rehearsal design

Validation proceeds from pure contract tests to repository/transaction tests,
fault injection, browser E2E, replay, and finally an explicitly authorized
synthetic rehearsal. Passing a lower layer never waives a higher layer and no
test may use a real payment identity, record, credential, or endpoint.

#### F.1 Rehearsal environment and authority boundary

The proposed environment contains:

- a dedicated synthetic tenant and identities with requester, executor,
  approver, investigator, data-auditor, and service-principal roles;
- a separately versioned, permanently non-production synthetic Pack and
  policy set whose namespace cannot be installed for a production tenant;
- a reversible browser application and isolated synthetic system of record
  with deterministic idempotency, version, state, and fault controls;
- Skyvern as the only browser executor; no Planner-controlled Playwright,
  test-only direct browser side effect, or second Agent loop;
- an authoritative, side-effect-free probe over a read interface independent
  from the browser action transport, with controllable lag/unavailability/
  conflict behavior;
- isolated model/data routes with synthetic-only classifications, egress
  allow/deny fixtures, dedicated credentials/keys, and disposable storage; and
- a tenant/Pack/workflow-scoped mode switch and a rehearsed rollback path that
  cannot make `enforce` globally available.

The existing offline `synthetic.payment` evidence may supply contract fixtures,
but it is not silently promoted into this environment or connected to Skyvern.
The environment, Pack/version, workflow, identities, data routes, exact test
window, stop authority, cleanup, and rollback owner require a separate written
authorization after A--F design review.

#### F.2 Required validation layers

| Layer | Required proof |
|---|---|
| Contract/property tests | Immutable version/reference validation; deny-by-default schemas; illegal payment-state edges; independent RBAC dimensions; Plan membership; Permit single use; outcome invariants. |
| Repository/transaction tests | Atomic admission, approval rounds, Permit consumption + `AUTHORIZED`, pre-browser `EXECUTING`, fencing/CAS, outbox delivery, idempotency, and current-authorization checks against the supported database. |
| Fault replay | Deterministic replay of every crash boundary and service outage with expected Attempt/Task state and no duplicate external effect. |
| Browser E2E | Real read-only and synthetic state-changing Skyvern paths with fresh observations, page drift, typed Actions, egress controls, result probing, approval pause/resume, and entrypoint coverage. |
| Migration/compatibility | Forward migration on a disposable production-like database, mixed-version/read compatibility if supported, restart/recovery, and evidence preservation. |
| Scoped rollback rehearsal | Stop new admissions/Permits, drain or fence work, reconcile every `EXECUTING` Attempt, transition `enforce -> audit -> off`, and demonstrate no ambiguous replay. |
| Evidence review | Independent reviewers can reconstruct authorization, approval, Permit, Attempt, browser boundary, probe, data-egress, and rollback decisions from redacted evidence. |

SQLite or mocked repositories may support fast contract tests but cannot be the
only evidence for transaction isolation, uniqueness, locking, migrations, or
lease/fencing behavior intended for PostgreSQL.

#### F.3 Fault and adversarial matrix

At minimum, the suite covers these cases and their duplicate-delivery variants:

| Area | Injection / race | Required assertion |
|---|---|---|
| Admission | failure after any Task/snapshot/Contract/outbox write | All admission records commit together or none exist; no worker sees a partial Task. |
| Authorization | role/scope revoked or policy/epoch changes after Plan, confirmation, approval, or Permit issue | Current check denies; affected artifacts invalidate; no browser effect. |
| SoD | approver also requester/executor, inherited `approve` role, stale delegation, two concurrent approvers | Explicit matrix/active round decides; no privilege inheritance or double resume. |
| Permit | duplicate issue/consume, expiry, observation/action mismatch, stale policy/facts, queue redelivery | At most one bound consumption/Attempt; mismatches deny before browser. |
| Attempt | crash before/after `AUTHORIZED`, during `EXECUTING` commit acknowledgement, or stale worker resumes | Browser is called only after confirmed durable `EXECUTING`; fence prevents stale outcome writes. |
| Browser | crash before click, during transport, after synthetic commit before response, lost response, timeout | Post-`EXECUTING` ambiguity invokes probe and stops at `UNKNOWN` when inconclusive; no blind retry. |
| Atomicity | synthetic partial/mixed batch result or a result different from the declared transition | No aggregate success/failure is invented; per-item contracts resolve independently or the aggregate remains `UNKNOWN`. |
| Probe | lag, outage, stale version, scope denial, conflicting records, false absence | Only authoritative positive evidence yields `CONFIRMED`/`FAILED`; otherwise `UNKNOWN`. |
| Approval/recovery | pause crash, notification duplicate/loss, adjudication race, restart, approval expires while paused | Durable state/outbox recover; old observation/Action/Permit never resumes. |
| Replan | material input/scope/order/version changes after preview or approval | New Plan/Contract version; affected confirmation/approval/Permit/Action invalidated. |
| Page behavior | locator/DOM/visual drift, prompt injection, new modal, stale cache/script, CUA fallback, and failure after a fallback mechanism may have acted | Work Order/effect boundary holds; fresh semantic validation occurs; undeclared paths deny; no later mechanism retries an ambiguous side effect. |
| Entrypoints | native, workflow, template, SDK/API, generated script, action cache, CUA, and direct action | Every state-changing path reaches the same admission/Permit/Attempt invariants or is rejected. |
| Data | unknown field, mask/classifier/KMS outage, payload digest mismatch, disallowed region/model, SDK fallback, log/export leak attempt | Actual egress/storage boundary blocks; no raw fallback or silent provider substitution. |
| Services | database, queue, pub/sub, audit outbox, policy, revocation, approval, Permit, model, browser, probe outage | Pre-effect authority loss fails closed; post-effect probe loss becomes `UNKNOWN`; recovery is owner-specific. |
| Rollback | mode change with queued, paused, `AUTHORIZED`, `EXECUTING`, and `UNKNOWN` work | New effects stop; in-flight states are fenced/reconciled; evidence remains readable; nothing ambiguous replays. |

Each test records fixture/version, seed, preconditions, injection point,
expected database and external state, actual browser-call count, probe evidence,
audit correlation IDs, and cleanup result. A test that cannot distinguish zero,
one, and duplicate synthetic commits is not valid side-effect evidence.

#### F.4 Hard safety metrics and review thresholds

For the complete approved scenario inventory, the following are release-blocking
invariants rather than averages:

| Metric | Required result |
|---|---|
| Unpermitted state-changing browser actions | `0` |
| Duplicate synthetic external commits per idempotency binding | `0` |
| State-changing browser calls before durable `EXECUTING` | `0` |
| Automatic state-changing replays after `UNKNOWN` | `0` |
| Inconclusive post-effect cases that stop at `UNKNOWN` | `100%` |
| Denied/unknown model-egress attempts blocked at the actual boundary | `100%` |
| New state-changing Attempts reaching `AUTHORIZED`/`EXECUTING` while required enforce authority or durable persistence is unavailable | `0` |
| Stale approval/Permit/Action accepted after invalidation | `0` |
| Rehearsal records using non-synthetic identity/data/endpoint | `0` |
| `EXECUTING` Attempts unaccounted for after recovery/rollback | `0` |

The scenario inventory and denominators are versioned before the run; skipped,
quarantined, flaky, or unobservable cases do not count as passes. Performance,
latency, and availability targets require owner-supplied thresholds, but they
cannot trade off the hard safety metrics. Results include negative controls
that intentionally remove each guard to show that the assertion detects the
failure rather than merely exercising a happy path.

#### F.5 Migration and rollback semantics

Runtime rollback and schema rollback are separate decisions. The required
runtime direction is scoped `enforce -> audit -> off`: stop new governed
admissions and Permit issuance, fence workers, allow no ambiguous action replay,
probe/reconcile every existing `EXECUTING` or `UNKNOWN` Attempt, and preserve
contracts, approvals, Permits, Attempts, audit/outbox, egress, and probe
evidence for their governed retention period.

The mode recorded by an admitted governed Task never downgrades into permission
to use the current Phase 1 path. During rollback, queued, paused, `AUTHORIZED`,
and replannable governed Tasks are stopped/quarantined or explicitly cancelled
before the scoped router enters audit/off; `EXECUTING`/`UNKNOWN` work remains
probe-only. Audit/off may retain their ordinary behavior only for separately
classified work allowed by the rollback policy. Restart, queue redelivery, and
manual resume must enforce this no-fall-through rule.

Runtime rollback does not delete governance rows, mark external effects
failed, initiate payment compensation/reversal, or authorize old binaries to
ignore new records. Any compensation is a separately authorized business
capability. Database downgrade is not part of the emergency mode switch. Any destructive/down migration requires
separate backup/restore, compatibility, retention, and evidence-integrity
review; an irreversible migration must be declared before approval. A forward
fix is preferred when downgrade would lose evidence.

The rehearsal must cover restart on the exact supported database, migration
failure before/after commit, mixed application versions only if deployment
claims to support them, outbox backlog, and restoration of read-only evidence
access after mode rollback. Cleanup removes only the authorized disposable
environment after verifying required rehearsal evidence retention.

#### F.6 Package F exit criteria

Package F is reviewable only when:

1. A--E contracts and owner-supplied synthetic fixtures are version-frozen for
   the run, with no unresolved safety behavior hidden in test code;
2. the environment proves synthetic-only isolation and Skyvern-only browser
   execution, and identifies an independent authoritative probe;
3. every row of F.3 is mapped to automated tests or an explicitly blocking
   gap, with all F.4 hard metrics measured from a frozen inventory;
4. PostgreSQL transaction/migration, browser E2E, service-fault, data-egress,
   entrypoint, approval/SoD, and rollback evidence is independently reviewed;
5. all `EXECUTING`/`UNKNOWN` records and synthetic external effects reconcile,
   with no unexplained call or commit;
6. the report is labelled synthetic engineering evidence and names failures,
   exclusions, residual risks, cleanup, and rollback results;
7. a subsequent review explicitly decides whether any narrower implementation
   or rehearsal slice may start; no result auto-promotes a production Pack or
   enables global enforcement.

Meeting these criteria completes the rehearsal design. It does not authorize
running the rehearsal, changing configuration, installing a Pack, using real
payment data, or claiming compliance/production readiness.

## 9. Synthetic Skyvern-integrated rehearsal

The existing synthetic.payment harness is isolated from Skyvern and is useful
reference evidence only. Package F defines the design for a future synthetic
Skyvern-integrated rehearsal. That rehearsal is recommended before any
production Pack wiring, but reviewing this Proposal does not authorize it. It
requires a separate, time-bounded execution authorization after packages A
through F have been reviewed and their required owners/fixtures are named.

Its proposed constraints are:

- synthetic identities, facts, policy, and system of record only;
- a dedicated non-production tenant, workflow, and Pack namespace;
- no real bank, ERP, customer, or financial dataset;
- no global availability of GOVERNANCE_MODE=enforce;
- every external-effect analogue remains reversible or contained in the
  synthetic system;
- assertions prove no stale action replay after UNKNOWN and a rollback to
  audit/off retains evidence.

The authorization record must identify the exact synthetic tenant, Pack and
policy versions, workflow, identities, endpoints, provider/data routes,
scenario inventory, run window, operator, stop authority, persistence and
probe owners, cleanup scope, evidence retention, and rollback command/owner.
Any mismatch stops admission rather than widening the authorization.

Success in this rehearsal is engineering evidence for a later review. It is
not a compliance conclusion and does not authorize a production payment Pack.

## 10. Enforce decision gates

No production or Skyvern-integrated enforcement work may begin before a named
Pack, tenant, and workflow satisfy an explicit review of all seven existing
master-status gates:

1. side-effect entry points are sealed or disabled for governed work;
2. the Pack has canonical facts, transitions, authorization scope, approval
   rules, and a result probe;
3. Governor to Permit to ActionHandler wiring enforces one state-changing
   action per observation and never tries a later fallback after an ambiguous
   side-effect mechanism;
4. AUTHORIZED to EXECUTING to CONFIRMED/UNKNOWN/FAILED has fault coverage and
   UNKNOWN never automatically replays, while FAILED has positive no-effect
   evidence;
5. current authorization, separation of duties, revocation, and identity
   snapshots are revalidated at relevant transitions;
6. egress, retention, access, masking, and fingerprint controls are tested at
   actual provider/storage boundaries with unknown classification denied;
7. isolated enforce-to-audit-to-off rollback preserves auditable evidence and
   cannot fall an admitted governed Task through to Phase 1.

An approval must be scoped to one Pack version, tenant, workflow, policy
version, and rollback path. It must not be interpreted as platform-wide
authorization.

## 11. Decisions and unresolved questions

### Decisions recorded here

| ID | Decision |
|---|---|
| D1 | Payment Operations is FinRPA's reference domain for the Pack SDK and Conformance Kit, not an in-repository production-candidate deliverable. |
| D2 | RBAC is the authority root; Capability Grants translate scope into permitted business operations. |
| D3 | UI and natural language are equivalent entry modes over the same Grant set. |
| D4 | Skyvern remains the sole browser executor; Planner and Coordinator never operate Playwright. |
| D5 | Domain expansion is vertical-first; adapters, templates, and policies are preferred over unnecessary new Packs. |
| D6 | Current mode remains off/audit; this Proposal does not approve enforcement work. |
| D7 | Package A freezes a Payment Operations contract shape only; owner-supplied production values remain unresolved and cannot be inferred from `synthetic.payment`. |
| D8 | Harness approval is a governance disposition, not an Agent capability; an external business-approval transition, if one exists, is a separate capability with its own separation-of-duties and result proof. |
| D9 | A business result probe is side-effect-free and logically independent from browser transport success; inconclusive or non-authoritative evidence remains UNKNOWN. |
| D10 | UNKNOWN is an ExecutionAttempt outcome, not a payment lifecycle state, and it never advances the domain state or authorizes replay. |
| D11 | Pack, capability, policy, template, adapter, semantic-resolver, and result-probe versions are independently immutable and are snapshotted rather than resolved through mutable aliases during a Task. |
| D12 | Payment authorization uses independent discover/read/request/execute/request-approval/adjudicate dimensions; the legacy numeric role ordering cannot grant an approver inherited execution authority. |
| D13 | A Capability Grant is a bounded, revocable Planner projection and evidence of a past decision, not a bearer Permit; current authorization is rechecked at every material transition. |
| D14 | UI and chat normalize into one CapabilityRequest; the Planner may propose only projected capabilities, while deterministic validation, preview confirmation, business approval, and execution authorization remain separate. |
| D15 | All governed Task entrypoints share one atomic admission boundary; Permit consumption plus `AUTHORIZED` is atomic, `EXECUTING` commits before state-changing browser transport, and all browser work runs outside database transactions. |
| D16 | After `EXECUTING` is durably committed, any transport error is treated as possibly post-effect and defaults to UNKNOWN; FAILED requires proof that the effect was not invoked or authoritative `NOT_CONFIRMED` evidence. |
| D17 | Unknown/unverifiable data classification fails closed, and egress policy is enforced against the exact payload at the actual provider boundary rather than only recorded in shadow audit. |
| D18 | A Skyvern-integrated rehearsal is synthetic-only, tenant/Pack/workflow-scoped, independently authorized, and measured by zero-tolerance side-effect/duplicate/replay invariants. |
| D19 | Runtime rollback is directional `enforce -> audit -> off`, preserves governance evidence, and cannot fall an admitted governed Task through to Phase 1; database downgrade is a separate, explicitly reviewed operation. |
| D20 | Interactive execution binds both the accountable business principal and the workload/service principal; neither identity substitutes for the other's capability, scope, or SoD checks. |
| D21 | Data policy uses independent sensitivity-tier and semantic-category axes, preserving PII/financial/credential/OTP distinctions; unknown values are restricted and denied by default. |

### Questions that require named owners

An unresolved item blocks only the package or capability that needs it. It does
not block source-free SDK, Contract Catalog, synthetic reference, or
Conformance Kit work when that work rejects unresolved values and opens no
runtime path. Writing `deferred` does not remove the block from an external
Pack installation or any live capability.

| ID | Required decision / artifact | Accountable owner required | Blocks |
|---|---|---|---|
| Q1 | Payment glossary, authoritative system, canonical facts, lifecycle/terminal states, transition meaning, commit preconditions, and result evidence. | Named payment business owner | Production-candidate Pack contract and any matching rehearsal fixture. |
| Q2 | Explicit role-to-dimension matrix, requester/executor/approver SoD, delegation, service-principal eligibility, administrator policy, and whether break-glass exists. | Named tenant-policy owner with business-owner sign-off | Package B and any state-changing capability. |
| Q3 | Identity/policy/revocation authorities, freshness/propagation limits, cache behavior, and owners for authorization checks at every B.3 transition. | Named identity/security owner and platform owner | Grant issuance, approval, Permit, resume, and enforce readiness. |
| Q4 | First candidate capability and why its synthetic external-effect analogue, approval path, idempotency, and probe are sufficient and safe. | Payment business owner, risk owner, and rehearsal owner | State-changing rehearsal authorization. |
| Q5 | Isolated non-production browser application/system of record, independent read-only authoritative probe, integration credentials, fault controls, and cleanup owner. | Named integration owner | Packages A/D/F and integrated rehearsal. |
| Q6 | Field/artifact classifications, transforms, provider/model/version/region/purpose allowlist, contractual no-training/retention terms, evidence retention/access/legal-hold/export/delete policy. | Named data-control owner, security owner, and legal/compliance owner as applicable | Package E, model egress, and evidence approval. |
| Q7 | Shared admission and runtime transaction owner, supported database/isolation/lease-fencing design, entrypoint inventory owner, and outbox/recovery operations. | Named platform/runtime owner | Package D implementation design and fault rehearsal. |
| Q8 | Deployment scope, migration compatibility, `enforce -> audit -> off` procedure, schema recovery position, stop authority, and on-call ownership. | Named release/SRE owner and data owner | Package F rehearsal and every later rollout. |
| Q9 | Which external Pack, tenant, workflow, or real enforce-wiring slice, if any, is proposed after the framework foundation. | Phase 2 program owner | Any external Pack implementation, migration, integration, rehearsal, or runtime execution. |
| Q10 | Named people for business, tenant-policy, platform/runtime, identity/security, integration/probe, data-control, risk, legal/compliance, rehearsal, and release/SRE roles, including allowed role combinations. | External Pack sponsor | Any external Pack installation, runtime exposure, rehearsal, or enforce authorization. |

## 12. Approval protocol

This document begins in Draft / Discussion. Approval requires a review that
records an outcome for every A--F exit criterion and every question in Section
11. A safety-critical unresolved item cannot be accepted by a generic risk
statement; the affected capability or slice remains absent.

The signed decision record must state:

1. reviewer names, owner roles, date, and `APPROVE`, `REVISE`, or `REJECT`;
2. the exact Proposal revision and immutable Pack/policy/interface versions in
   scope;
3. resolved questions, explicitly blocked/deferred items, assumptions, and
   residual risks with accountable owners and due dates;
4. whether the authorization is design-only, interface-only implementation,
   synthetic fixture construction, synthetic rehearsal execution, production
   Pack construction, or tenant/Pack/workflow-scoped enforce wiring;
5. the exact files/components, tenant, Pack, workflow, data class, provider,
   environment, migration, run window, and test inventory in scope;
6. prohibited actions, stop conditions, evidence/retention requirements,
   rollback path, stop authority, and completion/review criteria; and
7. confirmation that approval of one package, Pack/version, tenant, workflow,
   adapter, provider, or environment grants no authority to another.

Design acceptance and execution authorization are separate. The 2026-07-25
authorization permits only the interface slice stated at the top of this
document; it is not rehearsal or runtime authorization. Each later slice must
cite its own authorization record, fail closed when its scope cannot be proven,
and update the master status only after the authorized work or runtime fact
actually occurs.

Only after the named review resolves Q10 and all safety dependencies for an
external Pack or runtime scope may this document status change to Approved. As
of the date at the top of this document, the narrow interface slices and the
separately recorded source-free `payment.operations` Contract Catalog skeleton
are authorized. The production-owner record remains unresolved. The status
therefore remains **Draft / Discussion**, the runtime remains off/audit as
documented, and no Installation, external data access, rehearsal, deployment,
migration, state-changing capability, or enforce slice is authorized.
