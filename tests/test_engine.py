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
    status = adapter._finalize("run-1", "running", 2 ** 22)
    assert status.status == INTERRUPTED
    assert status.reconciled is True


def test_running_with_live_pid_stays_running(tmp_path: Path):
    adapter = _adapter(tmp_path)
    status = adapter._finalize("run-1", "running", os.getpid())
    assert status.status == RUNNING
    assert status.reconciled is False


def test_running_without_owner_is_not_a_false_success(tmp_path: Path):
    adapter = _adapter(tmp_path)
    status = adapter._finalize("run-1", "running", 0)
    assert status.status == INTERRUPTED


def test_terminal_statuses_are_not_reconciled(tmp_path: Path):
    adapter = _adapter(tmp_path)
    assert adapter._finalize("r", "succeeded", 0).status == SUCCEEDED
    assert adapter._finalize("r", "failed", 0).status == FAILED


def test_unknown_status_with_nonzero_exit_is_failed(tmp_path: Path):
    adapter = _adapter(tmp_path)
    status = adapter._finalize("r", "", 0, exit_code=1)
    assert status.status == FAILED


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
    runner = DockerRunner("img:1", tmp_path, user="1000:1000")
    argv = runner.container_argv(["dagu", "version"])
    assert argv[:2] == ["docker", "run"]
    assert "--user" in argv and "1000:1000" in argv
    assert f"{tmp_path}:/bench" in argv
    assert argv[-2:] == ["img:1", "dagu"] or argv[-3] == "img:1"


def test_docker_runner_never_chowns(tmp_path: Path):
    """The adapter must not rewrite ownership of the bench."""
    runner = DockerRunner("img:1", tmp_path, user="1000:1000")
    argv = runner.container_argv(["dagu", "version"])
    assert "chown" not in argv


def test_make_adapter_unknown_backend(tmp_path: Path):
    from mtm.errors import MtmError

    with pytest.raises(MtmError):
        make_adapter("nope", image_ref="img:1", bench=tmp_path)
