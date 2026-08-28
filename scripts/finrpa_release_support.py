"""Standard-library support for the FinRPA M5 developer release CLI."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

REPORT_SCHEMA = "finrpa.release-report/v1"
M4_ACCEPTED_FINGERPRINT = "743d05d191b3d96440161c70c04fd0a6ea5bf940b8b89e2d421ac94be6cb75b4"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "artifacts" / "m5"
LATEST_JSON = "latest.json"
LATEST_MARKDOWN = "latest.md"
CONFORMANCE_TESTS = (
    "tests/unit/test_pack_sdk_static_conformance.py",
    "tests/unit/test_synthetic_payment_pack_conformance.py",
)
DEMO_TEST = "tests/e2e/test_synthetic_payment_governed_browser.py"
REQUIRED_DEMO_PACKAGES = ("alembic", "asyncpg", "fastapi", "playwright", "pytest", "sqlalchemy", "uvicorn")
FORBIDDEN_REPORT_TERMS = (
    "api_key",
    "access_token",
    "client_secret",
    "password",
    "credential",
    "raw_dom",
    "screenshot",
    "database_string",
)


class ReleaseContractError(RuntimeError):
    """Raised when an M5 safety or evidence contract fails closed."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


def evidence_digest(report_without_digest: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(report_without_digest)).hexdigest()


def _distribution_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def postgres_executable(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def find_postgres_bin() -> Path | None:
    override = os.environ.get("FINRPA_POSTGRES_BIN")
    initdb = shutil.which(postgres_executable("initdb"))
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    if initdb:
        candidates.append(Path(initdb).resolve().parent)
    if os.name == "nt":
        candidates.append(Path("E:/tmp/postgresql-14.23-portable/pgsql/bin"))
    required = tuple(postgres_executable(name) for name in ("initdb", "pg_ctl", "createdb", "pg_isready"))
    return next((path.resolve() for path in candidates if all((path / name).is_file() for name in required)), None)


def find_chromium() -> Path | None:
    override = os.environ.get("FINRPA_CHROMIUM_EXECUTABLE")
    candidates: list[Path] = [Path(override)] if override else []
    browser_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if browser_root and browser_root != "0":
        roots = [Path(browser_root)]
    elif os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        roots = [Path(local_app_data) / "ms-playwright"] if local_app_data else []
    else:
        roots = [Path.home() / ".cache" / "ms-playwright"]
    patterns = (
        "chromium-*/chrome-win/chrome.exe",
        "chromium-*/chrome-linux/chrome",
        "chromium_headless_shell-*/chrome-linux/headless_shell",
        "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
    )
    for root in roots:
        for pattern in patterns:
            candidates.extend(sorted(root.glob(pattern), reverse=True))
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        if executable := shutil.which(name):
            candidates.append(Path(executable))
    return next((path.resolve() for path in candidates if path.is_file()), None)


def assert_loopback_target(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ReleaseContractError("FINRPA_SYNTHETIC_TARGET_URL must be unauthenticated http://127.0.0.1")
    return value


def doctor_result(*, require_demo: bool = True) -> dict[str, Any]:
    unsafe_target = os.environ.get("FINRPA_SYNTHETIC_TARGET_URL")
    target_ok = True
    target_detail = "unset; demo allocates an ephemeral 127.0.0.1 target"
    if unsafe_target:
        try:
            assert_loopback_target(unsafe_target)
            target_detail = "explicit loopback target accepted"
        except ReleaseContractError as exc:
            target_ok, target_detail = False, str(exc)

    postgres_bin = find_postgres_bin()
    chromium = find_chromium()
    package_versions = {name: _distribution_version(name) for name in REQUIRED_DEMO_PACKAGES}
    checks = [
        {
            "check_id": "python-version",
            "status": "pass" if (3, 11) <= sys.version_info[:2] < (3, 14) else "fail",
            "detail": f"Python {platform.python_version()} (requires >=3.11,<3.14)",
        },
        {
            "check_id": "project-root",
            "status": "pass" if (ROOT / "pyproject.toml").is_file() else "fail",
            "detail": "pyproject.toml present",
        },
        {"check_id": "loopback-boundary", "status": "pass" if target_ok else "fail", "detail": target_detail},
    ]
    if require_demo:
        checks.extend(
            [
                {
                    "check_id": "postgresql-binaries",
                    "status": "pass" if postgres_bin else "fail",
                    "detail": "found" if postgres_bin else "set FINRPA_POSTGRES_BIN or add PostgreSQL binaries to PATH",
                },
                {
                    "check_id": "chromium-binary",
                    "status": "pass" if chromium else "fail",
                    "detail": "found" if chromium else "run `python -m playwright install chromium`",
                },
                {
                    "check_id": "demo-packages",
                    "status": "pass" if all(package_versions.values()) else "fail",
                    "detail": "all installed"
                    if all(package_versions.values())
                    else "missing: " + ", ".join(name for name, version in package_versions.items() if version is None),
                },
            ]
        )
    return {
        "command": "doctor",
        "status": "pass" if all(check["status"] == "pass" for check in checks) else "fail",
        "platform": {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version()},
        "checks": checks,
        "side_effects_started": False,
    }


def command_environment() -> dict[str, str]:
    allowed_names = {
        "APPDATA",
        "CI",
        "COMSPEC",
        "FINRPA_CHROMIUM_EXECUTABLE",
        "FINRPA_POSTGRES_BIN",
        "GITHUB_ACTIONS",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PLAYWRIGHT_BROWSERS_PATH",
        "PYTHONPATH",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "USERPROFILE",
        "VIRTUAL_ENV",
        "WINDIR",
    }
    environment = {name: value for name, value in os.environ.items() if name.upper() in allowed_names}
    environment.update({"PYTHONUTF8": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "GOVERNANCE_MODE": "off"})
    return environment


def run_pytest(check_id: str, paths: Sequence[str]) -> dict[str, Any]:
    command = [sys.executable, "-m", "pytest", *paths, "-q"]
    completed = subprocess.run(command, cwd=ROOT, env=command_environment(), check=False)
    return {
        "check_id": check_id,
        "command": ["python", "-m", "pytest", *paths, "-q"],
        "exit_code": completed.returncode,
        "status": "pass" if completed.returncode == 0 else "fail",
    }


def cleanup_result(command: str) -> dict[str, Any]:
    retained = sorted(Path(tempfile.gettempdir()).glob("finrpa-m4-*"))
    result = {
        "status": "pass" if not retained else "fail",
        "retained_temp_roots": len(retained),
        "validated_by": DEMO_TEST if command == "demo" else "no runtime state created",
    }
    if retained:
        result["detail"] = "validated M4 temporary state was retained"
    return result


def _postgres_version(postgres_bin: Path | None) -> str:
    if postgres_bin is None:
        return "not-required-or-not-found"
    executable = postgres_bin / postgres_executable("postgres")
    if not executable.is_file():
        return postgres_bin.name
    completed = subprocess.run(
        [executable, "--version"],
        cwd=ROOT,
        env=command_environment(),
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    return completed.stdout.strip() if completed.returncode == 0 else postgres_bin.name


def _chromium_revision(chromium: Path | None) -> str:
    if chromium is None:
        return "not-required-or-not-found"
    for parent in chromium.parents:
        if parent.name.startswith(("chromium-", "chromium_headless_shell-")):
            return parent.name
    return chromium.name


def resolved_versions() -> dict[str, Any]:
    postgres_bin = find_postgres_bin()
    chromium = find_chromium()
    return {
        "python": platform.python_version(),
        "project": _distribution_version("finrpa-enterprise") or "0.1.0",
        "postgresql": _postgres_version(postgres_bin),
        "chromium": _chromium_revision(chromium),
        "playwright": _distribution_version("playwright") or "not-installed",
    }


def build_report(command: str, checks: Sequence[Mapping[str, Any]], cleanup: Mapping[str, Any]) -> dict[str, Any]:
    status = "pass" if all(check.get("status") == "pass" for check in checks) and cleanup.get("status") == "pass" else "fail"
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "command": command,
        "status": status,
        "platform": {"system": platform.system(), "machine": platform.machine()},
        "resolved_versions": resolved_versions(),
        "checks": list(checks),
        "m4_fingerprint": M4_ACCEPTED_FINGERPRINT,
        "cleanup": dict(cleanup),
        "limitations": [
            "synthetic and loopback only",
            "no production Domain Pack or tenant installation",
            "global GOVERNANCE_MODE=enforce remains unavailable",
            "browser transport success is not business confirmation",
        ],
    }
    report["evidence_digest"] = evidence_digest(report)
    validate_report(report)
    return report


def validate_report(report: Mapping[str, Any]) -> None:
    required = {
        "schema",
        "command",
        "status",
        "platform",
        "resolved_versions",
        "checks",
        "m4_fingerprint",
        "cleanup",
        "limitations",
        "evidence_digest",
    }
    if set(report) != required or report.get("schema") != REPORT_SCHEMA:
        raise ReleaseContractError("release report schema mismatch")
    if report.get("command") not in {"conformance", "demo"} or report.get("status") not in {"pass", "fail"}:
        raise ReleaseContractError("release report command/status mismatch")
    digest_value = report.get("evidence_digest")
    without_digest = dict(report)
    without_digest.pop("evidence_digest", None)
    if digest_value != evidence_digest(without_digest):
        raise ReleaseContractError("release report evidence digest mismatch")
    serialized = canonical_json_bytes(report).decode("utf-8").lower()
    if any(term in serialized for term in FORBIDDEN_REPORT_TERMS):
        raise ReleaseContractError("release report contains a forbidden secret or raw-evidence field")
    if report.get("cleanup", {}).get("status") != "pass" and report.get("status") == "pass":
        raise ReleaseContractError("successful report lacks passing cleanup evidence")


def render_markdown(report: Mapping[str, Any]) -> str:
    validate_report(report)
    checks = "\n".join(
        f"- `{check['check_id']}`: **{check['status']}** (exit {check.get('exit_code', 'n/a')})"
        for check in report["checks"]
    )
    limitations = "\n".join(f"- {item}" for item in report["limitations"])
    return (
        "# FinRPA M5 release evidence\n\n"
        f"- Schema: `{report['schema']}`\n"
        f"- Command: `{report['command']}`\n"
        f"- Status: **{report['status']}**\n"
        f"- Evidence digest: `{report['evidence_digest']}`\n"
        f"- Accepted M4 fingerprint: `{report['m4_fingerprint']}`\n\n"
        "## Checks\n\n"
        f"{checks}\n\n"
        "## Cleanup\n\n"
        f"- Status: **{report['cleanup']['status']}**\n"
        f"- Retained temporary roots: `{report['cleanup']['retained_temp_roots']}`\n\n"
        "## Limitations\n\n"
        f"{limitations}\n"
    )


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass


def write_report(report: Mapping[str, Any], output_dir: Path = DEFAULT_OUTPUT) -> tuple[Path, Path]:
    validate_report(report)
    json_path, markdown_path = output_dir / LATEST_JSON, output_dir / LATEST_MARKDOWN
    _atomic_write(json_path, canonical_json_bytes(report) + b"\n")
    _atomic_write(markdown_path, render_markdown(report).encode("utf-8"))
    return json_path, markdown_path


def load_report(output_dir: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    path = output_dir / LATEST_JSON
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseContractError(f"no valid release report at {path.relative_to(ROOT)}") from exc
    validate_report(report)
    return report
