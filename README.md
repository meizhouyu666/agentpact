# AgentPact

# Governed Browser-Agent Harness, Domain Pack SDK & Conformance Kit

AgentPact is a synthetic-only, non-production governed browser-agent reference
built on Skyvern. It demonstrates typed Domain Pack contracts, one-time
execution permits, durable attempt state, `UNKNOWN`/no-replay recovery, and
independent result confirmation without connecting to a real payment system.

## Run the synthetic proof

Use Python 3.11, 3.12, or 3.13. PostgreSQL 14+ must provide `initdb`, `pg_ctl`,
`createdb`, and `pg_isready` on `PATH`. The following blocks are directly
executable from the repository root and use the virtual-environment Python for
every installation and AgentPact command. The compatible command path remains
`scripts/finrpa_release.py`.

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

macOS is best-effort and is not a release gate. Set `FINRPA_POSTGRES_BIN` to
the PostgreSQL binary directory or `FINRPA_CHROMIUM_EXECUTABLE` to an installed
Chromium executable only when normal discovery is unavailable. These overrides
are executable paths, never credentials.

The commands return `0` for success, `2` for a missing or unsafe prerequisite
or invalid evidence, and `3` for a failed conformance/demo check. Successful
`conformance` and `demo` runs write canonical JSON and Markdown to the ignored
`artifacts/m5/` directory; `report` validates the evidence digest before
rendering it.

## What the proof demonstrates

Skyvern's `ActionHandler` remains the sole browser executor. The M4 proof
records durable `EXECUTING` before the browser effect, records an inconclusive
transport result as `UNKNOWN`, rejects replay under the same idempotency key,
and confirms the result only through an independently invoked probe. Exactly
one synthetic commit is made against a disposable loopback console.

Before success, teardown closes Chromium and Uvicorn, stops PostgreSQL, closes
loopback ports, and removes the validated temporary root. Browser transport
success is never treated as business confirmation.

## Boundary and limitations

- No real payment data, credentials, production API calls, or production
  Domain Pack are included.
- There is no deployment, package publication, migration, tenant installation,
  or production runtime path.
- There is no active-registry or Planner/ForgeAgent runtime wiring.
- Global `GOVERNANCE_MODE=enforce` remains configuration-rejected.
- This repository is a developer reference and evidence harness, not an
  operational financial system.

See the [M5 developer guide](docs/phase-2/m5-developer-release-guide.md),
[product Charter](docs/phase-2/final-product-charter.md), and [NOTICE](NOTICE.md)
for detailed reproducibility, limitations, licensing, and upstream attribution.
The public repository is [meizhouyu666/agentpact](https://github.com/meizhouyu666/agentpact).

## License and attribution

This repository is licensed under the [MIT License](LICENSE). It is derived
from [Skyvern](https://github.com/Skyvern-AI/skyvern); see [NOTICE](NOTICE.md)
for upstream copyright and license details.
