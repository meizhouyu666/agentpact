# Phase 2 Versioned Interface Contracts

## Boundary

This document freezes the versioned contracts and their isolated synthetic
admission-persistence adapter. It creates no Planner, ForgeAgent, browser,
permit, approval, recovery, production Task-creation, or enforce caller. The
legacy Skyvern/ForgeAgent-connected Phase 2 runtime path remains audit-only
candidate observation. Separately, `enterprise/browser_loop` now provides an
AgentPact-owned browser runtime/session boundary, and `stripe.payment` has an
explicit governed test-mode composition with durable Permit/Attempt/UNKNOWN/
probe handling. Those explicit compositions are not created by this contract
document or mounted by formal application startup.

The first 2026-07-25 slice added deterministic authorization, interaction, and
admission contracts. The approved follow-on adds an audit-only SQLAlchemy
repository and one isolated synthetic API caller. It still creates no runnable
Skyvern Task and adds no browser or enforce wiring.

## Independent Authorization Dimensions

Capability authorization no longer compares the ordinal legacy chain
`none < read < operate < approve`. A policy maps each scoped role explicitly
to any of these independent dimensions:

- discover;
- read record;
- request transition;
- execute transition;
- request approval; and
- adjudicate approval.

The compatibility adapter maps legacy fields by semantic role and deliberately
does not give approvers or administrators transition execution. An explicitly
empty role map denies every dimension instead of falling back to legacy
behavior. New Domain Pack definitions must use an explicit role map.

CapabilityGrant now binds the business principal and optional workload
principal separately, the exact allowed dimensions, purpose, policy version,
revocation epoch, not-before time, and exclusive expiry. A Grant remains a
Planner/UI input and is never a Permit.

## Unified Capability Request and Projection

UI and chat create the same strict CapabilityRequest. The only entry-specific
field is `entry_mode`; principal, tenant, scope, capability/version, request
kind, typed inputs, resources, Grant, and contract versions share one schema.
Unknown fields are rejected.

GrantSetProjection exposes only active, non-denied Grants and only
disclosure-safe capability descriptions/input schemas. Policy reasons,
thresholds, and denied capabilities are not projected. Deterministic validation
rejects principal/tenant mismatch, expired Grants, request kinds outside the
allowed dimension, scope/resource expansion, version mismatch, and typed input
failure.

Capability input validation is an injected trusted registry boundary. A
non-empty schema without a validator fails closed; the interface does not add
an undeclared JSON-schema dependency or silently accept unvalidated data.

The strict Plan validator binds request ID, the first requested capability and
exact typed inputs, capability version, Grant, scope, and requested resources.
It rejects duplicate step IDs and verifies that each full Grant still matches
the principal, workload, tenant, revocation epoch, scope, version, and expiry
shown in its projection. It does not depend on an ungoverned second execution
loop.

## Capability Grant Expiry

A resolver must assign every CapabilityGrant a positive TTL. A grant is active
only when now is before expires_at; its expiry instant is not executable.
Future plan and Work Order validation must receive an explicit trusted now
value, so a stale grant cannot be accepted because a process omitted an
expiration check.

## Creation-Time Identity Snapshot

TrustedTaskCreationSnapshot is the only persistence input accepted by the
unconnected TaskContract helper. It contains task and organization identity,
initiator and optional service principal, scope/authorization snapshot, policy
and contract versions, and a creation timestamp. It also requires exactly the
trusted provenance appropriate to its creation path:

| Path | Required provenance |
|---|---|
| Native Task | authenticated request ID |
| Workflow Task | workflow ID and workflow-run ID |
| Template Task | template ID, template version, and template-run ID |
| SDK/API Task | authenticated request ID and exact caller ID |
| Direct internal Task | authenticated request ID, exact caller ID, and workload service principal |

No page observation, DOM field, prompt, or audit event is an accepted source.
Persisting this snapshot from the actual task-creation owners remains deferred
and needs a separate runtime-wiring approval.

## Atomic Admission Interface

GovernedTaskAdmissionService validates one audit-only TaskAdmissionBundle that
contains the Task draft, trusted creation snapshot, TaskContract,
CapabilityRequest, all active Grants referenced by the BusinessPlan, exactly
one Work Order per unique Plan step, and a redacted admission audit record.
Every Plan capability must be allowed by the TaskContract and every Work Order
must use the capability registry's exact result probe.

The repository protocol owns one atomic, idempotent transaction for the whole
bundle. Its admission ID is deterministic for tenant + request. A retry of
the same stable admission semantics returns the prior receipt even when trusted
timestamps are regenerated. The stable fingerprint covers Task, snapshot,
Contract, Request, all Grants, Plan, and Work Orders; changing any authoritative
content is a conflict. Absolute reconstruction timestamps are normalized, but
Grant not-before offsets, Grant validity durations, and Contract validity
durations remain fingerprinted, so a changed authorization window conflicts.
An uncertain or mismatched receipt requires reconciliation by admission ID,
not submission of a new business request.

`ent_007` adds `governed_task_admissions` and `governance_outbox`. The first is
a non-runnable aggregate rather than a row in Skyvern's `tasks` table. The
second contains only the redacted AdmissionAuditRecord and remains `pending`;
no publisher is added in this slice. `SqlAlchemyTaskAdmissionRepository`
requires explicit tenant and capability allowlists, persists the aggregate
before the FK-bound outbox row, and commits both together.

The isolated `POST /api/task-admissions` synthetic endpoint is the only caller.
It must receive an injected repository and otherwise returns unavailable. It
accepts only fixed synthetic identities/data, creates audit-mode contracts and
non-executable Work Orders, and never calls the synthetic payment execution
harness or a Skyvern Task creator. Native/workflow/template/production SDK and
direct creators remain unconnected; `enforce` is still rejected.

## Serialization And Invalidation

VersionedGovernanceArtifact wraps a JSON payload with a schema version and
deterministic invalidation keys. The interface covers TaskContract,
CapabilityGrant, CapabilityRequest, GrantSetProjection, BusinessPlan,
ExecutionWorkOrder, ObservationContext, and RecoveryDecision.

| Artifact | Invalidation keys |
|---|---|
| Contract | contract ID, task ID, contract version, policy version |
| Grant | grant ID, capability/version, business/workload principals, tenant, policy snapshot version, revocation epoch, purpose, validity window |
| CapabilityRequest | request/principal/tenant IDs, capability/version, Grant, request kind |
| GrantSetProjection | business/workload principals, tenant, revocation epoch, generation time |
| BusinessPlan | plan ID, task ID, contract ID, plan version |
| WorkOrder | Work Order ID, task ID, plan step, contract ID, grant ID |
| Observation | observation ID, task ID, step ID, snapshot hash |
| RecoveryDecision | failure class, level, action, max attempts, reauthorization/result-probe flags |

A future owner must discard a serialized artifact when its kind, schema
version, or invalidation keys differ. The envelope is not a permit, does not
authorize a browser action, and is not consumed by any runtime component.

## Acceptance

The phase2 interface-contract test verifies all five trusted creation paths,
rejects missing provenance, asserts the persistence helper does not read
observation-time identity, and round-trips the eight versioned interface families
through the invalidation rules. Repository tests cover whole-bundle allowlists,
atomic rollback, stable retries, authorization-window and other semantic
conflicts, and redacted outbox content.

A disposable PostgreSQL 14.23 rehearsal applied both heads through `ent_007`.
Concurrent identical submissions produced one aggregate and one outbox row,
with one original and one duplicate receipt; a changed amount conflicted,
outbox evidence contained no typed inputs, and the Skyvern `tasks` table stayed
empty.
