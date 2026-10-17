"""Standalone ``mtm`` command-line interface.

Non-interactive by design (SYS-001): no GUI, no TUI, machine-readable JSON with
``--json``. Every started operation writes ``.mtm/state/last-operation.json``
when a bench exists, and returns one of the documented exit codes
(``docs/architecture/workflow-failures.md``): 0 ok, 10 test findings, 20 partial,
30 tool/workflow failure, 40 invalid config/input, 130 interrupted.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from . import MILESTONE, __version__
from .commands import (
    Context,
    cmd_doctor,
    cmd_init,
    cmd_install,
    cmd_not_implemented,
    cmd_scope_add,
    cmd_scope_list,
    cmd_scope_remove,
    cmd_status,
    cmd_workflow_run,
    cmd_workflow_view,
)
from .errors import EXIT_INVALID_INPUT, EXIT_TOOL_FAILURE, MtmError
from .paths import find_bench_root, write_last_operation

DEFAULT_CONTEXT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mtm",
        description="uTest Machinery — control CLI (PR 1 bootstrap and control).",
    )
    parser.add_argument("--version", action="version", version=f"mtm {__version__} ({MILESTONE})")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--bench", help="bench root (defaults to discovery from the current directory)")
    parser.add_argument("--context", default=str(DEFAULT_CONTEXT),
                        help="path to the uTest Machinery source tree (image, workflows)")
    parser.add_argument("--image-tag", default="1.0.0", help="generic image tag")
    parser.add_argument("--dagu-home", help="Dagu state directory (host-mounted)")

    sub = parser.add_subparsers(dest="_command", required=True)

    sub.add_parser("install", help="build/check the generic Docker image")

    sub.add_parser("doctor", help="diagnose Docker, image, bench and credentials")

    init = sub.add_parser("init", help="create a versioned bench in a directory")
    init.add_argument("directory", nargs="?", default=".", help="bench directory (default: .)")
    init.add_argument("--create-dir", action="store_true", help="create the directory if missing")
    init.add_argument("--force", action="store_true", help="reinitialise an existing bench")
    init.add_argument("--source", action="append", metavar="PATH", help="selected source root")
    init.add_argument("--spec", action="append", metavar="PATH", help="selected SDD/spec root")

    scope = sub.add_parser("scope", help="manage selected source/specification scope")
    scope_sub = scope.add_subparsers(dest="_scope_command", required=True)
    scope_sub.add_parser("list", help="list selected scope")
    add = scope_sub.add_parser("add", help="add a selected path")
    add.add_argument("path")
    add.add_argument("--kind", default="source", choices=["source", "spec", "include", "build-context"])
    add.add_argument("--id", help="explicit scope id")
    rm = scope_sub.add_parser("remove", help="remove a selected path (history preserved)")
    rm.add_argument("id", help="scope id or path")

    sub.add_parser("status", help="show bench state and the last operation")

    wf = sub.add_parser("workflow", help="inspect or run the headless workflow")
    wf_sub = wf.add_subparsers(dest="_workflow_command", required=True)
    view = wf_sub.add_parser("view", help="show the text DAG")
    view.add_argument("--dry", action="store_true", help="simulate the DAG without executing")
    run = wf_sub.add_parser("run", help="run the sample workflow headlessly")
    run.add_argument("--run-id", help="deterministic run id")

    for name, help_text in (
        ("plan", "view the verification plan (later milestone)"),
        ("generate", "generate tests and mocks (later milestone)"),
        ("verify", "run the frozen suite offline (later milestone)"),
        ("interpret", "interpret evidence with the LLM (later milestone)"),
        ("report", "open the results report (later milestone)"),
    ):
        sub.add_parser(name, help=help_text)

    return parser


DISPATCH: dict[str, Callable[[Any, Context], dict[str, Any]]] = {
    "install": cmd_install,
    "doctor": cmd_doctor,
    "init": cmd_init,
    "scope-list": cmd_scope_list,
    "scope-add": cmd_scope_add,
    "scope-remove": cmd_scope_remove,
    "status": cmd_status,
    "workflow-view": cmd_workflow_view,
    "workflow-run": cmd_workflow_run,
}


def _resolve_command(args: argparse.Namespace) -> str:
    if args._command == "scope":
        return f"scope-{args._scope_command}"
    if args._command == "workflow":
        return f"workflow-{args._workflow_command}"
    return args._command


def _print_human(record: dict[str, Any], command: str) -> None:
    status = record.get("status", "")
    print(f"[{command}] {status}")
    if record.get("message"):
        print(f"  {record['message']}")
    if record.get("action"):
        print(f"  next: {record['action']}")
    for check in record.get("checks", []):
        mark = "ok" if check["ok"] else ("!!" if check["level"] == "error" else "~")
        print(f"  [{mark}] {check['name']}: {check['message']}")
    for scope in record.get("scopes", []):
        print(f"  - {scope.get('id')} ({scope.get('kind')}) {scope.get('path')}")
    if record.get("scope"):
        scope = record["scope"]
        print(f"  added {scope.get('id')} ({scope.get('kind')}) {scope.get('path')}")
    for scope in record.get("removed", []):
        print(f"  removed {scope.get('id')} ({scope.get('path')}) — history preserved")
    if record.get("text_dag"):
        print(record["text_dag"])
    for key in ("bench_root", "image", "dag", "run_id", "pid", "raw_status"):
        if record.get(key) not in (None, "", 0):
            print(f"  {key}: {record[key]}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = _resolve_command(args)

    if args.bench:
        bench = Path(args.bench).expanduser().resolve()
        if not bench.is_dir():
            error = MtmError(code="CONFIG_INVALID", message=f"--bench is not a directory: {bench}",
                             action="Pass an existing bench directory.", stage="resolve-bench")
            return _fail(args, command, error)
        os.chdir(bench)

    ctx = Context(
        context=args.context,
        image_tag=args.image_tag,
        dagu_home=Path(args.dagu_home).expanduser().resolve() if args.dagu_home else None,
        json_output=args.json,
    )

    handler = DISPATCH.get(command)
    try:
        if handler is None:
            record = cmd_not_implemented(args, ctx)
        else:
            record = handler(args, ctx)
    except MtmError as err:
        return _fail(args, command, err)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("interrupted", file=sys.stderr)
        return 130

    exit_code = int(record.get("exit_code", 0))
    _persist(record)
    if args.json:
        json.dump(record, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        _print_human(record, command)
    return exit_code


def _fail(args: argparse.Namespace, command: str, err: MtmError) -> int:
    record = {
        "operation_id": f"op-{os.getpid()}",
        "stage": err.stage or command,
        "status": "FAILED",
        "category": err.category,
        "code": err.code,
        "message": err.message,
        "cause": err.cause,
        "action": err.action,
        "retryable": err.retryable,
        "log_path": err.log_path,
        "input_reference": err.input_reference,
        "milestone": MILESTONE,
        "exit_code": err.exit_code,
    }
    if err.tool_name:
        record["tool_name"] = err.tool_name
    _persist(record)
    if getattr(args, "json", False):
        json.dump(record, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print(f"error: {err.code}: {err.message}", file=sys.stderr)
        if err.cause:
            print(f"  cause: {err.cause}", file=sys.stderr)
        if err.action:
            print(f"  next:  {err.action}", file=sys.stderr)
    return err.exit_code


def _persist(record: dict[str, Any]) -> None:
    root = find_bench_root()
    if root is not None:
        try:
            write_last_operation(root, record)
        except OSError:
            pass  # never let journaling mask the real result


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
