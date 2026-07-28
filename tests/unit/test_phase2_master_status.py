"""Static consistency checks for the authoritative Phase 2 status document."""

from pathlib import Path

STATUS = (Path(__file__).parents[2] / "docs" / "phase-2" / "phase-2-master-status.md").read_text(encoding="utf-8")
FOUNDATION = (Path(__file__).parents[2] / "docs" / "phase-2" / "foundation-closure.md").read_text(encoding="utf-8")
PROPOSAL = (Path(__file__).parents[2] / "docs" / "phase-2" / "enforce-readiness-proposal.md").read_text(
    encoding="utf-8"
)
FRAMEWORK_REPLAN = (Path(__file__).parents[2] / "docs" / "phase-2" / "framework-first-replan.md").read_text(
    encoding="utf-8"
)
CHARTER = (Path(__file__).parents[2] / "docs" / "phase-2" / "final-product-charter.md").read_text(encoding="utf-8")
M2_HANDOFF = (Path(__file__).parents[2] / ".claude" / "plans" / "m2-pack-sdk-static-conformance.md").read_text(
    encoding="utf-8"
)
M2_REVIEW = (
    Path(__file__).parents[2] / ".claude" / "plans" / "m2-pack-sdk-static-conformance-review.md"
).read_text(encoding="utf-8")
M3_HANDOFF = (
    Path(__file__).parents[2] / ".claude" / "plans" / "m3-synthetic-reference-conformance.md"
).read_text(encoding="utf-8")
CONTRACT_APPROVAL = (
    Path(__file__).parents[2] / "docs" / "phase-2" / "payment-operations-readonly-contract-approval.md"
).read_text(encoding="utf-8")


def test_completed_audit_hardening_is_not_described_as_missing_prompt_or_partial():
    assert "P1: Make Audit Evidence Usable (Completed After Review Remediation)" in STATUS
    assert "it does not yet receive the real Prompt" not in STATUS
    assert "| 4. Semantic observation and evidence | Partial audit capture |" not in STATUS
    assert "| 6. Evaluation, audit, replay | Partial |" not in STATUS


def test_status_records_completed_database_rehearsal_and_keeps_runtime_gates_explicit():
    assert "GOVERNANCE_MODE=enforce` is configuration-rejected" in STATUS
    assert "`a86c9fdba6b3` and `ent_006` are" in STATUS
    assert "`uq_pending_action_active_step`" in STATUS
    assert "A production Domain Pack, real enforce wiring" in STATUS
    assert "remain outside the current scope" in STATUS


def test_interface_only_request_plan_and_admission_slice_is_recorded_without_live_wiring():
    assert "CapabilityRequest / Grant projection | Interface only" in STATUS
    assert "Governed Task admission | Synthetic persistence, audit-only" in STATUS
    assert "non-runnable aggregate plus redacted pending outbox" in STATUS
    assert "no Skyvern Task is published" in STATUS
    assert "Capability request, planning, and admission | Synthetic persistence foundation" in STATUS
    assert "additive governance migrations through `ent_007`" in STATUS
    assert "Audit-only governed Task admission persistence | Started" in STATUS
    assert "Audit-only governed Task admission persistence | Completed after strict cross-layer review" in STATUS
    assert "Admission/runtime baseline consistency | Started" in STATUS
    assert "Admission/runtime baseline consistency | Completed after strict cross-layer review" in STATUS
    assert "full unit `817 passed`" in STATUS
    assert "Both PostgreSQL processes are stopped" in STATUS


def test_foundation_and_proposal_match_the_completed_admission_and_rehearsal_baseline():
    assert "only Skyvern-connected Phase 2 runtime write" in FOUNDATION
    assert "isolated synthetic application may persist one audit-only" in FOUNDATION
    assert "not a Skyvern\n`tasks` row" in FOUNDATION
    assert "migration order through `ent_007`" in FOUNDATION
    assert "Two explicitly authorized disposable PostgreSQL 14.23 rehearsals" in FOUNDATION
    assert "this repository task did not run that command" not in FOUNDATION

    assert "Baseline documentation reconciliation" in PROPOSAL
    assert "completed through `ent_007`" in PROPOSAL
    assert "Documentation drift to resolve during review" not in PROPOSAL


def test_framework_first_contract_slice_is_approved_without_external_pack_authority():
    assert "M3 normative offline reference conformance complete" in FRAMEWORK_REPLAN
    assert "Domain Pack SDK and Conformance" in FRAMEWORK_REPLAN
    assert "not an in-repository production payment" in FRAMEWORK_REPLAN
    assert "Q1--Q10 are no longer blockers for framework authoring" in FRAMEWORK_REPLAN

    assert "Approved for source-free skeleton implementation only" in CONTRACT_APPROVAL
    assert "schema: `domain-pack-contract/v1`" in CONTRACT_APPROVAL
    assert "contract: `0.1.0-draft.1`" in CONTRACT_APPROVAL
    assert "Q1--Q3, Q5--Q6, and Q10 remain unresolved Installation gates" in CONTRACT_APPROVAL
    assert "must not modify the active `DomainPackRegistry`" in CONTRACT_APPROVAL
    assert "Implementation outcome: **Completed after strict cross-layer review.**" in CONTRACT_APPROVAL
    assert "full unit suite passed `823` tests" in CONTRACT_APPROVAL
    assert "separately recorded source-free `payment.operations` Contract Catalog skeleton" in PROPOSAL
    assert "no Installation, external data access, rehearsal, deployment" in PROPOSAL

    assert "Domain Pack Contract Catalog | Offline reference only" in STATUS
    assert "Domain Pack SDK / Contract Catalog | M3 reference conformance complete" in STATUS
    assert "External production Domain Pack | None" in STATUS
    assert "Payment Operations source-free Contract Catalog skeleton | Approved and started" in STATUS
    assert "Payment Operations source-free Contract Catalog skeleton | Completed after strict cross-layer review" in STATUS


def test_m3_completion_keeps_m4_and_runtime_wiring_closed():
    assert "M2: Pack SDK and static conformance | Complete" in CHARTER
    assert "M3: Reference Pack conformance | Complete" in CHARTER
    assert "Complete after review remediation and project-owner acceptance" in M2_HANDOFF
    assert "Accepted after remediation and project-owner completion decision" in M2_REVIEW
    assert "M2 Pack SDK and static Conformance Kit | Completed after review remediation and owner acceptance" in STATUS
    assert "M3 synthetic reference Pack conformance | Completed after bounded review remediation" in STATUS
    assert ".claude/plans/m3-synthetic-reference-conformance.md" in STATUS

    assert "Completed after bounded review remediation and independent acceptance" in M3_HANDOFF
    assert "interface-only` / offline reference conformance. No runtime caller" in M3_HANDOFF
    assert "enterprise/domains/synthetic_payment/sdk_manifest.py" in M3_HANDOFF
    assert "Do not re-export the new builder" in M3_HANDOFF
    assert "GOVERNANCE_MODE=enforce` remains configuration-rejected" in M3_HANDOFF
    assert "focused M3/M2 static suites: `32 passed`" in M3_HANDOFF
    assert "M4 remains closed" in M3_HANDOFF


def test_m4_acceptance_and_m5_release_experience_keep_production_closed():
    assert "M4 governed browser proof | Accepted test evidence" in STATUS
    assert "M5 developer/release experience | Implemented for review" in STATUS
    assert "743d05d191b3d96440161c70c04fd0a6ea5bf940b8b89e2d421ac94be6cb75b4" in STATUS
    assert "`finrpa.release-report/v1`" in STATUS
    assert "No CI\njob receives a production secret or publishes/deploys anything" in STATUS

    assert "M4: Synthetic governed end-to-end proof | Complete and independently accepted" in CHARTER
    assert "M5: Developer and release experience | Implemented for independent review" in CHARTER
    assert "Publication remains separately authorized" in CHARTER


def test_synthetic_payment_never_claims_production_eligibility():
    assert "Approved Synthetic Payment Sandbox" in STATUS
    assert "`production_eligible=false`" in STATUS
    assert "It does not import ForgeAgent, ActionHandler" in STATUS


def test_remaining_authorized_work_is_offline_or_audit_only():
    section = STATUS.split("### Remaining Authorized Work", maxsplit=1)[1].split(
        "## 7. Gates Before a Separate Enforce Decision", maxsplit=1
    )[0]
    assert "offline L0--L4 fault corpus" in section
    assert "execution-entry inventory/regression matrix" in section
    assert "audit replay compatibility" in section
    assert "static migration rehearsal" in section
    assert "known, finite gaps in this offline/audit-only package are closed" in section
    assert "All six known execution-entry sealing slices are complete" in section
    assert "requires a separate review and approval" in section


def test_browser_audit_review_findings_are_recorded_as_remediated():
    assert "Synthetic Domain Pack and Browser Audit Remediation" in STATUS
    assert "revalidates the current task contract" in STATUS
    assert "HMAC fingerprints for the page URL" in STATUS
    assert "rejects query strings and credentials" in STATUS
    assert "failed `networkidle` wait is preserved as `TRANSITIONING`" in STATUS
    assert "synthetic audit log is empty before" in STATUS
    assert "and after collection" in STATUS
    assert "Runtime boundary change: **none**" in STATUS


def test_pre_enforce_closure_is_recorded_without_claiming_runtime_wiring():
    current = STATUS.split("### Phase 2.2 Pre-Enforce Closure", maxsplit=1)[1].split(
        "## 5. Current Audit Evidence Contract", maxsplit=1
    )[0]
    assert "Six Entry Families Sealed; Enforce Still Disabled" in current
    assert "phase2-governed-dry-run-v1" in STATUS
    assert "CandidateBusinessBinding" in STATUS
    assert "BusinessSemanticResolver" in STATUS
    assert "fact_sources" in STATUS
    assert "preloading Plan values" in STATUS
    assert "record-B/closed/archived" in STATUS
    assert "Missing evidence, capability drift" in STATUS
    assert "readiness confidence below `0.60`" in STATUS
    assert "Unsupported values are" in STATUS
    assert "`now >= expires_at` is expired" in STATUS
    assert "execution_adapter_called=false" in STATUS
    assert "runtime_wiring_eligible=false" in STATUS
    assert "`skyvern_page_script_proxy`" in current
    assert "`shared_script_launchers`" in current
    assert "`handler_locator_coordinate_javascript`" in current
    assert "`sdk_direct_script_clients`" in current
    assert "are now `sealed`" in current
    assert "`enforce_eligible=true`" in current
    assert "Global enforce remains disabled" in current
    assert "GOVERNANCE_MODE=enforce` remains" in STATUS
    assert "configuration-rejected" in STATUS
    assert "both P1 and both P2 findings are remediated" in STATUS
    assert "Semantic Target Independent Re-Review and Offline Acceptance" in STATUS
    assert "accepted as an offline pre-enforce contract" in STATUS
    assert "Execution-entry sealing | 6 of 6 known families sealed" in STATUS


def test_shared_script_sealing_is_recorded_without_opening_global_enforce():
    review = STATUS.split("### 2026-07-23: Task 5 Shared Governed-Script Sealing", maxsplit=1)[1].split(
        "## 11. Activity Log", maxsplit=1
    )[0]

    assert "accepted for the two script execution-entry families" in review
    assert "execute_script(...)" in review
    assert "run_script(...)" in review
    assert "ScriptSkyvernPage.__init__(...)" in review
    assert "the other four rows remain" in review
    assert "`unsealed` and `enforce_eligible=false`" in review
    assert "`off` and" in review
    assert "`audit` preserve Phase 1 script behavior" in review
    assert "configuration still" in review
    assert "rejects `GOVERNANCE_MODE=enforce`" in review


def test_handler_mechanism_sealing_records_permit_binding_and_runtime_boundary():
    review = STATUS.split(
        "### 2026-07-23: Task 5 Governed ActionHandler Mechanism Sealing",
        maxsplit=1,
    )[1].split("## 11. Activity Log", maxsplit=1)[0]

    assert "accepted for `handler_locator_coordinate_javascript`" in review
    assert "stores the effect and full profile" in review
    assert "rejects a downgraded effect or substituted profile" in review
    assert "Permit consumption" in review
    assert "locator -> label -> coordinate -> JavaScript" in review
    assert "`external_write`" in review
    assert "`59 passed`" in review
    assert "`772 passed`" in review
    assert "dormant governed handler path only" in review
    assert "ForgeAgent does not issue or pass a Permit" in review
    assert "still rejects" in review
    assert "`GOVERNANCE_MODE=enforce`" in review
