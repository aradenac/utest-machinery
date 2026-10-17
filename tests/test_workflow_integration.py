"""Integration tests exercising a real engine backend.

Two backends are covered:

* ``docker`` — skipped unless Docker and the built image are present; this is the
  default user path and needs no host Dagu.
* ``host`` — skipped unless a host ``dagu`` exists; explicit opt-in only.

The non-docker unit tests of the adapter (status mapping, reconciliation) live in
``test_engine.py`` and always run.
"""

from __future__ import annotations

import shutil
import subprocess
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

    def test_failed_step_is_a_finding_with_full_contract(self, bench: Path):
        _write_dag(bench, "steps:\n  - name: boom\n    command: 'exit 3'\n")
        record, code = run_mtm_json("workflow", "run", "--run-id", "d-fail", cwd=bench)
        assert code == 10, record  # finding, not tool failure
        assert record["status"] == "FAILED"
        assert record["code"] == "TEST_ASSERTION"
        assert record["category"] == "TEST_FINDING"
        assert record["cause"]  # the failure reason is present
        assert record["action"]

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


@pytest.mark.skipif(HOST_DAGU_DIR is None, reason="no host dagu available")
class TestHostBackend:
    CLI_ENV = {"PATH": f"{HOST_DAGU_DIR}:/usr/bin:/bin"} if HOST_DAGU_DIR else {}

    def test_host_backend_reports_failed_run(self, bench: Path):
        _write_dag(bench, "steps:\n  - name: boom\n    command: 'exit 3'\n")
        record, code = run_mtm_json(
            "--backend", "host", "workflow", "run", "--run-id", "h-fail",
            cwd=bench, env=self.CLI_ENV,
        )
        assert code == 10, record
        assert record["backend"] == "host"
        assert record["code"] == "TEST_ASSERTION"


def test_docker_backend_does_not_require_host_dagu(bench: Path):
    """The default backend must work without any host dagu on PATH."""
    record, code = run_mtm_json("workflow", "run", "--run-id", "x", cwd=bench,
                                env={"PATH": "/usr/bin:/bin"})
    # Either it runs via docker (0/10) or it reports the image/docker is missing
    # (30/40). It must never fail with "dagu executable not found".
    assert record.get("code") != "TOOL_FAILED" or "docker" in (record.get("message", "").lower() + record.get("action", "").lower())
