"""Command implementations for the PR 1 control surface.

Each function takes parsed arguments plus a context and returns a structured
operation record (``{status, ...}``). The CLI wrapper turns that record into
stdout and the process exit code, and persists it as ``last-operation.json``.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import MILESTONE, SCHEMA_VERSION, __version__
from .docker import (
    base_image_digest,
    build_image,
    docker_available,
    image_digest,
    image_exists,
    image_ref,
)
from .engine import DaguAdapter, INTERRUPTED, RUNNING
from .errors import (
    Code,
    EXIT_INVALID_INPUT,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_TEST_FINDINGS,
    EXIT_TOOL_FAILURE,
    MtmError,
    most_severe,
)
from .paths import (
    BENCH_SUBDIRS,
    PROJECT_FILE,
    ProjectConfig,
    find_bench_root,
    logs_dir,
    new_bench_id,
    read_last_operation,
    require_bench_root,
    sha256_file,
    state_dir,
    write_last_operation,
    write_project,
)

CREDENTIALS_FILE = Path.home() / ".config" / "utest-machinery" / "credentials.env"

#: Commands that are declared in the user guide but belong to later milestones.
NOT_YET_IMPLEMENTED = {
    "plan": "PR5",
    "generate": "PR2",
    "verify": "PR3",
    "interpret": "PR5",
    "report": "PR5",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _op_id() -> str:
    return f"op-{uuid.uuid4().hex[:12]}"


def _record(operation_id: str, stage: str, status: str, **extra: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "operation_id": operation_id,
        "stage": stage,
        "status": status,
        "mtm_version": __version__,
        "milestone": MILESTONE,
        "timestamp": _now(),
    }
    record.update(extra)
    return record


def _error_record(operation_id: str, err: MtmError) -> dict[str, Any]:
    record = _record(
        operation_id,
        err.stage or "unknown",
        err.category if err.exit_code == 40 else "FAILED",
        scope_id=err.scope_id,
        category=err.category,
        code=err.code,
        message=err.message,
        cause=err.cause,
        action=err.action,
        retryable=err.retryable,
        log_path=err.log_path,
        input_reference=err.input_reference,
    )
    if err.tool_name:
        record["tool_name"] = err.tool_name
    if err.tool_version:
        record["tool_version"] = err.tool_version
    record["exit_code"] = err.exit_code
    return record


# --- init -------------------------------------------------------------------


def cmd_init(args: Any, ctx: "Context") -> dict[str, Any]:
    op = _op_id()
    root = Path(args.directory).resolve()
    if not root.exists():
        if not args.create_dir:
            raise MtmError(
                code=Code.INPUT_MISSING,
                message=f"Target directory does not exist: {root}",
                action="Create it first, or pass --create-dir.",
                stage="init",
                input_reference=str(root),
            )
        root.mkdir(parents=True, exist_ok=True)

    project_file = root / PROJECT_FILE
    if project_file.exists() and not args.force:
        raise MtmError(
            code=Code.BENCH_EXISTS,
            message=f"A bench already exists at {root}.",
            action="Use --force to reinitialise (scope history is preserved).",
            stage="init",
            input_reference=str(project_file),
        )

    existing = None
    if project_file.exists():
        existing = ProjectConfig.load(project_file)

    config = ProjectConfig(
        bench_id=(existing.bench_id if existing else new_bench_id(root)),
        source_roots=list(args.source or (existing.source_roots if existing else [])),
        spec_roots=list(args.spec or (existing.spec_roots if existing else [])),
    )
    write_project(root, config)

    created: list[str] = []
    for name in BENCH_SUBDIRS:
        target = root / name
        if not target.exists():
            (target / ".gitkeep").parent.mkdir(parents=True, exist_ok=True)
            (target / ".gitkeep").touch()
            created.append(name)
    state_dir(root)

    return _record(
        op, "init", "SUCCEEDED",
        bench_root=str(root),
        bench_id=config.bench_id,
        project_file=str(project_file),
        created_dirs=created,
        schema_version=SCHEMA_VERSION,
    )


# --- scope ------------------------------------------------------------------


def _load_config(root: Path) -> ProjectConfig:
    return ProjectConfig.load(root / PROJECT_FILE)


def _scope_id(path: Path) -> str:
    return f"scope-{sha256_file(path)[:12]}" if path.is_file() else f"scope-{abs(hash(str(path))) % 10**10:010d}"


def cmd_scope_list(args: Any, ctx: "Context") -> dict[str, Any]:
    op = _op_id()
    root = require_bench_root()
    config = _load_config(root)
    return _record(op, "scope-list", "SUCCEEDED",
                   bench_root=str(root), scopes=config.scopes)


def cmd_scope_add(args: Any, ctx: "Context") -> dict[str, Any]:
    op = _op_id()
    root = require_bench_root()
    config = _load_config(root)

    entry: dict[str, Any] = {
        "id": f"scope-{uuid.uuid4().hex[:12]}" if not args.id else args.id,
        "path": args.path,
        "kind": args.kind,
        "added_at": _now(),
    }
    resolved = (root / args.path).resolve() if not Path(args.path).is_absolute() else Path(args.path)
    if not resolved.exists():
        raise MtmError(
            code=Code.INPUT_MISSING,
            message=f"Selected path does not exist: {args.path}",
            action="Correct the path or add it after it exists.",
            stage="scope-add",
            input_reference=args.path,
        )
    if resolved.is_file():
        entry["sha256"] = sha256_file(resolved)
    else:
        entry["sha256"] = ""

    if any(s.get("path") == args.path for s in config.scopes):
        raise MtmError(
            code=Code.SCOPE_INVALID,
            message=f"Path already selected: {args.path}",
            action="Remove it first, or select a different path.",
            stage="scope-add",
            input_reference=args.path,
        )
    config.scopes.append(entry)
    write_project(root, config)
    return _record(op, "scope-add", "SUCCEEDED",
                   bench_root=str(root), scope=entry, scope_count=len(config.scopes))


def cmd_scope_remove(args: Any, ctx: "Context") -> dict[str, Any]:
    op = _op_id()
    root = require_bench_root()
    config = _load_config(root)
    before = len(config.scopes)
    removed = [s for s in config.scopes if s.get("id") == args.id or s.get("path") == args.id]
    if not removed:
        raise MtmError(
            code=Code.SCOPE_INVALID,
            message=f"No selected scope matches {args.id!r}.",
            action="Run 'mtm scope list' to see current scope identifiers and paths.",
            stage="scope-remove",
            input_reference=args.id,
        )
    config.scopes = [s for s in config.scopes if s not in removed]
    write_project(root, config)
    # Removal retains history: we return what was removed, nothing is deleted
    # from disk and no historical evidence is touched.
    return _record(op, "scope-remove", "SUCCEEDED",
                   bench_root=str(root), removed=removed,
                   scope_count=len(config.scopes), previous_count=before)


# --- status -----------------------------------------------------------------


def cmd_status(args: Any, ctx: "Context") -> dict[str, Any]:
    op = _op_id()
    root = find_bench_root()
    if root is None:
        return _record(op, "status", "NO_BENCH", bench_root=None,
                       message="No bench in this directory or any parent.")
    last = read_last_operation(root)
    config = _load_config(root)
    return _record(
        op, "status", "SUCCEEDED",
        bench_root=str(root),
        bench_id=config.bench_id,
        scope_count=len(config.scopes),
        last_operation=last,
    )


# --- doctor -----------------------------------------------------------------


@dataclass
class Check:
    name: str
    ok: bool
    level: str  # "error" | "warning" | "info"
    message: str
    action: str = ""


def _run_checks(root: Path | None, ctx: "Context") -> list[Check]:
    checks: list[Check] = []

    # Bench
    if root is None:
        checks.append(Check("bench", False, "error",
                            "No bench found in this directory or any parent",
                            "Run 'mtm init'."))
    else:
        try:
            ProjectConfig.load(root / PROJECT_FILE)
            checks.append(Check("bench", True, "info", f"Bench at {root}"))
        except MtmError as exc:
            checks.append(Check("bench", False, "error", exc.message, exc.action))

    # Docker
    info = docker_available()
    if info.available:
        checks.append(Check("docker", True, "info", f"Docker server {info.server_version}"))
    else:
        checks.append(Check("docker", False, "error",
                            f"Docker unavailable: {info.error}",
                            "Start the Docker daemon (WSL Docker Desktop integration or local engine)."))

    # Image
    if info.available:
        if image_exists(ctx.image_tag):
            checks.append(Check("image", True, "info",
                                f"Image {image_ref(ctx.image_tag)} present ({image_digest(ctx.image_tag)})"))
        else:
            checks.append(Check("image", False, "error",
                                f"Image {image_ref(ctx.image_tag)} not built",
                                "Run 'mtm install'."))

    # Credentials (required, per user decision D5)
    if CREDENTIALS_FILE.is_file():
        mode = CREDENTIALS_FILE.stat().st_mode & 0o777
        if mode & 0o077:
            checks.append(Check("credentials", False, "error",
                                f"{CREDENTIALS_FILE} is group/world-accessible (mode {mode:o})",
                                "chmod 600 the file."))
        else:
            keys = _credential_keys()
            missing = {"SCW_BASE_URL", "SCW_BEARER_TOKEN"} - keys
            if missing:
                checks.append(Check("credentials", False, "error",
                                    f"Credentials file missing keys: {', '.join(sorted(missing))}",
                                    "Add the missing keys (values from your shell environment)."))
            else:
                checks.append(Check("credentials", True, "info",
                                    f"Credentials present (mode {mode:o})"))
    else:
        checks.append(Check("credentials", False, "error",
                            f"Credentials file absent: {CREDENTIALS_FILE}",
                            "Create it with SCW_BASE_URL and SCW_BEARER_TOKEN (mode 600)."))

    # Host C toolchain — warning only: the in-image toolchain arrives in PR 3.
    if shutil.which("cc") or shutil.which("gcc") or shutil.which("clang"):
        checks.append(Check("host-cc", True, "info", "A host C compiler is available"))
    else:
        checks.append(Check("host-cc", False, "warning",
                            "No host C compiler found",
                            "Not required for PR 1; in-image compilers arrive in PR 3."))

    return checks


def _credential_keys() -> set[str]:
    keys: set[str] = set()
    try:
        for line in CREDENTIALS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                keys.add(line.split("=", 1)[0].strip())
    except OSError:
        pass
    return keys


def cmd_doctor(args: Any, ctx: "Context") -> dict[str, Any]:
    op = _op_id()
    root = find_bench_root()
    checks = _run_checks(root, ctx)
    errors = [c for c in checks if not c.ok and c.level == "error"]
    warnings = [c for c in checks if not c.ok and c.level == "warning"]
    status = "SUCCEEDED" if not errors else "FAILED"
    operation_errors = [
        _error_record(op, MtmError(code=_code_for(c.name), message=c.message,
                                   action=c.action, stage="doctor"))
        for c in errors
    ]
    return _record(
        op, "doctor", status,
        bench_root=str(root) if root else None,
        checks=[c.__dict__ for c in checks],
        errors=operation_errors,
        warnings=[w.__dict__ for w in warnings],
        exit_code=(EXIT_OK if not errors else most_severe([e["exit_code"] for e in operation_errors])),
    )


def _code_for(check_name: str) -> str:
    return {
        "bench": Code.BENCH_MISSING,
        "docker": Code.DOCKER_UNAVAILABLE,
        "image": Code.IMAGE_MISSING,
        "credentials": Code.CREDENTIALS_MISSING,
    }.get(check_name, Code.OPERATION_FAILED)


# --- install ----------------------------------------------------------------


def cmd_install(args: Any, ctx: "Context") -> dict[str, Any]:
    op = _op_id()
    info = docker_available()
    if not info.available:
        raise MtmError(
            code=Code.DOCKER_UNAVAILABLE,
            message=f"Docker is not available: {info.error}",
            action="Start Docker (WSL integration or local engine) and retry.",
            stage="install",
            tool_name="docker",
        )
    context = Path(ctx.context).resolve()
    ref = build_image(context, ctx.image_tag)
    digest = image_digest(ctx.image_tag)
    lock = context / "image" / "toolchain.lock"
    return _record(
        op, "install", "SUCCEEDED",
        image=ref,
        image_digest=digest,
        base_digest=base_image_digest(lock) if lock.is_file() else "",
    )


# --- workflow ---------------------------------------------------------------


def cmd_workflow_view(args: Any, ctx: "Context") -> dict[str, Any]:
    op = _op_id()
    root = require_bench_root()
    dag = _sample_dag_path(root, ctx)
    adapter = DaguAdapter(dagu_home=ctx.dagu_home)
    if args.dry:
        status = adapter.dry(dag)
        return _record(op, "workflow-dry", status.status,
                       dag=str(dag), raw_status=status.raw_status, error=status.error,
                       engine=adapter.name)
    steps = _read_dag_steps(dag)
    return _record(op, "workflow-view", "SUCCEEDED",
                   dag=str(dag), steps=steps, engine=adapter.name,
                   text_dag=_render_text_dag(steps))


def cmd_workflow_run(args: Any, ctx: "Context") -> dict[str, Any]:
    op = _op_id()
    root = require_bench_root()
    dag = _sample_dag_path(root, ctx)
    adapter = DaguAdapter(dagu_home=ctx.dagu_home)
    if not adapter.available():
        raise MtmError(
            code=Code.TOOL_FAILED,
            message="Dagu executable not found.",
            action="Run inside the generic image, or install the pinned Dagu (see image/SBOM.md).",
            stage="workflow-run",
            tool_name="dagu",
        )
    adapter.validate(dag)
    run_id = args.run_id or f"run-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:6]}"
    status = adapter.run(dag, run_id)
    exit_code = _exit_for_engine_status(status.status)
    record = _record(
        op, "workflow-run", status.status,
        dag=str(dag), run_id=run_id, engine=adapter.name,
        raw_status=status.raw_status, pid=status.pid,
        reconciled=status.reconciled, error=status.error,
        exit_code=exit_code,
    )
    if status.status == INTERRUPTED:
        record["category"] = "WORKFLOW"
        record["code"] = Code.WORKFLOW_INTERRUPTED
        record["action"] = "Rerun with 'mtm workflow run'; the previous owner process is gone."
    return record


def _exit_for_engine_status(status: str) -> int:
    return {
        "SUCCEEDED": EXIT_OK,
        "FAILED": EXIT_TOOL_FAILURE,
        "INTERRUPTED": 130,
        "BLOCKED": EXIT_PARTIAL,
    }.get(status, EXIT_TOOL_FAILURE)


def _sample_dag_path(root: Path, ctx: "Context") -> Path:
    """Use a bench-local DAG if the user placed one, else the bundled sample."""
    local = root / "workflows" / "sample.yaml"
    if local.is_file():
        return local
    return Path(ctx.context).resolve() / "workflows" / "sample.yaml"


def _read_dag_steps(dag: Path) -> list[dict[str, Any]]:
    import yaml

    data = yaml.safe_load(dag.read_text(encoding="utf-8")) or {}
    steps = []
    for step in data.get("steps", []):
        steps.append({"name": step.get("name", ""), "depends": list(step.get("depends", []) or [])})
    return steps


def _render_text_dag(steps: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for step in steps:
        deps = step["depends"]
        arrow = f"  (after: {', '.join(deps)})" if deps else ""
        lines.append(f"  - {step['name']}{arrow}")
    return "DAG steps:\n" + "\n".join(lines)


# --- not implemented --------------------------------------------------------


def cmd_not_implemented(args: Any, ctx: "Context") -> dict[str, Any]:
    op = _op_id()
    command = args._command
    milestone = NOT_YET_IMPLEMENTED.get(command, "a later milestone")
    raise MtmError(
        code=Code.NOT_IMPLEMENTED,
        message=f"'{command}' is specified but not implemented in this build ({MILESTONE}).",
        action=f"It arrives in {milestone}; see docs/plan/implementation.md.",
        stage=command,
    )


@dataclass
class Context:
    """Invocation context resolved by the CLI wrapper."""

    context: str
    image_tag: str
    dagu_home: Path | None
    json_output: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
