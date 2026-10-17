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
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .errors import Category, Code, MtmError

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

#: A step types its own outcome by emitting this output variable, e.g.
#: ``echo MTM_CATEGORY=TEST_FINDING``. Only a value listed in
#: :data:`REPORTED_CATEGORIES` is honoured; an unknown or absent value leaves the
#: outcome untyped (a workflow/tool failure), never a test finding.
CATEGORY_MARKER = "MTM_CATEGORY"

#: marker value -> (category, stable code, exit code). Kept tiny and explicit:
#: the wrapper must not guess a taxonomy from a generic shell exit status.
REPORTED_CATEGORIES: dict[str, tuple[str, str, int]] = {
    "TEST_FINDING": (Category.TEST_FINDING, Code.TEST_ASSERTION, 10),  # EXIT_TEST_FINDINGS
    "INPUT_LIMITATION": (Category.INPUT_LIMITATION, Code.HOST_UNSUPPORTED, 20),  # EXIT_PARTIAL
    "TOOL": (Category.TOOL, Code.TOOL_FAILED, 30),  # EXIT_TOOL_FAILURE
}


def _pid_alive(pid: int) -> bool | None:
    """Whether *pid* is alive; ``None`` when liveness cannot be determined.

    A PID is only meaningful for a host-local process. Callers that run the
    engine in a container pass a sentinel (``<= 0``), and this returns ``None``
    — "unknown", never ``False`` — so an active run is not mistaken for a dead
    one and reported as INTERRUPTED.
    """
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by someone else
    except OSError:  # pragma: no cover - platform specific
        return None
    return True


#: Dagu node status constants (see dagu ``scheduler``): the numeric ``status``
#: field of each node in ``status.jsonl``.
DAGU_NODE_FAILED = 2
DAGU_NODE_ABORTED = 3


def _decode_marker(raw: str | None) -> str:
    """Extract a marker value from Dagu's stored ``outputVariables`` string.

    Dagu stores an output variable both under its own name and under
    ``DAGU_OUTPUT.<name>`` and loosely appends the name again for booleans, so
    the raw text is ``MTM_CATEGORY=TEST_FINDING`` or ``TEST_FINDING``. Take the
    last ``=``-separated token and upper-case it; unknown values are ignored
    downstream.
    """
    if not raw:
        return ""
    value = str(raw).rsplit("=", 1)[-1].strip()
    return value.upper()


def _step_evidence_from_state(dagu_home: Path, run_id: str) -> list[dict]:
    """Parse per-step evidence from Dagu's own run state, host-side.

    Returns one entry per step: ``{name, status, failed, aborted, error,
    category}``. ``category`` is the value a step declared via
    :data:`CATEGORY_MARKER`, or '' when it declared none. Absent/corrupt state
    yields an empty list — untyped, which is the safe default.
    """
    root = dagu_home / "data" / "dag-runs"
    if not root.is_dir():
        return []
    candidates = sorted(root.rglob(f"dag-run_*_{run_id}/**/status.jsonl"), reverse=True)
    if not candidates:
        return []
    try:
        lines = [ln for ln in candidates[0].read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not lines:
            return []
        state = json.loads(lines[-1])
    except (OSError, json.JSONDecodeError):
        return []

    evidence: list[dict] = []
    for node in state.get("nodes", []) or []:
        step = node.get("step", {}) or {}
        status = node.get("status")
        outvars = node.get("outputVariables") or {}
        marker = ""
        for key, raw in outvars.items():
            if str(key).split(".")[-1] == CATEGORY_MARKER:
                marker = _decode_marker(raw)
        evidence.append({
            "name": step.get("name", ""),
            "status": status,
            "failed": status == DAGU_NODE_FAILED,
            "aborted": status == DAGU_NODE_ABORTED,
            "error": (node.get("error") or "").strip(),
            "category": marker if marker in REPORTED_CATEGORIES else "",
        })
    return evidence


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
    #: Always populated. ``None`` = liveness could not be determined.
    owner_alive: bool | None = None
    #: Human-readable justification for the liveness decision above.
    liveness_evidence: str = ""
    #: Typed per-step evidence parsed from the engine's own run state.
    step_evidence: list[dict] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in (SUCCEEDED, FAILED, INTERRUPTED, BLOCKED, SKIPPED)

    @property
    def category_hint(self) -> str:
        """Strongest typed category a failed step declared, or '' if none.

        A verification tool types its own outcome by emitting a ``MTM_CATEGORY``
        output (see :data:`CATEGORY_MARKER`); anything else is untyped. The
        taxonomy must never invent a test finding from a generic shell failure.
        """
        for step in self.step_evidence:
            if step.get("failed") and step.get("category"):
                return str(step["category"])
        return ""

    @property
    def cause(self) -> str:
        """Short, actionable cause extracted from the engine's own output."""
        if self.status == FAILED:
            failed = [s for s in self.step_evidence if s.get("failed")]
            if failed:
                names = ", ".join(s["name"] for s in failed if s.get("name"))
                detail = next((s.get("error") for s in failed if s.get("error")), "")
                base = f"failed step(s): {names}" if names else "at least one step failed"
                return f"{base}: {detail}".strip(": ") if detail else base
            text = (self.stderr or self.stdout).strip()
            if text:
                return text[-1000:]
            return "the workflow ran and at least one step failed"
        if self.status == INTERRUPTED:
            return self.liveness_evidence or "the engine process is no longer running"
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
                 user: str | None = None, run_label: str | None = None):
        self.image_ref = image_ref
        self.bench = bench
        self.extra_mounts = extra_mounts or []
        self.user = user if user is not None else _invoking_user()
        #: Set by the adapter for an owner run, so liveness can be probed later
        #: with ``docker ps --filter label=mtm.run-id=<id>``.
        self.run_label = run_label

    def container_argv(self, inner: list[str], *, user: str | None = None,
                       run_label: str | None = None) -> list[str]:
        effective_user = user if user is not None else self.user
        label = run_label if run_label is not None else self.run_label
        argv = ["docker", "run", "--rm"]
        if effective_user:
            argv += ["--user", effective_user]
        if label:
            argv += ["--label", f"{OWNER_LABEL}={label}"]
        # The bench is mounted READ-ONLY: selected inputs live inside it and must
        # never be writable from a run. Only the engine state directory is
        # mounted read-write, so Dagu can record runs without endangering inputs.
        (self.bench / ".mtm").mkdir(parents=True, exist_ok=True)
        argv += ["-v", f"{self.bench}:/bench:ro"]
        argv += ["-v", f"{self.bench / '.mtm'}:/bench/.mtm"]
        for mount in self.extra_mounts:
            argv += ["-v", mount]
        argv += [self.image_ref, *inner]
        return argv

    def owner_alive(self, run_id: str) -> tuple[bool | None, str]:
        """Whether an owner container for *run_id* is still running.

        Returns ``(alive, evidence)``. Evidence is the container id, or an
        explanation when liveness is undeterminable (Docker unreachable).
        """
        proc = subprocess.run(
            ["docker", "ps", "--quiet", "--filter", f"label={OWNER_LABEL}={run_id}"],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            return None, "docker ps failed; liveness not determinable"
        ids = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if ids:
            return True, f"owner container {ids[0][:12]} running"
        return False, f"no running container labelled {OWNER_LABEL}={run_id}"

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


#: Docker label used to find the owner container of a run for liveness checks.
OWNER_LABEL = "mtm.run-id"


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
                  *, exit_code: int = 0, stdout: str = "", stderr: str = "",
                  owner_alive: bool | None = None, liveness_evidence: str = "",
                  step_evidence: list[dict] | None = None) -> RunStatus:
        status = _STATUS_MAP.get(raw_status.lower().strip(), "UNKNOWN")
        reconciled = False
        if status == RUNNING and owner_alive is False:
            # Liveness was positively determined and the owner is gone: Dagu
            # never reconciles this on the local backend. When liveness is
            # unknown (``None``, e.g. a docker run) the run stays RUNNING — it
            # must never be reported INTERRUPTED merely because no host PID
            # exists.
            status = INTERRUPTED
            reconciled = True
        elif status == "UNKNOWN" and exit_code:
            status = FAILED
        return RunStatus(dag_run_id=run_id, status=status, raw_status=raw_status,
                         pid=pid, engine_exit_code=exit_code, stdout=stdout,
                         stderr=stderr, reconciled=reconciled,
                         owner_alive=owner_alive, liveness_evidence=liveness_evidence,
                         step_evidence=list(step_evidence or []))


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
        if isinstance(self.backend, DockerRunner):
            # Label the owner container so a later status query can positively
            # confirm whether this run is still alive (vs. a killed run).
            self.backend.run_label = run_id
        proc = self.backend.run(
            [self.dagu_bin, "start", "--run-id", run_id, self._dag_arg(dag_path)]
        )
        return self._reconcile(run_id, proc)

    def run_status(self, dag_path: Path, run_id: str) -> RunStatus:
        proc = self.backend.run([self.dagu_bin, "status", "--run-id", run_id, self._dag_arg(dag_path)])
        raw = self._parse_status_text(proc.stdout + proc.stderr)
        owner_alive, evidence = self._liveness(run_id)
        pid = self._find_pid(run_id) if isinstance(self.backend, LocalRunner) else 0
        return self._finalize(run_id, raw, pid, exit_code=proc.returncode,
                              stdout=proc.stdout, stderr=proc.stderr,
                              owner_alive=owner_alive, liveness_evidence=evidence,
                              step_evidence=self._step_evidence(run_id))

    # -- internals ----------------------------------------------------------

    def _liveness(self, run_id: str) -> tuple[bool | None, str]:
        """Determine whether the run's owner is still alive, with evidence.

        Host backend: the OS PID from the run state. Docker backend: the owner
        container found by its ``mtm.run-id`` label. If neither can be
        determined, returns ``(None, reason)`` so the run is *not* reconciled.
        """
        if isinstance(self.backend, DockerRunner):
            return self.backend.owner_alive(run_id)
        pid = self._find_pid(run_id)
        if pid <= 0:
            return None, "no host PID recorded for this run"
        alive = _pid_alive(pid)
        return alive, f"host PID {pid} {'alive' if alive else 'dead'}"

    def _step_evidence(self, run_id: str) -> list[dict]:
        return _step_evidence_from_state(self.paths.dagu_home_host, run_id)

    def _reconcile(self, run_id: str, proc: subprocess.CompletedProcess) -> RunStatus:
        raw = self._parse_status_text(proc.stdout + proc.stderr)
        # For the docker backend the daemon's exit code is Dagu's own exit code.
        status = self._finalize(run_id, raw, 0, exit_code=proc.returncode,
                                stdout=proc.stdout, stderr=proc.stderr,
                                step_evidence=self._step_evidence(run_id))
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
