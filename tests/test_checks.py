from __future__ import annotations

import json
import subprocess
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from code_mower import checks


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_detect_node_checks_uses_lockfile_package_manager(tmp_path: Path) -> None:
    _write(
        tmp_path / "package.json",
        json.dumps(
            {
                "scripts": {
                    "lint": "eslint .",
                    "typecheck": "tsc --noEmit",
                    "test": "vitest run",
                    "build": "vite build",
                }
            }
        ),
    )
    _write(tmp_path / "pnpm-lock.yaml", "lockfileVersion: '9.0'\n")
    _write(tmp_path / "tests" / "sample.test.mjs", "test('ok', () => {})\n")

    detected = {check.id: check for check in checks.detect_checks(tmp_path)}

    assert detected["node.lint"].command == ("pnpm", "run", "lint")
    assert detected["node.typecheck"].command == ("pnpm", "run", "typecheck")
    assert detected["node.test"].command == ("pnpm", "run", "test")
    assert detected["node.build"].command == ("pnpm", "run", "build")
    assert "python.pytest" not in detected


def test_detect_python_checks_prefers_repo_venv(tmp_path: Path) -> None:
    venv_python = tmp_path / ".venv" / "bin" / "python"
    _write(venv_python, "#!/usr/bin/env python\n")
    _write(tmp_path / "pyproject.toml", "[tool.ruff]\n")
    _write(tmp_path / "tests" / "test_sample.py", "def test_sample():\n    assert True\n")

    detected = {check.id: check for check in checks.detect_checks(tmp_path)}

    assert detected["python.ruff"].command == (str(venv_python), "-m", "ruff", "check", ".")
    assert detected["python.pytest"].command == (str(venv_python), "-m", "pytest", "tests", "-q")


def test_run_python_pytest_check_passes(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'sample'\nversion = '0.0.0'\n")
    _write(
        tmp_path / "tests" / "test_sample.py",
        "def test_sample():\n    assert 1 + 1 == 2\n",
    )

    selected = [
        check for check in checks.detect_checks(tmp_path) if check.id == "python.pytest"
    ]
    assert len(selected) == 1

    results, exit_code = checks.run_checks(
        selected,
        repo_path=tmp_path,
        timeout_seconds=60,
        include_output=False,
    )

    assert exit_code == 0
    assert results[0]["status"] == "passed"
    assert results[0]["command"][0] == sys.executable
    assert results[0]["stdout_preview"] is None


def test_cli_detect_json(tmp_path: Path) -> None:
    _write(tmp_path / "package.json", json.dumps({"scripts": {"lint": "eslint ."}}))

    out = StringIO()
    with redirect_stdout(out):
        exit_code = checks.main(["detect", "--repo-path", str(tmp_path), "--json"])

    payload = json.loads(out.getvalue())
    assert exit_code == 0
    assert payload["mode"] == "code-mower-checks-detect"
    assert payload["check_count"] == 1
    assert payload["checks"][0]["id"] == "node.lint"


def test_run_timeout_decodes_byte_output_for_json() -> None:
    check = checks.CheckSpec(
        id="sample.timeout",
        category="test",
        language="generic",
        tool="sample",
        command=("sample",),
        source="unit-test",
        reason="exercise timeout serialization",
    )

    with mock.patch(
        "code_mower.checks.subprocess.run",
        side_effect=subprocess.TimeoutExpired(
            cmd=("sample",),
            timeout=1,
            output=b"partial stdout",
            stderr=b"partial stderr",
        ),
    ):
        results, exit_code = checks.run_checks(
            [check],
            repo_path=Path.cwd(),
            timeout_seconds=1,
            include_output=True,
        )

    assert exit_code == 1
    assert results[0]["status"] == "timeout"
    assert results[0]["stdout_preview"] == "partial stdout"
    assert results[0]["stderr_preview"] == "partial stderr"
    json.dumps({"checks": results})
