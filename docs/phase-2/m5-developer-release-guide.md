# AgentPact M5 developer and release guide

> Status: AgentPact portfolio-ready synthetic release experience, locally DONE/PASS
>
> Runtime boundary: unchanged. Production Pack installation, runtime wiring,
> deployment, and global enforce remain unavailable.

AgentPact is the **Governed Browser-Agent Harness, Domain Pack SDK & Conformance Kit**
public brand. The executable `finrpa` command, environment,
artifact, workflow-path, and report-schema identifiers below remain stable
compatibility contracts.

## Canonical interface and installation

Run from the repository root with Python 3.11, 3.12, or 3.13. Each supported
platform block creates `.venv` and then uses that environment's Python for
package installation, Playwright installation, and all four canonical commands.

### Windows PowerShell

```powershell
py -3.11 -m venv .venv
$VenvPython = (Resolve-Path .venv\Scripts\python.exe).Path
& $VenvPython -m pip install -e . -r requirements-m5-demo.lock
& $VenvPython -m playwright install chromium
& $VenvPython scripts\finrpa_release.py doctor
& $VenvPython scripts\finrpa_release.py conformance
& $VenvPython scripts\finrpa_release.py demo
& $VenvPython scripts\finrpa_release.py report
```

### Linux/WSL

```bash
python3.11 -m venv .venv
VENV_PYTHON="$(pwd)/.venv/bin/python"
"$VENV_PYTHON" -m pip install -e . -r requirements-m5-demo.lock
"$VENV_PYTHON" -m playwright install chromium
"$VENV_PYTHON" scripts/finrpa_release.py doctor
"$VENV_PYTHON" scripts/finrpa_release.py conformance
"$VENV_PYTHON" scripts/finrpa_release.py demo
"$VENV_PYTHON" scripts/finrpa_release.py report
```

`doctor` is read-only. It checks the Python range, project root, loopback-only
target rule, PostgreSQL binaries, Chromium, and demo packages without starting
a subprocess or service. `conformance` runs the deterministic M1-M3 offline
contract suites. `demo` invokes the accepted M4 pytest proof rather than
reimplementing its ActionHandler, Permit, Attempt, UNKNOWN, result-probe,
idempotency, or cleanup logic. `report` validates and renders the latest
evidence.

Exit codes are `0` for success, `2` for a missing/unsafe prerequisite or
invalid report, and `3` for a failed conformance/demo command.

Install PostgreSQL 14 or newer and place `initdb`, `pg_ctl`, `createdb`, and
`pg_isready` on `PATH`. If necessary:

```text
FINRPA_POSTGRES_BIN=<directory containing the four PostgreSQL programs>
FINRPA_CHROMIUM_EXECUTABLE=<installed Chromium executable>
```

The CLI never installs system software. Missing programs produce actionable
diagnostics. macOS discovery is best-effort; Windows and Linux/WSL are the
release-supported local platforms.

## What the demo proves

1. The synthetic console, PostgreSQL, and Chromium are disposable and bound to
   `127.0.0.1`.
2. Skyvern's public `ActionHandler.handle_action` is the only path for the
   effecting click.
3. The durable Attempt is `EXECUTING` before the browser request is released.
4. A committed-but-inconclusive response becomes durable `UNKNOWN`.
5. The same idempotency key is rejected before a second browser request.
6. A separately invoked canonical result probe resolves the same Attempt to
   `CONFIRMED` after observing exactly one synthetic commit.
7. Chromium, Uvicorn, PostgreSQL, ports, and the validated temporary root are
   gone before success.

Transport success is not confirmation. An inconclusive result stays UNKNOWN
and is never automatically replayed.

## Evidence format

Successful `conformance` and `demo` commands atomically write ignored files:

- `artifacts/m5/latest.json`
- `artifacts/m5/latest.md`

JSON uses schema `finrpa.release-report/v1`. Its SHA-256 `evidence_digest`
covers the canonical report without the digest field. The report includes the
command, platform, resolved tool versions, check outcomes, accepted M4
fingerprint, cleanup result, and limitations. It excludes environment values,
API keys, credentials, connection strings, raw DOM, screenshots, payment
fields, and ordinary command output. `report` fails closed on a schema or
digest mismatch.

## CI and release boundary

`.github/workflows/finrpa-release.yml` runs Ubuntu conformance on Python
3.11-3.13 and the Ubuntu synthetic E2E on Python 3.11. A Windows 2022 smoke job
runs only for manual dispatch or an already-created release event. Jobs use the
same Python CLI and upload only the JSON/Markdown evidence.

The workflow does not push, tag, publish a GitHub Release, publish a package,
deploy, run a production migration, install a production Pack, or enable global
enforce. Release publication remains a separate owner action.

## Troubleshooting and cleanup

- PostgreSQL missing: set `FINRPA_POSTGRES_BIN` or add its binary directory to
  `PATH`.
- Chromium missing: rerun the platform block's `$VenvPython -m playwright
  install chromium` (Windows) or `"$VENV_PYTHON" -m playwright install
  chromium` (Linux/WSL), or set the explicit executable override.
- Unsafe target: remove `FINRPA_SYNTHETIC_TARGET_URL`; if set, only an
  unauthenticated `http://127.0.0.1:<port>/` value is accepted.
- Failed demo: inspect pytest output. A failure may retain only a validated
  `finrpa-m4-*` temporary root for diagnosis; never use `git clean` or delete an
  uncertain path. Stop its recorded processes, verify the exact root prefix,
  and remove only that disposable root.

No cleanup step operates on the repository, user home, virtual environment,
or any path that was not created by the M4 support layer.
