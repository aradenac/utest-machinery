#!/usr/bin/env python3
"""Small deterministic hooks for coordinator-owned delivery checks.

No LLM/tools/tests, daemon, periodic polling, persistent tracker or semantic approval.
All receipts are optional and ignored under .claude/.runtime-reports/.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sys


SAFE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
FINAL_STATES = {"COMPLETED", "FAILED", "BLOCKED", "NOT_VERIFIED"}


def _within(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _mission_receipt(event: dict, root: Path):
    """Only active, explicitly coordinated Agent Teams missions are inspected."""
    team = event.get("team_name") or event.get("session_id")
    teammate = event.get("teammate_name")
    if not all(isinstance(x, str) and SAFE.fullmatch(x) for x in (team, teammate)):
        return None
    file = root / ".claude/.runtime-reports" / team / "missions" / (teammate + ".json")
    if not file.is_file():
        return None
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("active") is not True:
        return None
    mid, fingerprint, rel = (data.get(k) for k in ("mission_id", "fingerprint", "report"))
    if not (isinstance(mid, str) and mid and isinstance(fingerprint, str)
            and fingerprint and isinstance(rel, str) and rel):
        return None
    report = (root / rel).resolve()
    scratch = (root / ".claude/.runtime-reports").resolve()
    if not _within(scratch, report) or report == scratch:
        return None
    return file, mid, fingerprint, report


def _report_matches(report: Path, mid: str, fingerprint: str) -> bool:
    if not report.is_file():
        return False
    try:
        # Reports are short text artifacts. Never trust a filename alone.
        lines = report.read_text(encoding="utf-8")[:4096].splitlines()
    except (OSError, UnicodeError):
        return False
    fields = {}
    for line in lines:
        if ": " in line:
            k, v = line.split(": ", 1)
            if k in {"MISSION_ID", "FINGERPRINT", "STATUS"}:
                fields[k] = v.strip()
    return (fields.get("MISSION_ID") == mid
            and fields.get("FINGERPRINT") == fingerprint
            and fields.get("STATUS") in FINAL_STATES)


def decide(event: dict, root: Path) -> tuple[int, str]:
    name = event.get("hook_event_name", "")
    if name == "SubagentStop":
        if event.get("stop_hook_active"):
            return 0, ""
        message = event.get("last_assistant_message") or ""
        if len(message.strip()) < 40:
            return 2, ("Return a short final specialist summary: MISSION_ID, "
                       "fingerprint, status and decisive evidence or NOT_VERIFIED. "
                       "Do not run further probes.")
    elif name == "TeammateIdle":
        receipt = _mission_receipt(event, root)
        if receipt is None:
            return 0, ""  # Idle can be normal; absent hooks/receipts are not failure.
        manifest, mid, fingerprint, report = receipt
        if _report_matches(report, mid, fingerprint):
            return 0, ""
        # One deterministic nudge per exact mission, even if TeammateIdle repeats.
        digest = hashlib.sha256((mid + "\0" + fingerprint).encode()).hexdigest()[:16]
        nudges = manifest.parent.parent / "nudges"
        nudges.mkdir(parents=True, exist_ok=True)
        marker = nudges / (manifest.stem + "-" + digest + ".nudged")
        try:
            with marker.open("x", encoding="utf-8") as f:
                f.write("nudge issued; coordinator owns retry/replacement\n")
        except FileExistsError:
            return 0, ""  # Do not trap a teammate in an idle-hook loop.
        return 2, ("Mission " + mid + " has no valid completion report for "
                   + fingerprint + ". Send the coordinator an immediate "
                   "COMPLETED/FAILED/BLOCKED/NOT_VERIFIED report at the assigned "
                   "path, with MISSION_ID/FINGERPRINT/STATUS headers. "
                   "Do not start unrelated work. One hook reminder only.")
    elif name == "TaskCompleted":
        desc = event.get("task_description") or ""
        match = re.search(r"(?m)^Proof-file: ([^\r\n]+)$", desc)
        if match:
            rel = Path(match.group(1).strip())
            target = (root / rel).resolve()
            if (rel.is_absolute() or not rel.parts
                    or not _within(root.resolve(), target) or not target.is_file()):
                return 2, ("The Task's declared Proof-file is missing or outside "
                           "this project. Correct the proof or the task.")
    elif name == "Stop":
        if event.get("stop_hook_active"):
            return 0, ""
        msg = event.get("last_assistant_message") or ""
        ready = re.search(r"(?im)^\s*(?:(?:STATUS|DECISION|VERDICT):\s*)?PUBLISH_READY\b", msg)
        active = [t for t in (event.get("background_tasks") or [])
                  if isinstance(t, dict) and str(t.get("status", "")).lower()
                  not in {"completed", "finished", "done", "success", "failed",
                          "error", "cancelled", "canceled", "stopped"}]
        if ready and active:
            return 2, ("PUBLISH_READY is claimed with background jobs still active. "
                       "Coordinator: join/cancel, inspect reports and reassess; "
                       "otherwise declare PUBLISH_DRAFT or HOLD.")
    return 0, ""


def main() -> int:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            return 0
        root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or ".").resolve()
        code, reason = decide(event, root)
    except (OSError, TypeError, ValueError):
        return 0  # A malformed/unsupported event must not wedge a session.
    if code:
        print(reason, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
