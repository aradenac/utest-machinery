"""Shared pytest fixtures: run the CLI in a temporary bench, fully offline."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def run_mtm(*args: str, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    """Invoke the mtm CLI as a subprocess and capture its result."""
    base_env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(REPO),
        "HOME": str(Path.home()),
    }
    if env:
        base_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "mtm.cli", *args],
        cwd=str(cwd or REPO),
        capture_output=True,
        text=True,
        env=base_env,
    )


def run_mtm_json(*args: str, cwd: Path | None = None, env: dict | None = None) -> tuple[dict, int]:
    proc = run_mtm("--json", *args, cwd=cwd, env=env)
    try:
        return json.loads(proc.stdout), proc.returncode
    except json.JSONDecodeError:
        raise AssertionError(f"non-JSON output: {proc.stdout!r} stderr={proc.stderr!r}")


@pytest.fixture
def bench(tmp_path: Path) -> Path:
    """An initialised bench in a temporary directory."""
    target = tmp_path / "bench"
    target.mkdir()
    record, code = run_mtm_json("init", str(target))
    assert code == 0, record
    return target
