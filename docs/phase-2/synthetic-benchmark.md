# Phase 2.1b Synthetic Governance Benchmark

## Purpose

This benchmark validates FinRPA's governance architecture without claiming access to a real financial institution, real customer data, or production compliance review.

Each scenario is an explicitly labelled engineering policy example:

    synthetic page state + candidate Action
    -> expected ActionIntent
    -> expected audit-only PolicyDecision

It is a regression oracle for implementation behavior, not evidence that a real bank's policy, threshold, or regulatory interpretation is complete.

The live audit observer does not run this analyzer and does not persist these
operation, effect, outcome, or risk labels. They remain offline synthetic
contracts only; see `foundation-closure.md` for the actual audit event format.

## Source of truth

- Fixture: `tests/fixtures/governance_scenarios.json`
- Test: `tests/unit/test_governance_benchmark.py`
- Analyzer: `enterprise/governance/analysis.py`
- Metrics/fault replay: `enterprise/governance/benchmark.py` and
  `tests/unit/test_governance_benchmark_metrics.py`
- Pre-enforce closure fixture:
  `tests/fixtures/pre_enforce_closure_scenarios.json`
- Pre-enforce closure test:
  `tests/unit/test_pre_enforce_closure_benchmark.py`

The fixture is intentionally plain English and static so it is deterministic, reviewable, and safe to run without external systems.

## Covered scenario families

| Family | Example expected outcome |
|---|---|
| Read/query | Query account balance -> allow / low |
| Public download | Product brochure -> allow / low |
| Sensitive export | Customer statement -> require approval / high |
| Business submit | Loan application -> require approval / high |
| Approval action | Approve loan -> require approval / high |
| External payment | Confirm transfer -> require approval / critical |
| Destructive action | Delete beneficiary -> require approval / critical |
| Sensitive input | Password input is identified as credential-bearing and redacted |
| Unsafe page state | Loading transfer confirmation -> needs human / unknown |

The Phase 2.2 pre-enforce corpus adds eleven cross-boundary cases: audit dry-run
allow/read, approval/high, external-write/critical, transitioning/needs-human,
action drift, page drift, multiple external writes, stale cached/speculative
actions, CUA missing engine evidence, governed-script rejection, and SDK/direct
route-or-reject closure. The first seven exercise pure governance/authorization
functions; the last four verify the machine-checked execution-entry contract.
All four referenced entry families are sealed by named regressions. None
invokes Skyvern, Playwright, ActionHandler, Permit issuance, or a business
result probe.

Review-remediation regressions additionally prove that `UNKNOWN` or
low-confidence page readiness cannot return `allow`, unsupported action-type
strings are rejected without being copied to evidence, and the exact Contract
expiry instant is denied. When a BusinessPlanStep declares inputs or an
expected transition, dry-run now requires a per-candidate
`BusinessSemanticResolver` must derive a `CandidateBusinessBinding` from the
current ActionIntent and element/page evidence; it does not receive Plan or
authorization objects. The offline baseline requires confidence of at least
`0.80`; missing or mismatched facts fail closed. Each canonical input and
transition leaf must declare a `fact_sources` path into the current Action
target facts. The dry-run resolves those paths itself and rejects incomplete,
unresolved, or value-mismatched mappings, including a resolver preloaded with
Plan facts for a different target. Raw binding facts, source paths, and
extractor/evidence references are used only as HMAC input and are not retained
in `phase2-governed-dry-run-v1`.

This binding contract does not create a production semantic extractor. Until a
named production Domain Pack owns and authenticates the adapter, the binding is
offline synthetic evidence only.

## How to extend it

For every new case, add:

1. a simulated DOM/page state;
2. a candidate Action payload;
3. a human-written expected operation, effect, outcome, and risk level;
4. an explanation in the change description of why that policy is expected.

New cases should prefer boundary conditions: ambiguous labels, dynamic loading, external effects, sensitive export, stale page states, and incomplete target evidence.

## Validation command

    py -3.11 -m pytest \
      tests/unit/test_governance_contracts.py \
      tests/unit/test_governance_audit.py \
      tests/unit/test_governance_benchmark.py -q

## Phase boundary

Passing this benchmark proves only that the implemented audit-only policy behaves as specified for these simulated cases. It does not authorize production enforce mode.

## Browser-backed synthetic scenario

The approved `synthetic.payment` console is also a safe browser target for
perception regression. `enterprise/governance/browser_audit.py` captures its
semantic DOM and a screenshot fingerprint, then runs the same deterministic
ActionIntent/PolicyDecision oracle without clicking or submitting anything.
The `phase2-browser-audit-v1` manifest contains stable field/action references,
HMAC-bound observation/screenshot fingerprints, HMAC-bound page/field/element
references, readiness, and redacted policy decisions. It never contains raw
HTML, screenshot bytes, form values, prompt text, browser session state, or
credentials. The collector only accepts the marked localhost synthetic console
and rejects redirects or unmarked pages. These manifests are local test
artifacts and do not replace the live `phase2-audit-candidate-v1` database event
contract.

## Task 4--6 foundation additions

`ObservationEvidenceBundle` records DOM-only, vision-only, or hybrid evidence
mode together with field controls, retention metadata, access roles, and model
egress policy. Conflicting or insufficient evidence for a state-changing action
fails safe; this contract does not collect screenshots or transmit any data.

`ExecutionProfile` records locator, label, coordinate, JavaScript, and
CUA-coordinate mechanisms. Weak coordinate/JavaScript mechanisms cannot
automatically cross an external commit boundary, and one observation may contain
at most one state-changing Action before re-observation. These are policy
contracts only, not a permit or ActionHandler integration.

Synthetic metrics include task success, first-action hit rate, incorrect-action
rate, L0--L4 distribution, UNKNOWN stop rate, fallback rate, audit completeness,
latency, and model cost. Audit completeness is now a read-only report of
versioned/redacted candidate payload validity, opaque observation coverage, and
externally aggregated non-blocking audit-write failures. Local egress-shadow
findings are redacted observation evidence only; they do not represent a model
egress decision or change a Skyvern request. Fault replay uses the policy-only
recovery decision; it never reruns a browser action. These additions remain
engineering regression evidence and are not compliance, banking, or regulatory
evidence.

The offline fault corpus now covers every declared failure class plus an
`ExecutionAttemptStatus.UNKNOWN` override. It verifies that UNKNOWN always
stops for a result probe, permission revocation requires reauthorization,
business-state drift outside the Contract escalates to L4, and only bounded
technical transients receive an L0 retry. These are pure decision replays with
no scheduler, browser, or result-probe invocation.

Before a separate enforce decision, the project must still:

1. retain the sealed known-entry inventory and add new callers when discovered;
2. extend browser audit evidence beyond the synthetic console only when a
   separate safe demo workflow is approved;
3. select and authenticate a named production Domain Pack with business owners;
4. separately approve and test the live Planner-to-Permit-to-ActionHandler
   wiring, approval recovery, result probes, and rollback path.
