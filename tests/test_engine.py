"""Workflow-adapter tests (offline): status mapping, backends, reconciliation.

The reconciliation logic is exercised deterministically by constructing the
adapter with a fake result, without killing any real process.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mtm.engine import (
    FAILED,
    INTERRUPTED,
    PENDING,
    RUNNING,
    SUCCEEDED,
    BenchPaths,
    DaguBackend,
    DockerRunner,
    LocalRunner,
    _STATUS_MAP,
    _pid_from_events,
    make_adapter,
)


def _adapter(tmp_path: Path, backend=None) -> DaguBackend:
    return DaguBackend(backend or LocalRunner(), BenchPaths(tmp_path), dagu_bin="/bin/true")


def test_status_map_covers_the_documented_set():
    assert _STATUS_MAP["succeeded"] == SUCCEEDED
    assert _STATUS_MAP["failed"] == FAILED
    assert _STATUS_MAP["running"] == RUNNING
    assert _STATUS_MAP["pending"] == PENDING


def test_running_with_dead_pid_reconciles_to_interrupted(tmp_path: Path):
    adapter = _adapter(tmp_path)
    status = adapter._finalize("run-1", "running", 2 ** 22, owner_alive=False,
                               liveness_evidence="host PID dead")
    assert status.status == INTERRUPTED
    assert status.reconciled is True


def test_running_with_live_pid_stays_running(tmp_path: Path):
    adapter = _adapter(tmp_path)
    status = adapter._finalize("run-1", "running", os.getpid(), owner_alive=True,
                               liveness_evidence="host PID alive")
    assert status.status == RUNNING
    assert status.reconciled is False


def test_running_with_unknown_liveness_stays_running(tmp_path: Path):
    """A docker run with no host PID must NOT be reconciled to INTERRUPTED."""
    adapter = _adapter(tmp_path)
    status = adapter._finalize("run-1", "running", 0, owner_alive=None)
    assert status.status == RUNNING
    assert status.reconciled is False


def test_pid_alive_returns_none_for_non_host_pid():
    from mtm.engine import _pid_alive

    assert _pid_alive(0) is None
    assert _pid_alive(-1) is None
    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(2 ** 22) is False


def test_terminal_statuses_are_not_reconciled(tmp_path: Path):
    adapter = _adapter(tmp_path)
    assert adapter._finalize("r", "succeeded", 0).status == SUCCEEDED
    assert adapter._finalize("r", "failed", 0).status == FAILED


def test_unknown_status_with_nonzero_exit_is_failed(tmp_path: Path):
    adapter = _adapter(tmp_path)
    status = adapter._finalize("r", "", 0, exit_code=1)
    assert status.status == FAILED


def test_step_evidence_parses_category_marker(tmp_path: Path):
    from mtm.engine import _step_evidence_from_state

    run_dir = tmp_path / "data" / "dag-runs" / "dag-run_20260101_000000Z_r1" / "a_1_1"
    run_dir.mkdir(parents=True)
    (run_dir / "status.jsonl").write_text(
        '{"nodes": ['
        '{"step": {"name": "check"}, "status": 2, "error": "exit status 1", '
        '"outputVariables": {"MTM_CATEGORY": "MTM_CATEGORY=TEST_FINDING"}},'
        '{"step": {"name": "boom"}, "status": 2, "error": "exit status 3"}'
        ']}\n'
    )
    evidence = _step_evidence_from_state(tmp_path, "r1")
    assert [s["name"] for s in evidence] == ["check", "boom"]
    assert evidence[0]["category"] == "TEST_FINDING"
    assert evidence[0]["failed"] is True
    assert evidence[1]["category"] == ""  # untyped


def test_category_hint_is_strongest_reported(tmp_path: Path):
    from mtm.engine import RunStatus

    status = RunStatus(dag_run_id="r", status=FAILED, step_evidence=[
        {"name": "boom", "failed": True, "category": ""},
        {"name": "check", "failed": True, "category": "TEST_FINDING"},
    ])
    # A crashed tool (untyped => TOOL) coexisting with a test finding must win:
    # the tool failure is more severe and must not be hidden.
    assert status.category_hint == "TOOL"
    assert "check" in status.cause or "boom" in status.cause


def test_mixed_failures_promote_tool_over_finding(tmp_path: Path):
    from mtm.engine import RunStatus

    status = RunStatus(dag_run_id="r", status=FAILED, step_evidence=[
        {"name": "check", "failed": True, "category": "TEST_FINDING"},
        {"name": "crash", "failed": True, "category": ""},
    ])
    outcome = status.dominant_outcome
    assert outcome is not None
    assert outcome[2] == 30  # tool failure dominates a finding


def test_finding_alone_is_a_finding(tmp_path: Path):
    from mtm.engine import RunStatus

    status = RunStatus(dag_run_id="r", status=FAILED, step_evidence=[
        {"name": "check", "failed": True, "category": "TEST_FINDING"},
    ])
    assert status.category_hint == "TEST_FINDING"
    assert status.dominant_outcome[2] == 10


def test_aborted_dependent_steps_are_not_counted(tmp_path: Path):
    from mtm.engine import RunStatus

    # A step marked aborted (upstream failed) is not itself a failure.
    status = RunStatus(dag_run_id="r", status=FAILED, step_evidence=[
        {"name": "check", "failed": True, "category": "TEST_FINDING"},
        {"name": "after", "failed": False, "aborted": True, "category": ""},
    ])
    assert status.category_hint == "TEST_FINDING"


def test_unknown_category_marker_is_ignored(tmp_path: Path):
    from mtm.engine import _step_evidence_from_state

    run_dir = tmp_path / "data" / "dag-runs" / "dag-run_20260101_000000Z_r2" / "a_1_1"
    run_dir.mkdir(parents=True)
    (run_dir / "status.jsonl").write_text(
        '{"nodes": [{"step": {"name": "x"}, "status": 2, '
        '"outputVariables": {"MTM_CATEGORY": "MTM_CATEGORY=BOGUS"}}]}\n'
    )
    evidence = _step_evidence_from_state(tmp_path, "r2")
    assert evidence[0]["category"] == ""


def test_parse_status_handles_start_and_status_verbs(tmp_path: Path):
    adapter = _adapter(tmp_path)
    assert adapter._parse_status_text("Result: Succeeded\n") == "Succeeded"
    assert adapter._parse_status_text("Status: Running\n") == "Running"
    assert adapter._parse_status_text("no status here") == ""


def test_pid_from_events_reads_last_pid(tmp_path: Path):
    log = tmp_path / "status.jsonl"
    log.write_text('{"pid": 111}\n{"pid": 222}\n')
    assert _pid_from_events(log) == 222


def test_docker_backend_argv_uses_user_and_bench_mount(tmp_path: Path):
    (tmp_path / ".mtm").mkdir()
    runner = DockerRunner("img:1", tmp_path, user="1000:1000")
    argv = runner.container_argv(["dagu", "version"])
    assert argv[:2] == ["docker", "run"]
    assert "--user" in argv and "1000:1000" in argv
    assert f"{tmp_path}:/bench:ro" in argv  # inputs read-only
    assert f"{tmp_path / '.mtm'}:/bench/.mtm" in argv  # only state is writable
    assert argv[-2:] == ["img:1", "dagu"] or argv[-3] == "img:1"


def test_docker_runner_never_chowns(tmp_path: Path):
    """The adapter must not rewrite ownership of the bench."""
    runner = DockerRunner("img:1", tmp_path, user="1000:1000")
    argv = runner.container_argv(["dagu", "version"])
    assert "chown" not in argv


def test_docker_run_arg_carries_bench_scoped_owner_label(tmp_path: Path):
    (tmp_path / ".mtm").mkdir()
    runner = DockerRunner("img:1", tmp_path, user="1000:1000", run_label="run-x")
    argv = runner.container_argv(["dagu", "start"])
    # The label is bench-scoped: another bench reusing run id "run-x" must not
    # collide with this one.
    assert any(a.startswith("mtm.owner=") for a in argv), argv
    label = next(a for a in argv if a.startswith("mtm.owner="))
    assert label.endswith("__run-x")
    assert "run-x" in label


def test_make_adapter_unknown_backend(tmp_path: Path):
    from mtm.errors import MtmError

    with pytest.raises(MtmError):
        make_adapter("nope", image_ref="img:1", bench=tmp_path)
