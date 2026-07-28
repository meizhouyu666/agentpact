# Synthetic Payment Domain Pack

## Status and Boundary

`synthetic.payment` is a non-production reference Domain Pack for validating
the Phase 2 control protocol. Its manifest always has
`production_eligible=false`.

It does not connect to Skyvern, ForgeAgent, ActionHandler, Playwright, a bank,
or a real payment system. It does not make `GOVERNANCE_MODE=enforce` available.
Results from this harness are engineering evidence, not compliance evidence.

## Canonical Facts

Each payment has these trusted business fields:

- payment ID;
- beneficiary ID;
- positive amount with two decimal places;
- currency: CNY, USD, or EUR;
- reference;
- object version;
- status: `draft` or `submitted`.

The governed operation is `synthetic.payment.submit`, with the only allowed
transition:

```text
draft -> submitted
```

## Fixed Test Identities

| Account | Role | Department | Use |
|---|---|---|---|
| `operator` | operator | synthetic payments | Creates the request |
| `approver` | approver | synthetic payments | Approves high-risk payments |
| `compliance` | approver | synthetic compliance | Approves critical payments |
| `viewer` | viewer | synthetic payments | Negative authorization test |

These identities exist only in memory. There are no passwords or production
credentials.

## Deterministic Policy

- every submission requires approval;
- amount below 100,000 is high risk and requires the payments approver;
- amount at or above 100,000 is critical and requires the compliance approver;
- requester and approver must be different users;
- both users must belong to the fixed synthetic tenant and business line;
- approval binds the current canonical object version and action fingerprint;
- one permit authorizes one submission attempt for 60 seconds;
- the permit is consumed before the business-system call;
- a used permit and idempotency key are never replayed.

## Result Probe and Faults

Browser or API success is not treated as business success. The result probe
reads canonical payment state and matches the attempt idempotency key.

| Fault mode | Business effect | Required outcome |
|---|---|---|
| `none` | Commit and response succeed | `CONFIRMED` |
| `fail_before_commit` | No commit | `FAILED`; permit remains consumed |
| `commit_then_timeout` | Commit succeeds, response is lost | Probe confirms; no replay |
| `commit_then_inconclusive` | Commit succeeds, state is temporarily unreadable | `UNKNOWN`; probe only, no replay |

An UNKNOWN attempt can become CONFIRMED or FAILED only through a later result
probe. Calling execute again is rejected.

## Local Console

Start the isolated console from the repository root:

```powershell
.\.venv\Scripts\python.exe -m uvicorn enterprise.domains.synthetic_payment.app:app --host 127.0.0.1 --port 18081
```

Open `http://127.0.0.1:18081`. The page supports draft creation, approval,
single execution, fault injection, and UNKNOWN result probing.

## Browser audit perception (audit-only)

The console exposes a stable semantic DOM contract for the browser audit
collector in `enterprise/governance/browser_audit.py`:

- business fields use `data-governance-field`, `name`, `data-testid`, and
  `aria-label`;
- action affordances use `data-governance-action` and stable test IDs;
- the page declares readiness with `data-governance-page` and
  `data-governance-readiness`.

`collect_browser_audit_evidence` opens the page with Playwright, reads the DOM,
captures a screenshot, and discards the raw artifacts after deriving:

- an HMAC-bound page observation hash;
- HMAC-bound field and action references (never semantic names, DOM IDs, or form values);
- an HMAC screenshot fingerprint (never screenshot bytes);
- deterministic `ActionIntent` and `PolicyDecision` records from the existing
  audit analyzer;
- a `phase2-browser-audit-v1` evidence manifest.

This collector never clicks a button, submits a challenge, issues a permit, or
calls the synthetic business store. A loading/blocked marker is reported as a
human-review signal for state-changing candidates; a failed network-idle wait is
reported as `TRANSITIONING` with reduced confidence. The collector accepts only
the marked `http://127.0.0.1:<port>/` synthetic console and rejects redirects,
query strings, credentials, or unmarked pages. The browser evidence is
perception evidence only; it is separate from the synthetic business result
probe and does not authorize Skyvern or production enforce execution.

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:18081/health
```

The response must include:

```json
{
  "status": "ready",
  "domain_pack": "synthetic.payment",
  "production_eligible": false
}
```

## M3 offline SDK conformance

`enterprise/domains/synthetic_payment/sdk_manifest.py` provides the normative
M3 adapter:

```python
from enterprise.domains.synthetic_payment.sdk_manifest import build_pack_sdk_manifest
from enterprise.governance.pack_conformance import evaluate_static_pack_conformance

report = evaluate_static_pack_conformance(build_pack_sdk_manifest())
assert report.status == "pass"
```

The adapter reuses the existing synthetic Pack ID/version, `PaymentFacts`
fields, lifecycle, authorization dimensions, risk-policy reference, and result-
probe reference. It is not re-exported by the synthetic package and is not
imported by the active manifest, registry, harness, application, browser
collector, store, or result-probe implementation.

The accepted static report has schema
`domain-pack-conformance-report/v1` and manifest digest
`388542cfe97350b8c83a4e1d147ba74ecd40b9a534965e4750e74f5cc0940946`.
The normative in-memory negative cases reject:

- execute authority on the read-only capability with
  `read_only_authority_violation`;
- evidence older than the manifest ceiling with
  `evidence_freshness_exceeds_ceiling`;
- an undeclared lifecycle state with `lifecycle_state_unknown`; and
- a missing canonical-fact or evidence binding with `reference_missing`; and
- a result probe detached from the write capability's evidence list with
  `external_write_probe_missing`.

This report validates static contract completeness only. It does not read the
synthetic store, invoke the result probe, issue a Permit/Attempt, launch the
console, collect browser evidence, execute an Action, or make global enforce
available. Browser audit perception remains separate from authoritative
business evidence and result confirmation.

M3 validation evidence: the normative M3/M2 static suites passed `32` tests;
the existing synthetic Domain Pack, application, browser-audit, governance-
configuration, and master-status regressions passed `38` tests; focused Ruff,
compileall, and `git diff --check` also passed. All four M3 files are currently
untracked, so `git diff --check` did not inspect their contents; the M3 review
therefore also requires direct file and whitespace inspection.

## Acceptance Evidence

Run the focused suite with Python 3.11:

```powershell
.\.venv\Scripts\python.exe -m pytest -q `
  tests\unit\test_synthetic_payment_domain_pack.py `
  tests\unit\test_synthetic_payment_app.py `
  tests\unit\test_capability_resolver.py `
  tests\unit\test_work_orders.py `
  tests\unit\test_governor.py `
  tests\unit\test_permit_service.py `
  tests\unit\test_execution_attempt_service.py `
  tests\unit\test_recovery_policy.py `
  tests\unit\test_governance_config.py
```

Acceptance requires approval separation, critical routing, one-time permit
consumption, zero replay after ambiguous commits, UNKNOWN-stop behavior, and
continued rejection of global enforce configuration.

The browser audit acceptance additionally requires that semantic fields and
actions are discovered, screenshot output is fingerprint-only, raw HTML and
form values are absent from the manifest, and high-impact candidates remain
audit decisions rather than executed actions:

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium  # once per machine
.\.venv\Scripts\python.exe -m pytest -q `
  tests\unit\test_browser_audit_evidence.py `
  tests\e2e\test_synthetic_payment_browser_audit.py
```

## Remaining Production Gates

This reference pack does not satisfy the real Domain Pack or production
enforce gates. Production work still requires named business owners, canonical
production facts, current permission sources, real result probes, complete
browser-entry sealing, data-governance controls, and a separate approval for
tenant/workflow-scoped Skyvern integration.

## M4 synthetic governed browser proof

M4 adds a process-isolated evidence test only. It starts portable PostgreSQL,
applies the existing migrations, launches the synthetic console under Uvicorn
on `127.0.0.1`, and opens the installed Playwright Chromium binary. The browser
is observed with Skyvern's real scraper, and every setup interaction plus the
effecting Execute click is dispatched through `ActionHandler.handle_action`.
The M4 test and support layer contain no direct Playwright `click()` or
`dblclick()` call.

The accepted state and effect evidence is:

- the exact `synthetic:<challenge-id>` idempotency key binds the Action,
  observation, Permit, execution profile, durable attempt, and synthetic
  business attempt;
- a request-route probe reads the disposable PostgreSQL row and observes
  `EXECUTING` before releasing the single Execute request to localhost;
- browser transport completion persists the durable attempt as `UNKNOWN`, not
  `CONFIRMED`;
- the synthetic store commits once, advancing its canonical object version
  from `1` to `2`, while the challenge retains its original version-`1`
  authorization snapshot;
- a second `ActionHandler.handle_action` call with the same idempotency key is
  rejected with `ExecutionAttemptRecoveryRequired` before another browser
  request;
- an initially inconclusive probe leaves the attempt `UNKNOWN`; after clearing
  only the synthetic probe fault, a separate probe observes canonical version
  `2` and resolves the same durable attempt to `CONFIRMED`; and
- teardown closes Chromium and Uvicorn, stops PostgreSQL, closes both loopback
  ports, and removes only the validated `finrpa-m4-*` temporary root.

Focused M4 E2E evidence:

```powershell
$env:PYTHONUTF8='1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
.\.venv\Scripts\python.exe -m pytest `
  tests/e2e/test_synthetic_payment_governed_browser.py -q
```

Result: `1 passed` in `32.99s`. The real Chromium executable was
`C:\Users\zhang\AppData\Local\ms-playwright\chromium-1187\chrome-win\chrome.exe`.
Post-run inspection found zero retained `finrpa-m4-*` temporary roots and zero
M4 PostgreSQL, Uvicorn, Python, or Chromium processes.

Focused contract and adjacent regression evidence:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/test_m4_synthetic_governed_browser_contract.py `
  tests/unit/test_permit_service.py `
  tests/unit/test_execution_attempt_service.py `
  tests/unit/test_synthetic_payment_domain_pack.py `
  tests/unit/test_synthetic_payment_app.py `
  tests/unit/test_governance_config.py `
  tests/unit/test_governance_entrypoints.py `
  tests/unit/test_execution_entrypoint_inventory_fixtures.py -q
```

Result: `66 passed` in `1.88s`. Focused Ruff passed on all three authorized
Python files. Isolated `compileall` passed with `PYTHONPYCACHEPREFIX` directed
to a validated temporary root that was removed afterward. Direct inspection of
all four authorized files found zero trailing-whitespace lines and a final
newline in every file.

The support layer contains narrow test-only shims for optional Skyvern branches
that are not part of this locator proof. It also normalizes timezone-aware UTC
service values to the repository's existing timezone-naive governance columns
at the disposable asyncpg adapter boundary; no model, migration, schema, or
production runtime was changed. The deterministic select helper supplies only
the approved `commit_then_inconclusive` setup choice; the actual selection and
all clicks still execute through `ActionHandler` and real Playwright locators.

M4 does not install a Pack, add a runtime caller, connect a production system,
change dependencies or migrations, enable a registry, or make global enforce
available. `GOVERNANCE_MODE=enforce` remains configuration-rejected. Rollback
is limited to deleting the four M4 evidence files (or restoring this appended
section) and running the same scoped disposable-environment teardown; there is
no production payment, credential, database, registry, or browser state to
compensate.

## M5 cross-platform developer evidence

M5 keeps the M4 semantics intact and changes only executable discovery plus the
developer/release boundary. Windows resolves `.exe` PostgreSQL programs and
the Playwright Windows cache; Linux/WSL resolves ordinary PostgreSQL programs,
the Playwright Linux cache, or a Chromium executable on `PATH`. Explicit
`FINRPA_POSTGRES_BIN` and `FINRPA_CHROMIUM_EXECUTABLE` overrides remain paths,
not connections or credentials.

The canonical commands are:

```text
python scripts/finrpa_release.py doctor
python scripts/finrpa_release.py conformance
python scripts/finrpa_release.py demo
python scripts/finrpa_release.py report
```

`demo` invokes this exact accepted M4 E2E; it does not duplicate or bypass
ActionHandler, Permit/Attempt ordering, UNKNOWN, idempotency rejection,
independent probing, or teardown. Passing evidence uses
`finrpa.release-report/v1`, records the accepted M4 fingerprint, validates a
canonical SHA-256 digest, and reports zero retained validated temporary roots.
Reports contain no environment values, credentials, connection strings, raw
DOM, screenshot, or payment field.

Ubuntu CI runs the same proof with PostgreSQL and Playwright Chromium. Windows
smoke is manual or release-triggered and uses the same CLI. Neither CI nor the
CLI installs a production Pack, changes the active registry, connects a real
system, makes a production API call, publishes a release/package, deploys, or
enables global enforce.
