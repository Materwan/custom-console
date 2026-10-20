"""The documents keep their promises: every review finding has a proof that exists, the README's numbers are the script's,
and the repository has what the review found missing (a licence, a CI)."""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "Custom console rebuild plan.md"
MAP = ROOT / "docs" / "REVIEW-MAP.md"


def table_rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        if line.startswith("| ") and not line.startswith("| ---") and not line.startswith("| Finding"):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def plan_rows() -> list[list[str]]:
    text = PLAN.read_text(encoding="utf-8")
    return table_rows(text[text.index("## How the review findings are fixed"):text.index("## Repository layout and module map")])


@pytest.mark.skipif(not PLAN.exists(), reason="the plan is not in this checkout")
def test_every_row_of_the_plans_review_tables_is_in_the_map():
    mapped = {(row[0], row[1]) for row in table_rows(MAP.read_text(encoding="utf-8"))}
    planned = {(row[0], row[1]) for row in plan_rows()}
    assert len(planned) == 39
    assert planned == mapped, f"missing: {sorted(planned - mapped)}; not in the plan: {sorted(mapped - planned)}"


def test_every_test_the_map_names_exists():
    text = MAP.read_text(encoding="utf-8")
    named = re.findall(r"`(tests/[\w/]+\.py)::(\w+)`", text)
    assert len(named) > 80
    missing = []
    for path, name in named:
        file = ROOT / path
        if not file.exists():
            missing.append(f"{path} (no such file)")
            continue
        functions = {node.name for node in ast.walk(ast.parse(file.read_text(encoding="utf-8")))
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if name not in functions:
            missing.append(f"{path}::{name}")
    assert not missing, missing


def test_a_row_is_fixed_with_tests_or_removed_with_a_reason():
    for row in table_rows(MAP.read_text(encoding="utf-8")):
        finding, _, result, proof = row[0], row[1], row[2], row[3]
        assert result in ("fixed", "removed"), finding
        if result == "fixed":
            assert "`" in proof or "tests/" in proof, f"{finding} has no proof"
        else:
            assert len(proof) > 10, f"{finding} does not say why it is gone"


def test_the_readme_says_what_goes_to_clara_and_what_stays():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "never leaves this machine" not in readme
    section = readme[readme.index("## What goes to Clara, and what stays here"):]
    section = section[:section.index("\n## ", 5)]
    for must in ("Sent to Clara", "Stays on this computer", "Incognito", "Local only", "session file holds no message text"):
        assert must in section, must


def test_the_readme_numbers_are_the_scripts_numbers():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import check_prompt
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    n = check_prompt.measure()
    expected = (f"{n['tools']} local tools · tool schemas {n['schemas']:,} characters · base prompt {n['base']:,} characters "
                f"({n['base_and_facts']:,} with the session facts) · longest possible prompt {n['worst_case']:,} of 20,000 characters")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    shown = readme.split("<!-- numbers -->")[1].split("<!-- /numbers -->")[0].strip()
    assert shown == expected


def test_ci_runs_every_check_on_the_three_systems():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for system in ("ubuntu-latest", "windows-latest", "macos-latest"):
        assert system in workflow
    assert '"3.11"' in workflow and '"3.13"' in workflow
    for step in ("ruff check", "mypy", "lint-imports", "pytest", "scripts/check_prompt.py"):
        assert step in workflow, step
    assert "--frozen" in workflow  # the dependencies are the locked ones


def test_the_layers_have_import_contracts():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["importlinter"]
    contracts = config["contracts"]
    assert len(contracts) >= 5
    layers = next(c for c in contracts if c["type"] == "layers")["layers"]
    assert layers[:3] == ["custom_console.ui", "custom_console.commands", "custom_console.core"]
    forbidden = [c for c in contracts if c["type"] == "forbidden"]
    assert any("textual" in c["forbidden_modules"] and "custom_console.core" in c["source_modules"] for c in forbidden)
    assert {tuple(c["source_modules"]) for c in forbidden} >= {("custom_console.clara",), ("custom_console.platform",)}


def test_license_and_ci_exist():
    assert "All rights reserved" in (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert (ROOT / ".github" / "workflows" / "ci.yml").is_file()
