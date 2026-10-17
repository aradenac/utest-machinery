"""Headless workflow engine adapter.

The product CLI never depends on the workflow engine directly: it talks to a
small adapter interface, so a fallback engine can replace the current one
without touching the CLI (``worklog/decisions/0003-third-party-licenses-and-dagu.md``).
Dagu is invoked as a **separate executable**, never linked into the product.

The measured Dagu gate (``worklog/decisions/0005-dagu-gate-proof.md``) found one
limitation that this adapter must handle, not hide:

    After a SIGKILL of the Dagu process, the run stays reported ``running``
    forever. Dagu does not reconcile a dead owner on the local backend.

Therefore :meth:`DaguAdapter.run_status` treats a ``running`` run whose recorded
PID is no longer alive as :data:`INTERRUPTED` — never ``RUNNING`` and never a
success. This satisfies the "never a false success" clause of
``docs/architecture/workflow-failures.md``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import Code, MtmError

# --- normalized engine statuses --------------------------------------------

PENDING = "PENDING"
RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
BLOCKED = "BLOCKED"
SKIPPED = "SKIPPED"
INTERRUPTED = "INTERRUPTED"

#: Map Dagu's own status strings to the normalized set.
_STATUS_MAP = {
    "pending": PENDING,
    "queued": PENDING,
    "not started": PENDING,
    "running": RUNNING,
    "succeeded": SUCCEEDED,
    "success": SUCCEEDED,
    "failed": FAILED,
    "error": FAILED,
    "aborted": INTERRUPTED,
    "cancelled": INTERRUPTED,
    "canceled": INTERRUPTED,
    "rejected": BLOCKED,
    "skipped": SKIPPED,
}


@dataclass
class RunStatus:
    dag_run_id: str
    status: str
    raw_status: str = ""
    pid: int = 0
    error: str = ""
    reconciled: bool = False

    @property
    def is_running(self) -> bool:
        return self.status == RUNNING

    @property
    def is_terminal(self) -> bool:
        return self.status in (SUCCEEDED, FAILED, INTERRUPTED, BLOCKED, SKIPPED)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by someone else
    return True


class EngineAdapter:
    """Interface every workflow engine adapter implements."""

    name = "abstract"

    def run_status(self, dag_path: Path, run_id: str) -> RunStatus:  # pragma: no cover
        raise NotImplementedError


class DaguAdapter(EngineAdapter):
    """Adapter over the Dagu CLI (separate executable)."""

    name = "dagu"

    def __init__(self, dagu_bin: str | None = None, dagu_home: Path | None = None):
        self.dagu_bin = dagu_bin or shutil.which("dagu") or "dagu"
        self.dagu_home = dagu_home

    # -- environment --------------------------------------------------------

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.dagu_home is not None:
            env["DAGU_HOME"] = str(self.dagu_home)
        return env

    def available(self) -> bool:
        return shutil.which(self.dagu_bin) is not None or Path(self.dagu_bin).exists()

    def version(self) -> str:
        proc = subprocess.run(
            [self.dagu_bin, "version"], capture_output=True, text=True,
            env=self._env(), check=False,
        )
        return proc.stdout.strip()

    # -- operations ---------------------------------------------------------

    def validate(self, dag_path: Path) -> None:
        proc = subprocess.run(
            [self.dagu_bin, "validate", str(dag_path)],
            capture_output=True, text=True, env=self._env(), check=False,
        )
        if proc.returncode != 0:
            raise MtmError(
                code=Code.CONFIG_INVALID,
                message=f"Workflow definition failed validation: {dag_path.name}.",
                action="Fix the DAG YAML; a file passed by path must not define 'name:'.",
                stage="workflow-validate",
                cause=proc.stderr.strip() or proc.stdout.strip(),
                input_reference=str(dag_path),
            )

    def dry(self, dag_path: Path) -> RunStatus:
        proc = subprocess.run(
            [self.dagu_bin, "dry", str(dag_path)],
            capture_output=True, text=True, env=self._env(), check=False,
        )
        status = SUCCEEDED if proc.returncode == 0 else FAILED
        return RunStatus(dag_run_id="", status=status, raw_status=status,
                         error=proc.stderr.strip())

    def run(self, dag_path: Path, run_id: str) -> RunStatus:
        proc = subprocess.run(
            [self.dagu_bin, "start", "--run-id", run_id, str(dag_path)],
            capture_output=True, text=True, env=self._env(), check=False,
        )
        return self._reconcile(dag_path, run_id, proc)

    # -- status with liveness reconciliation --------------------------------

    def run_status(self, dag_path: Path, run_id: str) -> RunStatus:
        proc = subprocess.run(
            [self.dagu_bin, "status", "--run-id", run_id, str(dag_path)],
            capture_output=True, text=True, env=self._env(), check=False,
        )
        raw = self._parse_status_text(proc.stdout + proc.stderr)
        return self._finalize(run_id, raw, self._find_pid(dag_path, run_id))

    def history_json(self) -> list[dict]:
        proc = subprocess.run(
            [self.dagu_bin, "history", "--format", "json"],
            capture_output=True, text=True, env=self._env(), check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return []

    # -- internals ----------------------------------------------------------

    def _reconcile(self, dag_path: Path, run_id: str, proc: subprocess.CompletedProcess) -> RunStatus:
        raw = self._parse_status_text(proc.stdout + proc.stderr)
        status = self._finalize(run_id, raw, self._find_pid(dag_path, run_id))
        if status.status in (PENDING, RUNNING) and proc.returncode != 0:
            # Dagu returned non-zero but we could not read a terminal status:
            # treat as failed rather than inventing success.
            status.status = FAILED
        if status.error == "" and proc.returncode != 0:
            status.error = (proc.stderr.strip() or proc.stdout.strip())[-1000:]
        return status

    def _finalize(self, run_id: str, raw_status: str, pid: int) -> RunStatus:
        status = _STATUS_MAP.get(raw_status.lower().strip(), "UNKNOWN")
        reconciled = False
        if status == RUNNING and pid and not _pid_alive(pid):
            # Dead owner: Dagu never reconciles this on the local backend.
            status = INTERRUPTED
            reconciled = True
        elif status == RUNNING and pid == 0:
            # 'running' with no discoverable owner is not provable liveness.
            status = INTERRUPTED
            reconciled = True
        return RunStatus(dag_run_id=run_id, status=status, raw_status=raw_status,
                         pid=pid, reconciled=reconciled)

    @staticmethod
    def _parse_status_text(text: str) -> str:
        # 'dagu status' prints 'Status: <x>'; 'dagu start' prints 'Result: <x>'.
        for line in reversed(text.splitlines()):
            stripped = line.strip()
            low = stripped.lower()
            for prefix in ("status:", "result:"):
                if low.startswith(prefix):
                    return stripped.split(":", 1)[1].strip()
        return ""

    def _find_pid(self, dag_path: Path, run_id: str) -> int:
        """Find the recorded PID of a run from Dagu's on-disk event log."""
        if self.dagu_home is None:
            return 0
        root = Path(self.dagu_home) / "data" / "dag-runs"
        if not root.is_dir():
            return 0
        matches = list(root.rglob(f"dag-run_*_{run_id}/**/status.jsonl"))
        for status_file in sorted(matches, reverse=True):
            pid = self._pid_from_events(status_file)
            if pid:
                return pid
        return 0

    @staticmethod
    def _pid_from_events(status_file: Path) -> int:
        last_pid = 0
        try:
            for line in status_file.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                pid = event.get("pid")
                if isinstance(pid, int) and pid > 0:
                    last_pid = pid
        except OSError:
            return 0
        return last_pid


def get_adapter(name: str = "dagu", **kwargs) -> EngineAdapter:
    if name == "dagu":
        return DaguAdapter(**kwargs)
    raise MtmError(
        code=Code.CONFIG_INVALID,
        message=f"Unknown workflow engine adapter {name!r}.",
        action="Use the default 'dagu' adapter.",
        stage="engine-select",
    )
