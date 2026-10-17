"""Workflow-adapter tests (offline): status mapping and dead-owner reconciliation.

The interruption test uses a fake Dagu that reports 'running' with a PID that is
guaranteed dead, so the reconciliation logic is exercised deterministically
without needing to kill a real process.
"""

from __future__ import annotations

import os
from pathlib import Path

from mtm.engine import (
    FAILED,
    INTERRUPTED,
    PENDING,
    RUNNING,
    SUCCEEDED,
    DaguAdapter,
    _STATUS_MAP,
)


def test_status_map_covers_the_documented_set():
    assert _STATUS_MAP["succeeded"] == SUCCEEDED
    assert _STATUS_MAP["failed"] == FAILED
    assert _STATUS_MAP["running"] == RUNNING
    assert _STATUS_MAP["pending"] == PENDING


def test_running_with_dead_pid_reconciles_to_interrupted(tmp_path: Path):
    adapter = DaguAdapter(dagu_bin="/bin/true", dagu_home=tmp_path)
    dead_pid = 2 ** 22  # far above any plausible live PID on Linux
    status = adapter._finalize("run-1", "running", dead_pid)
    assert status.status == INTERRUPTED
    assert status.reconciled is True


def test_running_with_live_pid_stays_running(tmp_path: Path):
    adapter = DaguAdapter(dagu_bin="/bin/true", dagu_home=tmp_path)
    status = adapter._finalize("run-1", "running", os.getpid())
    assert status.status == RUNNING
    assert status.reconciled is False


def test_running_without_owner_is_not_a_false_success(tmp_path: Path):
    adapter = DaguAdapter(dagu_bin="/bin/true", dagu_home=tmp_path)
    status = adapter._finalize("run-1", "running", 0)
    assert status.status == INTERRUPTED


def test_terminal_statuses_are_not_reconciled(tmp_path: Path):
    adapter = DaguAdapter(dagu_bin="/bin/true", dagu_home=tmp_path)
    assert adapter._finalize("r", "succeeded", 0).status == SUCCEEDED
    assert adapter._finalize("r", "failed", 0).status == FAILED


def test_parse_status_handles_start_and_status_verbs():
    assert DaguAdapter._parse_status_text("Result: Succeeded\n") == "Succeeded"
    assert DaguAdapter._parse_status_text("Status: Running\n") == "Running"
    assert DaguAdapter._parse_status_text("no status here") == ""


def test_pid_from_events_reads_last_pid(tmp_path: Path):
    log = tmp_path / "status.jsonl"
    log.write_text('{"pid": 111}\n{"pid": 222}\n')
    assert DaguAdapter._pid_from_events(log) == 222
