"""Developer-only stdlib smoke tests for the session-scoped Claude Code hooks."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / ".claude/hooks/develop-verified.py"


def invoke(event):
    return subprocess.run(
        [sys.executable, str(SCRIPT)], input=json.dumps(event), text=True,
        capture_output=True, check=False,
    )


class WorkflowHookSmoke(unittest.TestCase):
    def test_subagent_empty_summary_gets_one_bounded_retry(self):
        event = {"hook_event_name": "SubagentStop", "last_assistant_message": "Done"}
        self.assertEqual(invoke(event).returncode, 2)
        event["stop_hook_active"] = True
        self.assertEqual(invoke(event).returncode, 0)

    def test_subagent_substantive_summary_passes(self):
        self.assertEqual(invoke({"hook_event_name": "SubagentStop",
            "last_assistant_message": "VERDICT: READY; fingerprint abc123, no mandatory findings."}).returncode, 0)

    def test_native_task_default_is_unconstrained(self):
        self.assertEqual(invoke({"hook_event_name": "TaskCompleted",
            "task_description": "Implement a coherent slice"}).returncode, 0)

    def test_declared_task_proof_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "evidence.txt"
            event = {"hook_event_name": "TaskCompleted", "cwd": tmp,
                     "task_description": "Proof-file: evidence.txt"}
            self.assertEqual(invoke(event).returncode, 2)
            file.write_text("proof", encoding="utf-8")
            self.assertEqual(invoke(event).returncode, 0)
            event["task_description"] = "Proof-file: ../not-project.txt"
            self.assertEqual(invoke(event).returncode, 2)

    def test_final_ready_requires_no_background_job(self):
        event = {"hook_event_name": "Stop", "last_assistant_message": "PUBLISH_READY",
                 "background_tasks": [{"id": "t1", "status": "running"}]}
        self.assertEqual(invoke(event).returncode, 2)
        event["background_tasks"] = []
        self.assertEqual(invoke(event).returncode, 0)
        event["background_tasks"] = [{"id": "done", "status": "completed"},
                                     {"id": "bad", "status": "failed"}]
        self.assertEqual(invoke(event).returncode, 0)
        event["background_tasks"] = [{"id": "t2", "status": "running"}]
        event["last_assistant_message"] = "PUBLISH_DRAFT"
        self.assertEqual(invoke(event).returncode, 0)
        event["last_assistant_message"] = "Do not claim PUBLISH_READY yet."
        self.assertEqual(invoke(event).returncode, 0)
        event["stop_hook_active"] = True
        event["last_assistant_message"] = "PUBLISH_READY"
        self.assertEqual(invoke(event).returncode, 0)

    def test_idle_without_assigned_mission_is_normal(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = {"hook_event_name": "TeammateIdle", "cwd": tmp,
                 "team_name": "my-team", "teammate_name": "developer"}
            self.assertEqual(invoke(e).returncode, 0)

    def test_idle_missing_report_gets_exactly_one_nudge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missions = root / ".claude/.runtime-reports/my-team/missions"
            missions.mkdir(parents=True)
            receipt = missions / "developer.json"
            report = ".claude/.runtime-reports/my-team/developer-report.md"
            receipt.write_text(json.dumps({
                "active": True, "mission_id": "m01", "fingerprint": "sha-a",
                "report": report
            }), encoding="utf-8")
            e = {"hook_event_name": "TeammateIdle", "cwd": tmp,
                 "team_name": "my-team", "teammate_name": "developer"}
            self.assertEqual(invoke(e).returncode, 2)
            self.assertEqual(invoke(e).returncode, 0)  # never an idle-hook loop
            target = root / report
            target.write_text("MISSION_ID: m01\nFINGERPRINT: stale\nSTATUS: COMPLETED\n",
                              encoding="utf-8")
            self.assertEqual(invoke(e).returncode, 0)  # no additional nudge
            receipt.write_text(json.dumps({
                "active": True, "mission_id": "m02", "fingerprint": "sha-b",
                "report": report
            }), encoding="utf-8")
            self.assertEqual(invoke(e).returncode, 2)  # new mission, new nudge

    def test_idle_matching_report_and_inactive_assignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missions = root / ".claude/.runtime-reports/my-team/missions"
            missions.mkdir(parents=True)
            report = root / ".claude/.runtime-reports/my-team/done.md"
            report.write_text("MISSION_ID: m01\nFINGERPRINT: sha-a\n"
                              "STATUS: COMPLETED\nProof: observed test exit 0\n",
                              encoding="utf-8")
            receipt = missions / "developer.json"
            info = {"active": True, "mission_id": "m01",
                    "fingerprint": "sha-a", "report":
                    ".claude/.runtime-reports/my-team/done.md"}
            receipt.write_text(json.dumps(info), encoding="utf-8")
            e = {"hook_event_name": "TeammateIdle", "cwd": tmp,
                 "team_name": "my-team", "teammate_name": "developer"}
            self.assertEqual(invoke(e).returncode, 0)
            report.write_text("MISSION_ID: wrong\nFINGERPRINT: sha-a\n"
                              "STATUS: COMPLETED\n", encoding="utf-8")
            self.assertEqual(invoke(e).returncode, 2)
            info["active"] = False
            receipt.write_text(json.dumps(info), encoding="utf-8")
            self.assertEqual(invoke(e).returncode, 0)

    def test_idle_ignores_unsafe_mission_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missions = root / ".claude/.runtime-reports/my-team/missions"
            missions.mkdir(parents=True)
            (missions / "developer.json").write_text(json.dumps({
                "active": True, "mission_id": "m01",
                "fingerprint": "sha-a", "report": "../../unsafe.md"
            }), encoding="utf-8")
            e = {"hook_event_name": "TeammateIdle", "cwd": tmp,
                 "team_name": "my-team", "teammate_name": "developer"}
            self.assertEqual(invoke(e).returncode, 0)  # malformed receipt cannot trap worker


if __name__ == "__main__":
    unittest.main()
