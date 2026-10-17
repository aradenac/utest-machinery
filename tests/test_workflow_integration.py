"""Integration tests exercising a real engine backend.

Two backends are covered:

* ``docker`` — skipped unless Docker and the built image are present; this is the
  default user path and needs no host Dagu.
* ``host`` — skipped unless a host ``dagu`` exists; explicit opt-in only.

The non-docker unit tests of the adapter (status mapping, reconciliation) live in
``test_engine.py`` and always run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REPO, dagu_dir, run_mtm_json

IMAGE = "utest-machinery/mtm:1.0.0"


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    info = subprocess.run(["docker", "info"], capture_output=True, text=True)
    if info.returncode != 0:
        return False
    return subprocess.run(["docker", "image", "inspect", IMAGE],
                          capture_output=True, text=True).returncode == 0


DOCKER_READY = _docker_ready()
HOST_DAGU_DIR = dagu_dir()


def _write_dag(bench: Path, body: str) -> None:
    (bench / "workflows").mkdir(exist_ok=True)
    (bench / "workflows" / "sample.yaml").write_text(body)


@pytest.mark.skipif(not DOCKER_READY, reason="docker or the built image is unavailable")
class TestDockerBackend:
    def test_run_success_via_cli(self, bench: Path):
        _write_dag(bench, "steps:\n  - name: ok\n    command: echo ok\n")
        record, code = run_mtm_json("workflow", "run", "--run-id", "d-ok", cwd=bench)
        assert code == 0, record
        assert record["status"] == "SUCCEEDED"
        assert record["backend"] == "docker"
        # bench-local state, written by the container, on the host
        assert (bench / ".mtm" / "dagu" / "data" / "dag-runs").is_dir()

    def test_failed_step_is_a_tool_failure_by_default(self, bench: Path):
        """An untyped failing command is a tool/workflow failure (exit 30)."""
        _write_dag(bench, "steps:\n  - name: boom\n    command: 'exit 3'\n")
        record, code = run_mtm_json("workflow", "run", "--run-id", "d-fail", cwd=bench)
        assert code == 30, record  # tool failure, NOT a test finding
        assert record["status"] == "FAILED"
        assert record["code"] == "TOOL_FAILED"
        assert record["category"] == "TOOL"
        assert record["cause"]  # the failing step is named
        assert record["action"]

    def test_typed_test_finding_is_exit_10(self, bench: Path):
        """A step reporting MTM_CATEGORY=TEST_FINDING is a finding -> exit 10."""
        _write_dag(
            bench,
            "steps:\n  - name: check\n    command: |\n"
            "      echo MTM_CATEGORY=TEST_FINDING\n      exit 1\n"
            "    output: MTM_CATEGORY\n",
        )
        record, code = run_mtm_json("workflow", "run", "--run-id", "d-finding", cwd=bench)
        assert code == 10, record
        assert record["status"] == "FAILED"
        assert record["code"] == "TEST_ASSERTION"
        assert record["category"] == "TEST_FINDING"

    def test_state_files_are_owned_by_the_invoking_user(self, bench: Path):
        _write_dag(bench, "steps:\n  - name: ok\n    command: echo ok\n")
        run_mtm_json("workflow", "run", "--run-id", "d-own", cwd=bench)
        import os

        uid = os.getuid()
        for path in (bench / ".mtm" / "dagu").rglob("*"):
            assert path.stat().st_uid == uid, f"{path} not owned by the invoking user"

    def test_workflow_status_reports_terminal_state(self, bench: Path):
        _write_dag(bench, "steps:\n  - name: ok\n    command: echo ok\n")
        run_mtm_json("workflow", "run", "--run-id", "d-st", cwd=bench)
        record, code = run_mtm_json("workflow", "status", "--run-id", "d-st", cwd=bench)
        assert code == 0
        assert record["status"] == "SUCCEEDED"

    def _wait_for_owner(self, run_id: str, timeout: float = 20.0) -> bool:
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            found = subprocess.run(
                ["docker", "ps", "-q", "--filter", f"label=mtm.run-id={run_id}"],
                capture_output=True, text=True,
            ).stdout.strip()
            if found:
                return True
            time.sleep(0.5)
        return False

    def test_running_docker_run_is_not_reported_interrupted(self, bench: Path):
        """A genuinely running container must read as RUNNING, not INTERRUPTED.

        Regression for the docker liveness path: the owner container is found by
        its mtm.run-id label while it runs, and only a killed owner (no running
        container) reconciles to INTERRUPTED.
        """
        import subprocess

        _write_dag(bench, "steps:\n  - name: slow\n    command: 'sleep 8'\n")
        proc = subprocess.Popen(
            [sys.executable, "-m", "mtm.cli", "--json", "workflow", "run", "--run-id", "d-live"],
            cwd=str(bench),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "PYTHONPATH": str(REPO)},
        )
        try:
            assert self._wait_for_owner("d-live"), "owner container never appeared"
            record, code = run_mtm_json("workflow", "status", "--run-id", "d-live", cwd=bench)
            assert record["status"] == "RUNNING", record
            assert record["owner_alive"] is True, record
            assert record["liveness_evidence"]
        finally:
            proc.wait(timeout=30)

    def test_killed_docker_run_reconciles_to_interrupted(self, bench: Path):
        """Killing the owner container reconciles a stale running run."""
        import subprocess

        _write_dag(bench, "steps:\n  - name: slow\n    command: 'sleep 30'\n")
        proc = subprocess.Popen(
            [sys.executable, "-m", "mtm.cli", "--json", "workflow", "run", "--run-id", "d-kill"],
            cwd=str(bench),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "PYTHONPATH": str(REPO)},
        )
        try:
            assert self._wait_for_owner("d-kill"), "owner container never appeared"
            ids = subprocess.run(
                ["docker", "ps", "-q", "--filter", "label=mtm.run-id=d-kill"],
                capture_output=True, text=True,
            ).stdout.split()
            assert ids, "no owner container to kill"
            subprocess.run(["docker", "kill", *ids], check=False, capture_output=True)
        finally:
            proc.wait(timeout=30)
        record, code = run_mtm_json("workflow", "status", "--run-id", "d-kill", cwd=bench)
        assert record["status"] == "INTERRUPTED", record
        assert record["owner_alive"] is False, record
        assert code == 130

    def test_run_cannot_write_a_selected_source(self, bench: Path):
        """The bench is mounted read-only: a step cannot tamper with inputs."""
        src = bench / "selected.c"
        src.write_text("int f(void){return 0;}\n")
        before = src.read_text()
        _write_dag(bench, "steps:\n  - name: tamper\n    command: 'echo hacked > /bench/selected.c'\n")
        run_mtm_json("workflow", "run", "--run-id", "d-ro", cwd=bench)
        assert src.read_text() == before, "a step modified a selected input"


@pytest.mark.skipif(HOST_DAGU_DIR is None, reason="no host dagu available")
class TestHostBackend:
    CLI_ENV = {"PATH": f"{HOST_DAGU_DIR}:/usr/bin:/bin"} if HOST_DAGU_DIR else {}

    def test_host_backend_reports_failed_run(self, bench: Path):
        _write_dag(bench, "steps:\n  - name: boom\n    command: 'exit 3'\n")
        record, code = run_mtm_json(
            "--backend", "host", "workflow", "run", "--run-id", "h-fail",
            cwd=bench, env=self.CLI_ENV,
        )
        assert code == 30, record  # untyped failure -> tool failure
        assert record["backend"] == "host"
        assert record["code"] == "TOOL_FAILED"


def test_docker_backend_does_not_require_host_dagu(bench: Path):
    """The default backend must work without any host dagu on PATH."""
    record, code = run_mtm_json("workflow", "run", "--run-id", "x", cwd=bench,
                                env={"PATH": "/usr/bin:/bin"})
    # Either it runs via docker (0/10) or it reports the image/docker is missing
    # (30/40). It must never fail with "dagu executable not found".
    assert record.get("code") != "TOOL_FAILED" or "docker" in (record.get("message", "").lower() + record.get("action", "").lower())
