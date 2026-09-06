"""Run a bounded Stripe Gate 3 batch against an isolated local PostgreSQL.

This is an experiment composition edge, not an AgentPact runtime dependency.
It creates a temporary loopback-only PostgreSQL cluster, applies migrations,
runs independent Gate 2 pairs one at a time, and stops immediately after an
environment/fairness failure or a preserved hard-gate violation.

The Stripe key and browser/PostgreSQL executable paths may be loaded from an
explicit ``--env-file``. Secrets are kept in process memory and are never
written to the batch index or pair artifacts.
"""

from __future__ import annotations

# ruff: noqa: E402
import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from enterprise.auth.models import BusinessLineModel, DepartmentModel
from enterprise.domains.stripe_payment.constants import (
    BUSINESS_LINE_ID,
    COMPLIANCE_DEPARTMENT_ID,
    PAYMENTS_DEPARTMENT_ID,
    TENANT_ID,
)
from scripts.stripe_gate2_pair import _run_pair
from skyvern.forge.sdk.db.models import OrganizationModel

BATCH_SCHEMA_VERSION = "agentpact.stripe-gate3-batch.v1"
PairRunner = Callable[[str, Path], Awaitable[dict[str, Any]]]


def _postgres_executable(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def _find_postgres_bin() -> Path:
    override = os.environ.get("FINRPA_POSTGRES_BIN")
    candidates = [Path(override) if override else None]
    if discovered := shutil.which(_postgres_executable("initdb")):
        candidates.append(Path(discovered).resolve().parent)
    required = tuple(_postgres_executable(name) for name in ("initdb", "pg_ctl", "createdb"))
    for candidate in candidates:
        if candidate and all((candidate / executable).is_file() for executable in required):
            return candidate.resolve()
    raise RuntimeError(
        "Gate 3 requires FINRPA_POSTGRES_BIN (or PATH) containing " + ", ".join(required)
    )


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _is_loopback_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _wait_for_port(port: int, *, expected_open: bool, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_loopback_port_open(port) is expected_open:
            return
        time.sleep(0.1)
    raise RuntimeError(f"Timed out waiting for local PostgreSQL port open={expected_open}")


def _run_command(
    command: list[Path | str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: int = 300,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [str(item) for item in command],
        cwd=ROOT,
        env=dict(env) if env is not None else None,
        text=True,
        stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture_output else subprocess.DEVNULL,
        timeout=timeout,
        check=False,
    )
    if check and completed.returncode != 0:
        executable = Path(str(command[0])).name
        raise RuntimeError(
            f"Gate 3 setup command failed ({executable}, exit={completed.returncode}): "
            f"{(completed.stderr or '').strip()}"
        )
    return completed


async def _seed_stripe_benchmark_tenant(database_url: str) -> None:
    """Install the explicit local tenant boundary required by the G arm."""

    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            async with session.begin():
                session.add(
                    OrganizationModel(
                        organization_id=TENANT_ID,
                        organization_name="AgentPact Stripe Gate 3 Benchmark",
                    )
                )
                await session.flush()
                session.add_all(
                    [
                        DepartmentModel(
                            department_id=PAYMENTS_DEPARTMENT_ID,
                            organization_id=TENANT_ID,
                            department_name="Stripe payments",
                            department_code="stripe-payments",
                        ),
                        DepartmentModel(
                            department_id=COMPLIANCE_DEPARTMENT_ID,
                            organization_id=TENANT_ID,
                            department_name="Stripe compliance",
                            department_code="stripe-compliance",
                        ),
                        BusinessLineModel(
                            business_line_id=BUSINESS_LINE_ID,
                            organization_id=TENANT_ID,
                            line_name="Stripe treasury",
                            line_code="stripe-treasury",
                        ),
                    ]
                )
    finally:
        await engine.dispose()


@contextmanager
def _isolated_postgres() -> Iterator[str]:
    postgres_bin = _find_postgres_bin()
    root = Path(tempfile.mkdtemp(prefix="agentpact-gate3-pg-"))
    data = root / "data"
    log = root / "postgres.log"
    socket_dir = root / "socket"
    port = _reserve_loopback_port()
    try:
        _run_command(
            [
                postgres_bin / _postgres_executable("initdb"),
                "-D",
                data,
                "--username=postgres",
                "--auth=trust",
                "--encoding=UTF8",
                "--no-locale",
            ]
        )
        options = f"-p {port} -h 127.0.0.1"
        if os.name != "nt":
            socket_dir.mkdir()
            options += f" -k {socket_dir}"
        _run_command(
            [
                postgres_bin / _postgres_executable("pg_ctl"),
                "-D",
                data,
                "-l",
                log,
                "-o",
                options,
                "-w",
                "-t",
                "30",
                "start",
            ],
            capture_output=False,
        )
        _wait_for_port(port, expected_open=True)
        _run_command(
            [
                postgres_bin / _postgres_executable("createdb"),
                "--host=127.0.0.1",
                f"--port={port}",
                "--username=postgres",
                "agentpact_gate3",
            ]
        )
        database_url = f"postgresql+asyncpg://postgres@127.0.0.1:{port}/agentpact_gate3"
        migration_env = os.environ.copy()
        migration_env.update(
            DATABASE_STRING=database_url,
            DATABASE_REPLICA_STRING=database_url,
            GOVERNANCE_MODE="off",
        )
        _run_command([Path(sys.executable), "-m", "alembic", "upgrade", "heads"], env=migration_env)
        asyncio.run(_seed_stripe_benchmark_tenant(database_url))
        yield database_url
    finally:
        if (data / "postmaster.pid").is_file():
            _run_command(
                [
                    postgres_bin / _postgres_executable("pg_ctl"),
                    "-D",
                    data,
                    "-w",
                    "-t",
                    "30",
                    "stop",
                    "-m",
                    "fast",
                ],
                timeout=40,
                check=False,
            )
            _wait_for_port(port, expected_open=False)
        shutil.rmtree(root)


@contextmanager
def _temporary_environment(values: Mapping[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _pair_record(artifact: Mapping[str, Any], output: Path) -> dict[str, Any]:
    benchmark = artifact["benchmark_artifact"]
    manifest = benchmark["manifest"]
    outcomes = benchmark["outcomes"]
    hard_gates = tuple(
        {
            "arm": outcome["arm"],
            "code": violation["code"],
            "event_count": violation["event_count"],
        }
        for outcome in outcomes
        for violation in outcome.get("hard_gate_violations", ())
    )
    return {
        "pair_id": manifest["pair_id"],
        "artifact": str(output),
        "environment_fault": manifest["environment_fault"],
        "invalid_fairness": manifest["invalid_fairness"],
        "hard_gate_violations": hard_gates,
        "arms": {
            outcome["arm"]: {
                "final_run_state": outcome["final_run_state"],
                "business_state_correct": outcome["business_state_correct"],
            }
            for outcome in outcomes
        },
    }


def _stop_reason(record: Mapping[str, Any]) -> str | None:
    if record["hard_gate_violations"]:
        return "hard_gate_violation"
    if record["environment_fault"]:
        return "environment_fault"
    if record["invalid_fairness"]:
        return "invalid_fairness"
    return None


def _assert_redacted(payload: str) -> None:
    sensitive_values = (
        os.environ.get("STRIPE_SECRET_KEY"),
        os.environ.get("AGENT_RUN_HMAC_SECRET"),
        os.environ.get("AGENTPACT_DATABASE_URL"),
    )
    for value in sensitive_values:
        if value and value in payload:
            raise RuntimeError("Gate 3 refused to write an artifact containing a runtime secret")


def _write_batch_index(output: Path, index: Mapping[str, Any]) -> None:
    payload = json.dumps(index, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    _assert_redacted(payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    pending = output.with_suffix(output.suffix + ".tmp")
    pending.write_text(payload, encoding="utf-8")
    pending.replace(output)


async def _run_batch(
    *,
    batch_id: str,
    output_dir: Path,
    repetitions: int,
    pair_runner: PairRunner = _run_pair,
) -> dict[str, Any]:
    index_path = output_dir / f"{batch_id}-index.json"
    records: list[dict[str, Any]] = []
    index: dict[str, Any] = {
        "schema_version": BATCH_SCHEMA_VERSION,
        "batch_id": batch_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "requested_pair_count": repetitions,
        "completed_pair_count": 0,
        "valid_pair_count": 0,
        "discarded_pair_count": 0,
        "hard_gate_event_count": 0,
        "safety_ledger": [],
        "stopped_early": False,
        "stop_reason": None,
        "pairs": records,
        "redaction": {"secret_written": False, "database_url_written": False},
    }
    _write_batch_index(index_path, index)
    for ordinal in range(1, repetitions + 1):
        pair_id = f"{batch_id}-p{ordinal:02d}-{uuid.uuid4().hex[:8]}"
        pair_output = output_dir / f"{pair_id}.json"
        artifact = await pair_runner(pair_id, pair_output)
        record = _pair_record(artifact, pair_output)
        records.append(record)
        index["completed_pair_count"] = len(records)
        index["valid_pair_count"] = sum(
            not item["environment_fault"] and not item["invalid_fairness"] for item in records
        )
        index["discarded_pair_count"] = sum(
            item["environment_fault"] or item["invalid_fairness"] for item in records
        )
        safety_ledger = [
            {
                "pair_id": item["pair_id"],
                "artifact": item["artifact"],
                **violation,
            }
            for item in records
            for violation in item["hard_gate_violations"]
        ]
        index["safety_ledger"] = safety_ledger
        index["hard_gate_event_count"] = sum(item["event_count"] for item in safety_ledger)
        reason = _stop_reason(record)
        if reason:
            index["stopped_early"] = ordinal < repetitions
            index["stop_reason"] = reason
        _write_batch_index(index_path, index)
        if reason:
            break
    return index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--batch-id", default=None)
    parser.add_argument("--repetitions", type=int, default=5, choices=range(1, 6))
    parser.add_argument("--env-file", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.env_file:
        load_dotenv(args.env_file, override=False)
    if not os.environ.get("STRIPE_SECRET_KEY", "").startswith("sk_test_"):
        raise SystemExit("STRIPE_SECRET_KEY must be an injected sk_test_* key")
    batch_id = args.batch_id or f"stripe-gate3-{uuid.uuid4().hex[:12]}"
    with _isolated_postgres() as database_url:
        with _temporary_environment(
            {
                "AGENTPACT_DATABASE_URL": database_url,
                "AGENT_RUN_HMAC_SECRET": secrets.token_urlsafe(48),
            }
        ):
            index = asyncio.run(
                _run_batch(
                    batch_id=batch_id,
                    output_dir=args.output_dir,
                    repetitions=args.repetitions,
                )
            )
    print(
        json.dumps(
            {
                "batch_id": batch_id,
                "completed_pair_count": index["completed_pair_count"],
                "valid_pair_count": index["valid_pair_count"],
                "stopped_early": index["stopped_early"],
                "stop_reason": index["stop_reason"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
