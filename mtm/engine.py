"""Headless workflow engine adapter.

The product CLI never depends on the workflow engine directly: it talks to a
small adapter interface, so a fallback engine can replace the current one
without touching the CLI (``worklog/decisions/0003-third-party-licenses-and-dagu.md``).

Two execution backends are supported, chosen explicitly by the CLI:

* :class:`DaguBackend` — runs the pinned ``dagu`` binary **inside the generic
  Docker image**. The bench is host-mounted, so its state and results persist on
  the host; ``DAGU_HOME`` is a bench-local directory, never Docker's or the
  host's global Dagu state. This is the default and the only backend that works
  on a machine that has Docker but no host Dagu.
* :class:`LocalProcess` — the engine parsed from ``docker run ... dagu status``
  text. It never spawns a host process merely because one happens to exist; a
  host Dagu is used only when a caller selects the ``host`` backend explicitly.

The measured Dagu gate (``worklog/decisions/0005-dagu-gate-proof.md``) found one
limitation this adapter must handle, not hide:

    After a SIGKILL of the Dagu process, the run stays reported ``running``
    forever. Dagu does not reconcile a dead owner on the local backend.

Therefore a ``running`` run whose recorded PID is no longer alive is reported as
:data:`INTERRUPTED` — never ``RUNNING`` and never a success. This satisfies the
"never a false success" clause of ``docs/architecture/workflow-failures.md``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
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

#: Dagu exit status meaning "the DAG ran and at least one step failed". This is a
#: domain finding preserved as evidence, not a workflow/infrastructure failure.
DAGU_GO_EXIT_ERROR = 1


@dataclass
class RunStatus:
    """Normalized result of a run, with everything the error contract needs."""

    dag_run_id: str
    status: str
    raw_status: str = ""
    pid: int = 0
    engine_exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    reconciled: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.status in (SUCCEEDED, FAILED, INTERRUPTED, BLOCKED, SKIPPED)

    @property
    def cause(self) -> str:
        """Short, actionable cause extracted from the engine's own output."""
        if self.status == FAILED:
            text = (self.stderr or self.stdout).strip()
            if text:
                return text[-1000:]
            return "the workflow ran and at least one step failed"
        if self.status == INTERRUPTED:
            return "the engine process is no longer running"
        return ""


class CommandRunner:
    """Runs a command (host or docker) and returns its completed result."""

    def run(self, argv: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout)

    def which(self, name: str) -> str | None:
        return shutil.which(name)


class DockerRunner(CommandRunner):
    """Runs a command inside a container image, with a host-mounted bench.

    On Linux/WSL the container runs as the invoking user (``--user UID:GID``) so
    files it creates in the host-mounted bench stay owned by the user. This is
    deliberately *not* a ``chown -R`` fallback: that would rewrite ownership of
    selected inputs. If the ids cannot be resolved, ``--user`` is omitted and
    the container runs as root, which the CLI reports as a limitation.
    """

    def __init__(self, image_ref: str, bench: Path, *, extra_mounts: list[str] | None = None,
                 user: str | None = None):
        self.image_ref = image_ref
        self.bench = bench
        self.extra_mounts = extra_mounts or []
        self.user = user if user is not None else _invoking_user()

    def container_argv(self, inner: list[str], *, user: str | None = None) -> list[str]:
        effective_user = user if user is not None else self.user
        argv = ["docker", "run", "--rm"]
        if effective_user:
            argv += ["--user", effective_user]
        argv += ["-v", f"{self.bench}:/bench"]
        for mount in self.extra_mounts:
            argv += ["-v", mount]
        argv += [self.image_ref, *inner]
        return argv

    def run(self, argv: list[str], *, timeout: int | None = None, user: str | None = None) -> subprocess.CompletedProcess:
        if argv and argv[0] == "dagu":
            argv = self.container_argv(argv, user=user)
        return super().run(argv, timeout=timeout)


def _invoking_user() -> str | None:
    """UID:GID of the invoking user, or None if it cannot be determined."""
    try:
        if hasattr(os, "getuid"):
            return f"{os.getuid()}:{os.getgid()}"
    except OSError:  # pragma: no cover - platform specific
        pass
    return None


@dataclass
class BenchPaths:
    """Bench-local paths shared by the adapter (host view and container view)."""

    bench: Path

    @property
    def dagu_home_host(self) -> Path:
        return self.bench / ".mtm" / "dagu"

    @property
    def dagu_home_container(self) -> str:
        return "/bench/.mtm/dagu"


class EngineAdapter:
    """Interface every workflow engine adapter implements."""

    name = "abstract"

    def __init__(self, backend: CommandRunner, paths: BenchPaths, *, workflow_dir: Path | None = None):
        self.backend = backend
        self.paths = paths
        self.workflow_dir = workflow_dir

    # -- environment --------------------------------------------------------

    def available(self) -> bool:
        raise NotImplementedError

    def version(self) -> str:  # pragma: no cover - overridden
        return ""

    def validate(self, dag_path: Path) -> None:
        raise NotImplementedError

    def dry(self, dag_path: Path) -> RunStatus:
        raise NotImplementedError

    def run(self, dag_path: Path, run_id: str) -> RunStatus:
        raise NotImplementedError

    def run_status(self, dag_path: Path, run_id: str) -> RunStatus:
        raise NotImplementedError

    # -- helpers ------------------------------------------------------------

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

    def _finalize(self, run_id: str, raw_status: str, pid: int,
                  *, exit_code: int = 0, stdout: str = "", stderr: str = "") -> RunStatus:
        status = _STATUS_MAP.get(raw_status.lower().strip(), "UNKNOWN")
        reconciled = False
        if status == RUNNING and not _pid_alive(pid):
            # Dead owner: Dagu never reconciles this on the local backend.
            status = INTERRUPTED
            reconciled = True
        elif status == "UNKNOWN" and exit_code:
            status = FAILED
        return RunStatus(dag_run_id=run_id, status=status, raw_status=raw_status,
                         pid=pid, engine_exit_code=exit_code, stdout=stdout,
                         stderr=stderr, reconciled=reconciled)


def _pid_alive(pid: int) -> bool:
    # A containerized run's PID is meaningless on the host, so liveness is only
    # meaningful for a host-local process. Callers pass pid=0 for the docker
    # backend, which is treated as "liveness not determinable".
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by someone else
    return True


class DaguBackend(EngineAdapter):
    """Dagu driven through a :class:`CommandRunner` (docker by default)."""

    name = "dagu"

    def __init__(self, backend: CommandRunner, paths: BenchPaths, *,
                 workflow_dir: Path | None = None, dagu_bin: str = "dagu"):
        super().__init__(backend, paths, workflow_dir=workflow_dir)
        self.dagu_bin = dagu_bin
        # The DAG path as the engine must see it. For the docker backend the
        # workflow directory is bind-mounted at /workflows (read-only); for a
        # host backend it stays the real path.
        self._mount_workflow_dir = isinstance(backend, DockerRunner)

    # -- environment --------------------------------------------------------

    def available(self) -> bool:
        if isinstance(self.backend, DockerRunner):
            return self.backend.which("docker") is not None
        return self.backend.which(self.dagu_bin) is not None

    def version(self) -> str:
        proc = self.backend.run([self.dagu_bin, "version"])
        return proc.stdout.strip()

    def _dag_arg(self, dag_path: Path) -> str:
        if self._mount_workflow_dir:
            self.backend.extra_mounts = [f"{dag_path.parent}:/workflows:ro"]
            return f"/workflows/{dag_path.name}"
        return str(dag_path)

    # -- operations ---------------------------------------------------------

    def validate(self, dag_path: Path) -> None:
        proc = self.backend.run([self.dagu_bin, "validate", self._dag_arg(dag_path)])
        if proc.returncode != 0:
            raise MtmError(
                code=Code.CONFIG_INVALID,
                message=f"Workflow definition failed validation: {dag_path.name}.",
                action="Fix the DAG YAML; a file passed by path must not define 'name:'.",
                stage="workflow-validate",
                cause=(proc.stderr.strip() or proc.stdout.strip()),
                input_reference=str(dag_path),
            )

    def dry(self, dag_path: Path) -> RunStatus:
        proc = self.backend.run([self.dagu_bin, "dry", self._dag_arg(dag_path)])
        status = SUCCEEDED if proc.returncode == 0 else FAILED
        return RunStatus(dag_run_id="", status=status, raw_status=status,
                         engine_exit_code=proc.returncode,
                         stdout=proc.stdout, stderr=proc.stderr)

    def run(self, dag_path: Path, run_id: str) -> RunStatus:
        proc = self.backend.run(
            [self.dagu_bin, "start", "--run-id", run_id, self._dag_arg(dag_path)]
        )
        return self._reconcile(run_id, proc)

    def run_status(self, dag_path: Path, run_id: str) -> RunStatus:
        proc = self.backend.run([self.dagu_bin, "status", "--run-id", run_id, self._dag_arg(dag_path)])
        raw = self._parse_status_text(proc.stdout + proc.stderr)
        # Docker runs have no meaningful host PID; liveness comes from the
        # engine's own state. A 'running' status is only reconciled when a PID
        # is available (host backend).
        pid = self._find_pid(run_id) if isinstance(self.backend, LocalRunner) else 0
        return self._finalize(run_id, raw, pid, exit_code=proc.returncode,
                              stdout=proc.stdout, stderr=proc.stderr)

    # -- internals ----------------------------------------------------------

    def _reconcile(self, run_id: str, proc: subprocess.CompletedProcess) -> RunStatus:
        raw = self._parse_status_text(proc.stdout + proc.stderr)
        # For the docker backend the daemon's exit code is Dagu's own exit code.
        status = self._finalize(run_id, raw, 0, exit_code=proc.returncode,
                                stdout=proc.stdout, stderr=proc.stderr)
        if status.status in (PENDING, RUNNING) and proc.returncode != 0:
            status.status = FAILED
        return status

    def _find_pid(self, run_id: str) -> int:
        root = self.paths.dagu_home_host / "data" / "dag-runs"
        if not root.is_dir():
            return 0
        for status_file in sorted(root.rglob(f"dag-run_*_{run_id}/**/status.jsonl"), reverse=True):
            pid = _pid_from_events(status_file)
            if pid:
                return pid
        return 0


class LocalRunner(CommandRunner):
    """Runs the engine directly on the host (explicit opt-in only)."""

    def __init__(self, env: dict[str, str] | None = None):
        self.env = env

    def run(self, argv: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(argv, capture_output=True, text=True, check=False,
                              timeout=timeout, env=self.env)


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


def make_adapter(backend_name: str, *, image_ref: str, bench: Path,
                 workflow_dir: Path | None = None, host_dagu: str | None = None) -> DaguBackend:
    """Build the engine adapter for the requested backend.

    ``docker`` (default) runs Dagu inside the pinned image; ``host`` runs a host
    Dagu explicitly and is never chosen implicitly.
    """
    paths = BenchPaths(bench)
    if backend_name == "docker":
        return DaguBackend(DockerRunner(image_ref, bench), paths, workflow_dir=workflow_dir)
    if backend_name == "host":
        return DaguBackend(LocalRunner(), paths, workflow_dir=workflow_dir,
                           dagu_bin=host_dagu or "dagu")
    raise MtmError(
        code=Code.CONFIG_INVALID,
        message=f"Unknown workflow backend {backend_name!r}.",
        action="Use 'docker' (default) or 'host'.",
        stage="engine-select",
    )
