#!/usr/bin/env python3
"""Lightweight skill hooks: mechanical delivery checks, never semantic approval.

No toolchain, test or model invocation. Input is Claude Code's stdin JSON.
"""
import json
import os
from pathlib import Path
import re
import sys


def decide(event: dict, root: Path) -> tuple[int, str]:
    name = event.get("hook_event_name", "")
    if name == "SubagentStop":
        if event.get("stop_hook_active"):
            return 0, ""
        # Handback reports are delivered by the tool, not last_assistant_message;
        # our reviewers also return a short substantive closing summary.
        message = event.get("last_assistant_message") or ""
        if len(message.strip()) < 40:
            return 2, ("Return a short final reviewer summary: status, reviewed "
                       "checkpoint/fingerprint, and decisive findings or explicit "
                       "NOT_VERIFIED. Do not run further probes.")
    elif name == "TaskCompleted":
        # This check is opt-in; ordinary native Tasks need no special fields.
        desc = event.get("task_description") or ""
        match = re.search(r"(?m)^Proof-file: ([^\r\n]+)$", desc)
        if match:
            rel = Path(match.group(1).strip())
            target = (root / rel).resolve()
            if (rel.is_absolute() or not rel.parts or
                    not target.is_relative_to(root.resolve()) or
                    not target.is_file()):
                return 2, ("The Task's explicitly declared Proof-file is missing "
                           "or outside this project. Fix it or update the Task.")
    elif name == "Stop":
        if event.get("stop_hook_active"):
            return 0, ""
        # A mere discussion of PUBLISH_READY must not trigger this check.
        message = event.get("last_assistant_message") or ""
        claimed = re.search(r"(?im)^\s*(?:(?:STATUS|DECISION|VERDICT):\s*)?PUBLISH_READY\b", message)
        if claimed and event.get("background_tasks"):
            return 2, ("PUBLISH_READY is claimed while background tasks are still "
                       "active. Collect or explicitly cancel them and reassess; "
                       "otherwise report PUBLISH_DRAFT or HOLD.")
    return 0, ""


def main() -> int:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            return 0
        root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or ".").resolve()
        code, reason = decide(event, root)
    except (ValueError, TypeError, OSError):
        return 0  # Malformed hook input must not wedge an interactive session.
    if code:
        print(reason, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
