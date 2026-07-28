#!/usr/bin/env python3
"""Canonical FinRPA developer/release interface."""

from __future__ import annotations

import argparse
import sys

from finrpa_release_support import (
    CONFORMANCE_TESTS,
    DEMO_TEST,
    ReleaseContractError,
    build_report,
    canonical_json_bytes,
    cleanup_result,
    doctor_result,
    load_report,
    render_markdown,
    run_pytest,
    write_report,
)

EXIT_OK = 0
EXIT_PREREQUISITE = 2
EXIT_CHECK_FAILED = 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="finrpa_release",
        description="Run the synthetic-only FinRPA M5 diagnosis, conformance, demo, and evidence commands.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("doctor", help="check prerequisites and loopback boundaries without starting services")
    subcommands.add_parser("conformance", help="run deterministic M1-M3 offline static conformance")
    subcommands.add_parser("demo", help="run the accepted M4 governed Chromium/PostgreSQL proof")
    subcommands.add_parser("report", help="validate and render the latest release evidence")
    return parser


def _emit_json(value: object) -> None:
    sys.stdout.buffer.write(canonical_json_bytes(value) + b"\n")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "doctor":
            result = doctor_result(require_demo=True)
            _emit_json(result)
            return EXIT_OK if result["status"] == "pass" else EXIT_PREREQUISITE

        if args.command == "conformance":
            check = run_pytest("m1-m3-static-conformance", CONFORMANCE_TESTS)
            report = build_report("conformance", [check], cleanup_result("conformance"))
            write_report(report)
            _emit_json(report)
            return EXIT_OK if report["status"] == "pass" else EXIT_CHECK_FAILED

        if args.command == "demo":
            diagnosis = doctor_result(require_demo=True)
            if diagnosis["status"] != "pass":
                _emit_json(diagnosis)
                return EXIT_PREREQUISITE
            check = run_pytest("m4-governed-browser-proof", [DEMO_TEST])
            report = build_report("demo", [check], cleanup_result("demo"))
            write_report(report)
            _emit_json(report)
            return EXIT_OK if report["status"] == "pass" else EXIT_CHECK_FAILED

        report = load_report()
        write_report(report)
        sys.stdout.write(render_markdown(report))
        return EXIT_OK
    except ReleaseContractError as exc:
        sys.stderr.write(f"finrpa_release: {exc}\n")
        return EXIT_PREREQUISITE


if __name__ == "__main__":
    raise SystemExit(main())
