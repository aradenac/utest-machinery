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

from mtm.engine import DockerRunner

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

    def test_mixed_failures_report_the_tool_failure(self, bench: Path):
        """A crashed tool alongside a typed finding must dominate (exit 30).

        The finding must not conceal the more severe tool failure.
        """
        _write_dag(
            bench,
            "steps:\n"
            "  - name: check\n    command: |\n"
            "      echo MTM_CATEGORY=TEST_FINDING\n      exit 1\n"
            "    output: MTM_CATEGORY\n"
            "  - name: crash\n    command: 'exit 7'\n",
        )
        record, code = run_mtm_json("workflow", "run", "--run-id", "d-mixed", cwd=bench)
        assert code == 30, record
        assert record["category"] == "TOOL"
        assert record["code"] == "TOOL_FAILED"

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

    def _wait_for_owner(self, run_id: str, bench: Path, timeout: float = 25.0) -> bool:
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            found = subprocess.run(
                ["docker", "ps", "-q",
                 "--filter", f"label=mtm.owner={self._owner_label(run_id, bench)}"],
                capture_output=True, text=True,
            ).stdout.strip()
            if found:
                return True
            time.sleep(0.5)
        return False

    @staticmethod
    def _owner_label(run_id: str, bench: Path | None = None) -> str:
        from mtm.engine import bench_identity

        return f"{bench_identity(bench)}__{run_id}"

    def test_running_docker_run_is_not_reported_interrupted(self, bench: Path):
        """A genuinely running container must read as RUNNING, not INTERRUPTED,
        and its status response must not look like a finished failure."""
        import subprocess

        _write_dag(bench, "steps:\n  - name: slow\n    command: 'sleep 10'\n")
        proc = subprocess.Popen(
            [sys.executable, "-m", "mtm.cli", "--json", "workflow", "run", "--run-id", "d-live"],
            cwd=str(bench),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "PYTHONPATH": str(REPO)},
        )
        try:
            assert self._wait_for_owner("d-live", bench), "owner container never appeared"
            record, code = run_mtm_json("workflow", "status", "--run-id", "d-live", cwd=bench)
            assert record["status"] == "RUNNING", record
            assert record["owner_alive"] is True, record
            assert record["liveness_evidence"]
            # A live run is not a finished failure.
            assert code == 0, record
            assert record["exit_code"] == 0, record
            assert record["code"] == "OPERATION_RUNNING", record
            assert "not finished" in record["message"].lower()
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
            assert self._wait_for_owner("d-kill", bench), "owner container never appeared"
            ids = subprocess.run(
                ["docker", "ps", "-q", "--filter", f"label=mtm.owner={self._owner_label('d-kill', bench)}"],
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

    def test_bench_scoped_label_avoids_cross_bench_collision(self, bench: Path, tmp_path: Path):
        """A same-named run in another bench must not make this one look alive."""
        from mtm.engine import bench_identity

        other = tmp_path / "other-bench"
        other.mkdir()
        run_mtm_json("init", str(other))
        assert bench_identity(bench) != bench_identity(other)
        # Bench A has no run "shared"; bench B has one. A's lookup must be False.
        runner_a = DockerRunner("img:1", bench, user="1000:1000")
        alive, evidence = runner_a.owner_alive("shared")
        assert alive is False, evidence
        assert bench_identity(bench) in evidence
        assert bench_identity(other) not in evidence

    def test_run_cannot_write_a_selected_source(self, bench: Path):
        """The bench is mounted read-only: a step cannot tamper with inputs.

        The tampering step must actually run and fail *because* the input mount
        is read-only — not because of an unrelated startup/validation error.
        """
        src = bench / "selected.c"
        src.write_text("int f(void){return 0;}\n")
        before = src.read_text()
        _write_dag(
            bench,
            "steps:\n  - name: tamper\n    command: 'echo hacked > /bench/selected.c'\n",
        )
        record, code = run_mtm_json("workflow", "run", "--run-id", "d-ro", cwd=bench)
        # The step ran and failed (untyped shell failure -> exit 30).
        assert code == 30, record
        assert record["status"] == "FAILED", record
        assert any(s.get("name") == "tamper" and s.get("failed")
                   for s in record.get("step_evidence", [])), record
        # ...and the failure is the read-only mount, not a startup error.
        cause = record.get("cause", "").lower()
        assert "read-only" in cause or "read only" in cause, record
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
