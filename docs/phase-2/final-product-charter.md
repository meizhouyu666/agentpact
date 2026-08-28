# AgentPact Final Product Charter and Delivery Policy

> Status: **M1-M5 synthetic portfolio release DONE/PASS locally; production scope remains gated**
>
> Date: 2026-07-29
>
> Runtime impact: none. The current runtime remains audit-only and
> `GOVERNANCE_MODE=enforce` remains configuration-rejected.

## 1. Final product objective

AgentPact is an open-source portfolio project for Agent Platform / AI
Application Infrastructure work. Its finished product is the **Governed Browser-Agent Harness, Domain Pack SDK & Conformance Kit**
built on Skyvern.

The project demonstrates how a browser Agent can be constrained by typed
business contracts, authorization, evidence, and recovery rules. It is not an
in-repository production payment system, a banking integration, a compliance
certification, or a generic multi-Agent product.

`synthetic.payment` is the sole reference Pack and fault-injectable synthetic
system. It does not represent production business facts or a customer Pack.

## 2. Fixed product boundaries

The following are frozen unless the project owner explicitly changes this
Charter:

1. Skyvern remains the sole browser executor. A Planner, coordinator, SDK, or
   Domain Pack never receives Playwright control.
2. A browser response never proves a business effect. A result probe is the
   only confirmation source; inconclusive post-effect work remains `UNKNOWN`
   and is never automatically replayed.
3. The framework is tested only with synthetic identities, facts, sources,
   pages, effects, and fault injection. It contains no real payment data,
   credentials, bank/ERP connector, or tenant installation.
4. The active runtime registry consumes only a future tenant-scoped
   Installation artifact. Offline Contract Catalog artifacts are never
   discoverable or executable.
5. Q1--Q10 are requirements for a future external Pack installation or
   runtime/rehearsal/enforce decision. They do not block source-free framework
   authoring, but unresolved values must fail closed.
6. No second reference domain, production Pack, deployment work, or global
   enforce work is in scope before this Charter's release criteria are met.

## 3. Verified current baseline

| Area | Current fact | Release gap |
|---|---|---|
| Runtime boundary | Audit-only; global enforce remains rejected. | No production runtime work is implied by the accepted synthetic proof. |
| Governance foundations | RBAC/Capability, Plan/WorkOrder, Permit/Attempt/UNKNOWN, audits, entry sealing, dry-run, and fault fixtures exist. | Expose them through a focused framework Conformance Kit, not more generic foundations. |
| Reference domain | `synthetic.payment` is the accepted normative offline reference suite and the accepted process-isolated M4 browser-effect proof. | It remains synthetic, uninstalled, and non-production. |
| Pack SDK boundary | M1 through M3 are complete: immutable offline Contract Catalog, reusable Pack SDK shapes, deterministic static Conformance Kit, invalid-Pack fixtures, active-runtime exclusion, and the accepted synthetic reference adapter. | No SDK/runtime wiring gap is implicitly open. |
| Public delivery | M5 provides the cross-platform CLI, release reports, Ubuntu CI, Windows release smoke, README/guide, and attribution. | Remote publication remains a separate owner action. |

## 4. Delivery roadmap

| Milestone | State | Deliverable and acceptance boundary |
|---|---|---|
| M1: Contract boundary | Complete | Offline immutable Contract Catalog is separate from the active registry; discover/read-only reference contract is uninstalled and fail-closed. |
| M2: Pack SDK and static conformance | Complete | Reusable contract/effect/evidence/version rules, invalid-Pack fixtures, and a deterministic versioned static report are implemented and accepted after review remediation. No runtime caller. |
| M3: Reference Pack conformance | Complete | `synthetic.payment` is the independently reviewed normative offline SDK/conformance reference. Ordinary, denied, stale, malformed, import-isolation, and evidence-binding cases pass without real-system or runtime wiring. |
| M4: Synthetic governed end-to-end proof | Complete and independently accepted | The process-isolated proof records durable `EXECUTING`, post-effect `UNKNOWN`, no replay, independent probe confirmation, one synthetic commit, and complete cleanup. Global enforce remains unavailable. |
| M5: Developer and release experience | Implemented for independent review; now DONE/PASS locally | The canonical Python CLI, deterministic release report, cross-platform discovery, Ubuntu CI, manual/release Windows smoke, README/guide, attribution, and curated local history are independently accepted at HEAD `d1c2587b2b03ae107429e1cd131dd5bc5082c390`; final review SHA-256 `d73196352e8f06512d69fc92a86127f19e370c11ffaf31f67183a39c55153707`. |

M2 and M3 are framework/interface work. M4 was separately approved and accepted
only as a synthetic evidence test; it did not authorize a production Pack,
runtime caller, deployment, or global enforce. M5 is the portfolio-ready local
release milestone. Publication remains separately authorized.

## 5. Definition of done

The project is ready to publish when a new reader can, without production
access:

1. start a synthetic demo and see the Agent/Harness boundary;
2. run a Conformance Kit that accepts the reference Pack and rejects deliberate
   violations such as an uninstalled Pack, execution authority in a read-only
   contract, missing source/owner, stale evidence, or invalid effect state;
3. reproduce a synthetic post-effect timeout that becomes `UNKNOWN`, confirm it
   only through a probe, and observe that retry is rejected;
4. see that browser execution is confined to Skyvern and that no real system,
   credential, or global enforce setting is required; and
5. understand the project in the README within five minutes, including what was
   built on Skyvern and what AgentPact adds.

## 6. Architecture and development operating model

The primary architecture agent owns the coherent technical design, Charter,
interfaces, acceptance criteria, and cross-slice review. Development processes
or workers implement only one frozen milestone slice at a time, run its tests,
and report concrete evidence; they do not independently redefine product scope,
runtime authority, or Pack semantics.

The project owner approves:

- this Charter and any future change to the fixed boundaries;
- any change to M4's accepted synthetic-effect boundary; and
- any later external Pack, deployment, real-data, or enforce proposal.

Every implementation handoff must state: target milestone, exact files,
prohibited actions, acceptance tests, runtime-boundary statement, and rollback
scope. A proposal that adds a new domain, source connector, production claim,
or global mode change is rejected unless the Charter is explicitly amended
first.

## 7. Documentation authority

For future work, resolve conflicts in this order:

1. `phase-2-master-status.md` -- current runtime and completed-work facts;
2. this Charter -- final product scope and release criteria;
3. `framework-first-replan.md` -- framework-first rationale and package design;
4. a separately approved milestone handoff -- exact implementation authority.

Earlier Payment Operations material is reference/conformance design only. It
must not be read as approval to create a production Pack or to enable enforce.
