"""Keep Synthetic implementations behind test and fixture boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
RETIRED_ENTRYPOINTS = (
    "scripts/agentpact_eval.py",
    "scripts/finrpa_release.py",
    "scripts/finrpa_release_support.py",
    ".github/workflows/finrpa-release.yml",
    "requirements-m5-demo.lock",
)
HISTORICAL_RELEASE_DOCS = (
    "docs/phase-2/m5-developer-release-guide.md",
    "docs/phase-2/final-product-charter.md",
    "docs/phase-2/phase-2-master-status.md",
)


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_synthetic_has_no_formal_demo_evaluation_or_release_entrypoint() -> None:
    assert all(not (ROOT / path).exists() for path in RETIRED_ENTRYPOINTS)


def test_shipped_python_does_not_import_the_synthetic_fixture_domain() -> None:
    shipped_sources = [
        *sorted((ROOT / "scripts").glob("*.py")),
        *sorted((ROOT / "skyvern").rglob("*.py")),
        *sorted((ROOT / "enterprise").rglob("*.py")),
    ]
    synthetic_root = ROOT / "enterprise" / "domains" / "synthetic_payment"
    offenders: list[str] = []
    for path in shipped_sources:
        if path.is_relative_to(synthetic_root):
            continue
        if any(module.startswith("enterprise.domains.synthetic_payment") for module in _imported_modules(path)):
            offenders.append(path.relative_to(ROOT).as_posix())

    assert offenders == []


def test_synthetic_evaluation_wrapper_is_test_only_and_not_executable() -> None:
    support = ROOT / "tests" / "support" / "synthetic_agent_eval.py"
    tree = ast.parse(support.read_text(encoding="utf-8"), filename=str(support))
    assert not any(isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'" for node in tree.body)


def test_retired_release_contract_remains_in_historical_docs_only() -> None:
    historical = [(ROOT / path).read_text(encoding="utf-8") for path in HISTORICAL_RELEASE_DOCS]
    assert all("AgentPact" in document for document in historical)
    assert all("M5" in document for document in historical)
    assert all("scripts/finrpa_release.py" in document for document in (historical[0], historical[2]))
    assert "finrpa.release-report/v1" in "".join(historical)

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "| M5 开发者体验 | Historical |" in readme
    assert "scripts/finrpa_release.py" not in readme
    assert "scripts/agentpact_eval.py" not in readme
