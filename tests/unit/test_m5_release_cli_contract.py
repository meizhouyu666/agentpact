"""Static and focused functional contracts for the M5 release interface."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from finrpa_release_support import (  # noqa: E402
    M4_ACCEPTED_FINGERPRINT,
    ReleaseContractError,
    assert_loopback_target,
    build_report,
    canonical_json_bytes,
    validate_report,
)

CLI = SCRIPTS / "finrpa_release.py"
SUPPORT = SCRIPTS / "finrpa_release_support.py"
WORKFLOW = ROOT / ".github" / "workflows" / "finrpa-release.yml"
README = ROOT / "README.md"
RELEASE_GUIDE = ROOT / "docs" / "phase-2" / "m5-developer-release-guide.md"
CHARTER = ROOT / "docs" / "phase-2" / "final-product-charter.md"
MASTER_STATUS = ROOT / "docs" / "phase-2" / "phase-2-master-status.md"
NOTICE = ROOT / "NOTICE.md"
PYPROJECT = ROOT / "pyproject.toml"
BRAND = "AgentPact"
TAGLINE = "Governed Browser-Agent Harness, Domain Pack SDK & Conformance Kit"
M5_HEAD = "d1c2587b2b03ae107429e1cd131dd5bc5082c390"
M5_REVIEW_SHA256 = "d73196352e8f06512d69fc92a86127f19e370c11ffaf31f67183a39c55153707"


def fenced_block_after_heading(document: str, heading: str) -> str:
    section = document.split(heading, maxsplit=1)[1]
    fenced = section.split("```", maxsplit=2)[1]
    return fenced.split("\n", maxsplit=1)[1]


def test_cli_help_exposes_only_the_four_canonical_commands() -> None:
    completed = subprocess.run([sys.executable, str(CLI), "--help"], cwd=ROOT, text=True, capture_output=True, check=True)
    assert "{doctor,conformance,demo,report}" in completed.stdout
    for command in ("doctor", "conformance", "demo", "report"):
        assert command in completed.stdout


@pytest.mark.parametrize(
    "target",
    [
        "https://127.0.0.1:9000/",
        "http://localhost:9000/",
        "http://10.0.0.1:9000/",
        "http://user@127.0.0.1:9000/",
        "http://127.0.0.1:9000/?token=secret",
    ],
)
def test_non_loopback_or_credentialed_targets_fail_closed(target: str) -> None:
    with pytest.raises(ReleaseContractError):
        assert_loopback_target(target)
    assert assert_loopback_target("http://127.0.0.1:9000/") == "http://127.0.0.1:9000/"


def test_release_report_digest_is_canonical_and_tamper_evident() -> None:
    check = {"check_id": "contract", "command": ["python", "-m", "pytest", "-q"], "exit_code": 0, "status": "pass"}
    report = build_report(
        "conformance",
        [check],
        {"status": "pass", "retained_temp_roots": 0, "validated_by": "no runtime state created"},
    )
    validate_report(json.loads(canonical_json_bytes(report)))
    assert report["m4_fingerprint"] == M4_ACCEPTED_FINGERPRINT
    assert len(report["evidence_digest"]) == 64

    changed = dict(report)
    changed["status"] = "fail"
    with pytest.raises(ReleaseContractError, match="digest"):
        validate_report(changed)


def test_release_sources_never_install_or_target_production() -> None:
    cli = CLI.read_text(encoding="utf-8")
    support = SUPPORT.read_text(encoding="utf-8")
    combined = (cli + support).lower()
    assert "pip install" not in combined
    assert "subprocess.run" in support
    assert "governance_mode\": \"off" in combined
    assert "governance_mode\": \"enforce" not in combined
    assert "domainpackregistry" not in combined
    assert "http://127.0.0.1" in support
    assert "urlopen(" not in combined
    assert "allowed_names = {" in support
    assert "os.environ.copy()" not in support
    assert "stdout" not in report_field_names()


def report_field_names() -> set[str]:
    report = build_report(
        "conformance",
        [{"check_id": "contract", "command": ["python"], "exit_code": 0, "status": "pass"}],
        {"status": "pass", "retained_temp_roots": 0, "validated_by": "no runtime state created"},
    )
    return set(report)


def test_ci_uses_same_cli_and_contains_no_publish_or_production_secret_step() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert '["3.11", "3.12", "3.13"]' in workflow
    assert "python scripts/finrpa_release.py conformance" in workflow
    assert "python scripts/finrpa_release.py demo" in workflow
    assert "windows-2022" in workflow
    assert "github.event_name == 'workflow_dispatch' || github.event_name == 'release'" in workflow
    assert "actions/upload-artifact@v4" in workflow
    forbidden = ("secrets.", "docker login", "twine upload", "gh release create", "git push", "GOVERNANCE_MODE=enforce")
    assert all(term not in workflow for term in forbidden)


def test_agentpact_public_brand_preserves_finrpa_compatibility_contracts() -> None:
    readme = README.read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")
    guide = RELEASE_GUIDE.read_text(encoding="utf-8")
    charter = CHARTER.read_text(encoding="utf-8")
    status = MASTER_STATUS.read_text(encoding="utf-8")
    notice = NOTICE.read_text(encoding="utf-8")
    metadata = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]

    assert metadata["name"] == "agentpact"
    assert metadata["description"] == TAGLINE
    assert workflow.startswith(f"name: {BRAND} synthetic release evidence\n")
    for document in (readme, guide, charter, status, notice):
        assert BRAND in document
    for document in (readme, guide, charter):
        assert TAGLINE in document
    assert "https://github.com/meizhouyu666/agentpact" in readme

    for identifier in (
        "scripts/finrpa_release.py",
        "FINRPA_POSTGRES_BIN",
        "FINRPA_CHROMIUM_EXECUTABLE",
        "finrpa.release-report/v1",
        "artifacts/m5/",
    ):
        assert identifier in readme + guide + status
    assert WORKFLOW.name == "finrpa-release.yml"
    assert "finrpa-conformance-${{ matrix.python-version }}" in workflow
    assert "finrpa-synthetic-demo" in workflow
    assert "finrpa-windows-release-smoke" in workflow


def test_m5_authority_documents_record_local_done_pass() -> None:
    charter = CHARTER.read_text(encoding="utf-8")
    status = MASTER_STATUS.read_text(encoding="utf-8")
    for document in (charter, status):
        assert "DONE/PASS" in document
        assert M5_HEAD in document
        assert M5_REVIEW_SHA256 in document


@pytest.mark.parametrize("path", [README, RELEASE_GUIDE])
def test_supported_setup_blocks_never_fall_back_to_ambient_python(path: Path) -> None:
    document = path.read_text(encoding="utf-8")
    windows = fenced_block_after_heading(document, "### Windows PowerShell")
    linux = fenced_block_after_heading(document, "### Linux/WSL")

    assert "py -3.11 -m venv .venv" in windows
    assert "$VenvPython = (Resolve-Path .venv\\Scripts\\python.exe).Path" in windows
    assert 'python3.11 -m venv .venv' in linux
    assert 'VENV_PYTHON="$(pwd)/.venv/bin/python"' in linux

    for operation in ("-m pip install", "-m playwright install chromium"):
        assert f"& $VenvPython {operation}" in windows
        assert f'"$VENV_PYTHON" {operation}' in linux
    for command in ("doctor", "conformance", "demo", "report"):
        assert f"& $VenvPython scripts\\finrpa_release.py {command}" in windows
        assert f'"$VENV_PYTHON" scripts/finrpa_release.py {command}' in linux

    for line in windows.splitlines():
        if "-m pip" in line or "-m playwright" in line or "finrpa_release.py" in line:
            assert line.startswith("& $VenvPython ")
    for line in linux.splitlines():
        if "-m pip" in line or "-m playwright" in line or "finrpa_release.py" in line:
            assert line.startswith('"$VENV_PYTHON" ')


def test_release_readme_is_concise_synthetic_only_and_non_production() -> None:
    readme = README.read_text(encoding="utf-8")
    lowered = readme.casefold()
    assert len(readme.splitlines()) < 120
    assert readme.isascii()
    assert "synthetic-only, non-production" in lowered
    assert "there is no deployment" in lowered
    forbidden = (
        "make dev-prod",
        "default credentials",
        "production-ready",
        "day-14",
        "legacy project overview",
        "img.shields.io",
        "admin123",
        "finrpa123",
    )
    assert all(term not in lowered for term in forbidden)


def test_doctor_does_not_start_subprocesses() -> None:
    source = SUPPORT.read_text(encoding="utf-8")
    doctor = source[source.index("def doctor_result") : source.index("def command_environment")]
    assert "subprocess" not in doctor
    assert "side_effects_started" in doctor
    assert os.name in {"nt", "posix"}


def test_m4_discovery_is_windows_and_posix_aware_without_fixed_commands() -> None:
    source = (ROOT / "tests" / "e2e" / "m4_synthetic_support.py").read_text(encoding="utf-8")
    assert 'return f"{name}.exe" if os.name == "nt" else name' in source
    assert '"chromium-*/chrome-win/chrome.exe"' in source
    assert '"chromium-*/chrome-linux/chrome"' in source
    assert 'shutil.which(postgres_executable("initdb"))' in source
    assert 'postgres_bin / "initdb.exe"' not in source
    assert 'postgres_bin / "pg_ctl.exe"' not in source
    assert 'postgres_socket = root / "postgres-socket"' in source
    assert 'postgres_options += f" -k {postgres_socket}"' in source
    assert 'postgres log:\\n{server_log}' in source
    assert "capture_output=False" in source
