# Approval Request: `payment.operations` Contract Skeleton

> Status: **Approved for source-free skeleton implementation only**
>
> Date: 2026-07-25
>
> Boundary: `domain-pack` -- a source-free, parameterized, uninstalled,
> read-only reference contract skeleton. It does not authorize a
> production Pack, external data access, runtime integration, a state-changing
> capability, or `GOVERNANCE_MODE=enforce`.

## 1. Decision requested

Approve a narrow authoring slice: create a `payment.operations` semantic
contract skeleton and an offline Contract Catalog entry for discover/read
semantics only. There is no actual production business, identity,
source-system, data-classification, or named-owner material. The skeleton must
therefore require those values later and fail closed while they are unresolved.

It is uninstalled in every tenant and cannot be resolved by a live task, UI,
chat request, Planner, ForgeAgent, Governor, ActionHandler, browser flow,
admission service, or result-probe runner.

## 2. Manifest decision

The existing `DomainPackManifest` plus `DomainPackRegistry.register(...)`
cannot represent this boundary: registration makes capabilities visible to the
active registry. Do not put an `installation_state` boolean in that Manifest;
a mutable flag in a copied artifact cannot prove that no tenant installed it.

Use two distinct artifacts:

```text
DomainPackContractManifest (offline, immutable, this slice)
  -> separate Contract Catalog (offline only)
  -> DomainPackInstallation (tenant/runtime, absent from this slice)
  -> active DomainPackRegistry / CapabilityResolver
```

The source-free `DomainPackContractManifest` is versioned as:

```text
schema_version: domain-pack-contract/v1
pack_id: payment.operations
contract_version: 0.1.0-draft.1
maturity: reference
```

It contains only:

1. parameter definitions for the payment glossary, authoritative source,
   canonical facts, lifecycle, and read-result evidence;
2. discover/read capability shapes with an empty transition/effect surface;
3. parameter definitions for authorization freshness, data classification,
   egress, retention, and access evidence; and
4. owner references whose unresolved value is the literal `unassigned`.

It contains no connector, credential, tenant, installation, active registry
binding, live policy, live fact, result-probe invocation, or executable work
order. A future `DomainPackInstallation` binds an immutable contract digest to
a tenant, source, adapter, policy, and enabled capabilities. No Installation
artifact is created or accepted in this slice.

`reference` means the shape exists to exercise the SDK and Conformance Kit. It
does not assert production semantics, data, ownership, eligibility,
installation, connectivity, or usability.

## 3. Decided boundary and exclusions

The following decisions are made now:

| ID | Decision |
|---|---|
| Q4 | Only discover/read semantics exist. There is no state-changing capability, transition, approval, execution, compensation, or retry. |
| Q7 | `FinRPA Platform` owns the offline Contract Catalog interface. The active `DomainPackRegistry` must accept only a later Installation artifact. No admission/runtime work opens. |
| Q8 | No deployment, schema change, tenant installation, or migration. Rollback removes only contract/catalog source files. |
| Q9 | This is the sole next material slice: a source-free, uninstalled `payment.operations` contract skeleton. It excludes adapter construction, a live read, rehearsal, production Pack, and enforce wiring. |

The slice must not add an API route, source connector, credential, model
request, database migration, task admission, outbox publisher, Scheduler,
ForgeAgent adapter, Planner caller, Permit, Attempt, ActionHandler context,
Skyvern/Playwright call, configuration change, or deployment manifest.

Discover/read authority must never imply `execute_transition`,
`request_approval`, or `adjudicate_approval` through role ordering,
administrator status, or a legacy permission level.

## 4. Q1--Q10 installation gates

The skeleton records each requirement and rejects installation or a runtime
read when it is unresolved. It does not invent a production value.

| ID | Required before Installation or runtime read |
|---|---|
| Q1 | Named payment business owner; immutable business glossary, authoritative source/version, canonical facts, lifecycle/terminal states, and read-result provenance/freshness evidence. |
| Q2 | Deterministic organization x department x business-line x role matrix for discover/read, including explicit SoD and no inherited execution/approval authority. |
| Q3 | Identity, policy, and revocation authorities; trusted clock; propagation/cache maximum; expiry rule; and a fail-safe result when freshness is unprovable. |
| Q4 | A new approval for every state-changing capability, regardless of a user's read authority. |
| Q5 | Named authoritative read source, permitted path, credential issuer/custodian, integration/probe owner, and connector review. |
| Q6 | Field classification, transformations, model/provider/region/purpose decision, retention/access/legal-hold/export/delete policy, and unknown-classification deny rule. The skeleton default is no model egress and no payload retention. |
| Q7 | A named registry/interface owner and proof that the contract remains outside the active registry until an Installation exists. |
| Q8 | A new release decision for every deployment, migration, or tenant installation; source rollback is insufficient once any runtime artifact exists. |
| Q9 | A new scoped decision naming the Pack version, tenant, workflow, source, adapter, and exact allowed runtime behavior. |
| Q10 | Named accountable people or teams and permitted role combinations for business, policy, identity/security, integration/probe, registry, data, risk, legal/compliance, rehearsal, and SRE/release. |

## 5. Owner model while no production material exists

There are no production owners to name now. The only current owner is the
project-level `FinRPA Platform` role for the Contract Catalog interface. It is
not a business, security, data, risk, legal, rehearsal, release, or production
authority.

All other owner references are `unassigned`. That is valid for this offline
artifact and invalid for Installation. When named, the future owner record must
include the following roles:

| Role | Installation responsibility | Combination rule |
|---|---|---|
| Payment business | Q1 business semantics and evidence meaning | May combine with integration/probe only if risk is separately named. |
| Tenant policy | Q2 RBAC matrix and SoD | May combine with identity/security only under an explicit delegation. |
| Identity/security | Q3 freshness and revocation | May combine with data control only if legal/compliance is separate. |
| Integration/probe | Q5 source and credential boundary | May not self-approve risk acceptance. |
| Registry/interface | Q7 catalog versus active registry | May combine with platform/runtime; does not authorize wiring. |
| Data control | Q6 classification and retention | May combine with legal/compliance only under documented delegation and security co-signature. |
| Risk | Residual-risk acceptance | Must be distinct from the primary contract author. |
| Legal/compliance | Q6 legal and retention position | Separate unless the documented data/legal delegation applies. |
| Rehearsal | Any later rehearsal request | May combine with SRE/release only before execution is authorized. |
| SRE/release | Any deployment/migration/rollback decision | May combine with rehearsal only before execution is authorized. |

## 6. Acceptance evidence for this slice

The skeleton is complete only when static review proves that:

1. it has the declared schema and contract version and no
   `DomainPackInstallation` exists;
2. it is absent from every active/default runtime registry;
3. every production field and owner reference is required and currently
   `unassigned` rather than copied from `synthetic.payment` or represented as
   a real fact;
4. its capability catalog is limited to discover/read and has an empty
   transition/effect surface;
5. static tests reject an active registry binding, installation artifact,
   state-changing capability, connector, credential, runtime import, API,
   migration, or model-egress path; and
6. source rollback removes only the offline contract/catalog files and has no
   database, tenant, browser, outbox, or external-system action.

Passing these checks proves only source-free contract authoring. It does not
authorize a live read or assert production readiness.

## 7. Approval record

Decision: **APPROVE** the source-free contract skeleton only, as authorized by
the workspace project owner in the current Codex thread and independently
reviewed against the Phase 2 master status on 2026-07-25.

Approved versions:

- schema: `domain-pack-contract/v1`;
- Pack ID: `payment.operations`;
- contract: `0.1.0-draft.1`.

File whitelist:

- `enterprise/governance/domain_pack_contracts.py`;
- `enterprise/domains/payment_operations/__init__.py`;
- `enterprise/domains/payment_operations/contract.py`;
- `tests/unit/test_payment_operations_contract.py`;
- `tests/unit/test_phase2_master_status.py`;
- `docs/phase-2/framework-first-replan.md`;
- `docs/phase-2/enforce-readiness-proposal.md`;
- `docs/phase-2/payment-operations-readonly-contract-approval.md`; and
- `docs/phase-2/phase-2-master-status.md`.

The implementation must not modify the active `DomainPackRegistry`, export the
reference contract from `enterprise.governance.__init__`, or add any
installation, tenant, source, credential, API, persistence, migration, model,
browser, Planner, admission, outbox, approval, Permit, Attempt, recovery,
configuration, deployment, or enforce path. Rollback is ordinary source
removal of only the whitelisted contract/catalog files and their tests/docs.

Q1--Q3, Q5--Q6, and Q10 remain unresolved Installation gates. This approval
does not authorize a live read or any external Pack adoption activity.

Implementation outcome: **Completed after strict cross-layer review.** The
approved files add only an immutable `DomainPackContractManifest`, an offline
`DomainPackContractCatalog`, and the source-free
`payment.operations@0.1.0-draft.1` reference skeleton. Focused validation
passed `46` tests; the full unit suite passed `823` tests with one existing
FastAPI deprecation warning. Ruff, compileall, Alembic heads, diff/whitespace,
forbidden-dependency, active-registry import, and runtime-import checks passed.
No runtime boundary changed.

Every later Installation/read-adapter decision must resolve Q1--Q10 with named
owners, immutable source/policy versions, exact file scope, data controls, and
a rollback owner. It is a separate approval.

## 8. Relationship to Phase 2

This is the narrow follow-up to `enforce-readiness-proposal.md`. It does not
replace the broader A--F design or its production/enforce gates. The Phase 2
master status remains authoritative for runtime facts.
