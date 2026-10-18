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
        event["background_tasks"] = [{"id": "t2", "status": "running"}]
        event["last_assistant_message"] = "PUBLISH_DRAFT"
        self.assertEqual(invoke(event).returncode, 0)
        event["last_assistant_message"] = "Do not claim PUBLISH_READY yet."
        self.assertEqual(invoke(event).returncode, 0)
        event["stop_hook_active"] = True
        event["last_assistant_message"] = "PUBLISH_READY"
        self.assertEqual(invoke(event).returncode, 0)


if __name__ == "__main__":
    unittest.main()
