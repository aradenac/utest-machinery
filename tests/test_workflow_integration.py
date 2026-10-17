"""Integration tests that exercise a real Dagu engine when one is available.

Skipped automatically when ``dagu`` is not on PATH, so the fast CI stays
offline and deterministic while a developer machine can run the full path.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from conftest import run_mtm_json

DAGU = shutil.which("dagu")

pytestmark = pytest.mark.skipif(DAGU is None, reason="dagu not on PATH")

#: PATH handed to the CLI: the minimal base plus dagu's directory. The CLI sets
#: DAGU_HOME itself from --dagu-home, so no other environment is needed.
CLI_ENV = {"PATH": f"{Path(DAGU).parent}:/usr/bin:/bin"} if DAGU else {}


def _write_dag(bench: Path, body: str) -> None:
    (bench / "workflows").mkdir(exist_ok=True)
    (bench / "workflows" / "sample.yaml").write_text(body)


def test_workflow_run_success(bench: Path, tmp_path: Path):
    _write_dag(bench, "steps:\n  - name: ok\n    command: echo ok\n")
    record, code = run_mtm_json(
        "--dagu-home", str(tmp_path / "dagu"),
        "workflow", "run", "--run-id", "t-ok", cwd=bench, env=CLI_ENV,
    )
    assert code == 0, record
    assert record["status"] == "SUCCEEDED"


def test_workflow_run_failure_is_tool_failure_not_success(bench: Path, tmp_path: Path):
    _write_dag(bench, "steps:\n  - name: boom\n    command: 'exit 3'\n")
    record, code = run_mtm_json(
        "--dagu-home", str(tmp_path / "dagu"),
        "workflow", "run", "--run-id", "t-fail", cwd=bench, env=CLI_ENV,
    )
    assert code == 30, record
    assert record["status"] == "FAILED"


def test_workflow_view_text_dag(bench: Path):
    _write_dag(
        bench,
        "steps:\n  - name: a\n    command: echo a\n  - name: b\n    command: echo b\n    depends: [a]\n",
    )
    record, code = run_mtm_json("workflow", "view", cwd=bench)
    assert code == 0
    assert [s["name"] for s in record["steps"]] == ["a", "b"]
    assert "(after: a)" in record["text_dag"]
