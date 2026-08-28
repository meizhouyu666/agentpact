# Framework-First Replan: Domain Pack SDK and Conformance

> Status: **M3 normative offline reference conformance complete**
>
> Date: 2026-07-25
>
> Runtime impact: none. `GOVERNANCE_MODE=enforce` remains rejected and the only
> Skyvern-connected Phase 2 behavior remains audit observation.

## 1. Corrected product objective

FinRPA's deliverable is a Financial Operations Agent Harness, Domain Pack SDK,
and Domain Pack Conformance Kit. It is not an in-repository production payment
product and does not claim ownership of any customer's payment facts, policy,
source system, or approval process.

`synthetic.payment` remains the sole reference Pack and fault-injectable test
system. It is not a production Pack, tenant installation, integration, or
evidence of production readiness.

## 2. What the project can prove without production material

The framework can be tested against a complete synthetic reference domain:

```text
Pack contract -> RBAC/Capability -> Plan/Work Order -> Harness controls
-> synthetic browser/system -> independent synthetic result probe
-> conformance report
```

The Conformance Kit must prove framework properties, including:

1. a Pack defines versioned capabilities, canonical facts, effect class,
   result-evidence requirements, and invalid/terminal states;
2. discover/read never implies request, execution, or approval dimensions;
3. an uninstalled Contract Catalog artifact cannot be resolved by the active
   runtime registry;
4. Permit/Attempt/UNKNOWN/rollback invariants hold under synthetic faults;
5. Skyvern is the sole browser executor and browser transport is not business
   confirmation; and
6. unknown data class, missing source evidence, missing ownership, stale
   authority, or an incomplete Pack contract fails closed.

This evidence proves that the framework satisfies its published contract. It
does not prove that a particular bank, ERP, tenant policy, or payment workflow
is suitable for production.

## 3. Replanned work packages

| Package | Framework deliverable | Explicit exclusion |
|---|---|---|
| F1 | Split immutable `DomainPackContractManifest`/offline Contract Catalog from future tenant-scoped `DomainPackInstallation`/active registry. | Tenant installation, capability exposure, runtime caller. |
| F2 | Define the Pack SDK: facts, effect classification, state machine, evidence, owner/reference fields, versioning, and fail-closed unresolved values. | Customer facts, policies, credentials, or source connectors. |
| F3 | Upgrade `synthetic.payment` into the normative reference Pack and synthetic authoritative-system/browser fixture. | Rebranding synthetic facts as production facts. |
| F4 | Build a reusable Conformance Kit: static contract checks, semantic fixtures, fault matrix, browser-isolation tests, and machine-readable report. | A production certification or compliance claim. |
| F5 | Define an external Pack adoption protocol: installation manifest, adapter/probe boundary, evidence bundle, and scoped approval template. | Implementing or installing an external Pack. |

F1, M2's reusable F2/static-F4 slice, and M3's F3 adaptation of
`synthetic.payment` into the normative offline reference contract and
conformance suite are complete. M3 passed independent review after bounded
relative-import-guard and evidence-documentation remediation. Later
browser-effect proof and external adoption work remain separately gated. No
package grants enforce wiring. The final release criteria and operating model
are frozen in `final-product-charter.md`.

## 4. Q1--Q10 are reclassified

Q1--Q10 are no longer blockers for framework authoring. They are required
inputs to a future customer's Installation, adapter, runtime read, state change,
rehearsal, or scoped enforce decision.

| Questions | Framework requirement now | Required from a future Pack adopter |
|---|---|---|
| Q1, Q5 | Schema and evidence interfaces with required source/provenance fields. | Actual business glossary, authoritative source, credentials, and result evidence. |
| Q2, Q3 | Independent authorization dimensions and deterministic freshness/revocation contracts. | Tenant role matrix, SoD, identity/policy authority, and real freshness limits. |
| Q4 | State-changing effect class and UNKNOWN-safe protocol. | The specific transition, idempotency, approval, and probe semantics. |
| Q6 | Classification/egress/retention policy interfaces that deny unknown values. | Field classifications, provider/region, legal, retention, and access decisions. |
| Q7 | Contract Catalog, Installation boundary, active-registry exclusion, and SDK version rules. | Named Pack registry/interface owner for the installed Pack. |
| Q8 | Rollback contract and no-fall-through invariants. | Deployment, migration, stop authority, and on-call procedure. |
| Q9, Q10 | Scoped approval template and conformance report format. | Named Pack/version/tenant/workflow and accountable owner record. |

## 5. Acceptance boundary

Framework completion is evidenced by a versioned Conformance Kit passing all
required synthetic cases and rejecting deliberately invalid Packs. It does not
require real payment records. A real Pack is complete only after its adopter
supplies Q1--Q10, passes the same Kit using its own non-production environment,
and receives a separate Pack/tenant/workflow-scoped approval.

## 6. Plan supersession

This document supersedes the earlier interpretation that Enterprise Payment
Operations is FinRPA's first in-repository production-candidate deliverable.
`enforce-readiness-proposal.md` remains the long-term safety architecture, but
its Payment Operations sections must be read as reference/conformance design
until a future adopter supplies production material and obtains a new approval.
