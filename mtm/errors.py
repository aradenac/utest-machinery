"""Public error contract.

Single source of truth for CLI exit codes, error categories and stable error
codes, per ``docs/architecture/workflow-failures.md``. The CLI, the workflow
steps and the report all read the same record; no second taxonomy exists.

When several conditions occur, the *most severe operation-blocking* category
wins the process exit code, while every finding is preserved in the structured
record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --- exit codes (workflow-failures.md) -------------------------------------

EXIT_OK = 0
EXIT_TEST_FINDINGS = 10
EXIT_PARTIAL = 20
EXIT_TOOL_FAILURE = 30
EXIT_INVALID_INPUT = 40
EXIT_INTERRUPTED = 130

#: Ordering used to pick the most severe code when several apply. Higher wins.
SEVERITY = {
    EXIT_OK: 0,
    EXIT_TEST_FINDINGS: 1,
    EXIT_PARTIAL: 2,
    EXIT_TOOL_FAILURE: 3,
    EXIT_INVALID_INPUT: 4,
    EXIT_INTERRUPTED: 5,
}


class Category:
    """Coarse error families; distinct from the stable :class:`Code` values."""

    CONFIGURATION = "CONFIGURATION"
    INPUT_LIMITATION = "INPUT_LIMITATION"
    LLM = "LLM"
    TEST_FINDING = "TEST_FINDING"
    TOOL = "TOOL"
    WORKFLOW = "WORKFLOW"
    EVIDENCE = "EVIDENCE"


class Code:
    """Stable, user-facing error codes."""

    CONFIG_INVALID = "CONFIG_INVALID"
    INPUT_MISSING = "INPUT_MISSING"
    SCOPE_INVALID = "SCOPE_INVALID"
    SNAPSHOT_MISMATCH = "SNAPSHOT_MISMATCH"
    BENCH_MISSING = "BENCH_MISSING"
    BENCH_EXISTS = "BENCH_EXISTS"

    DOCKER_UNAVAILABLE = "DOCKER_UNAVAILABLE"
    IMAGE_MISSING = "IMAGE_MISSING"
    CREDENTIALS_MISSING = "CREDENTIALS_MISSING"

    LLM_AUTH = "LLM_AUTH"
    LLM_CONFIG = "LLM_CONFIG"
    LLM_NETWORK = "LLM_NETWORK"

    HOST_UNSUPPORTED = "HOST_UNSUPPORTED"
    SDD_GAP = "SDD_GAP"
    SDD_CONFLICT = "SDD_CONFLICT"

    TEST_ASSERTION = "TEST_ASSERTION"
    TEST_HARNESS_INVALID = "TEST_HARNESS_INVALID"
    TOOL_FAILED = "TOOL_FAILED"
    WORKFLOW_INTERRUPTED = "WORKFLOW_INTERRUPTED"
    OPERATION_RUNNING = "OPERATION_RUNNING"
    EVIDENCE_INVALID = "EVIDENCE_INVALID"
    OPERATION_FAILED = "OPERATION_FAILED"

    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


#: code -> (category, exit code at the operation level)
CONTRACT: dict[str, tuple[str, int]] = {
    Code.CONFIG_INVALID: (Category.CONFIGURATION, EXIT_INVALID_INPUT),
    Code.INPUT_MISSING: (Category.CONFIGURATION, EXIT_INVALID_INPUT),
    Code.SCOPE_INVALID: (Category.CONFIGURATION, EXIT_INVALID_INPUT),
    Code.SNAPSHOT_MISMATCH: (Category.CONFIGURATION, EXIT_INVALID_INPUT),
    Code.BENCH_MISSING: (Category.CONFIGURATION, EXIT_INVALID_INPUT),
    Code.BENCH_EXISTS: (Category.CONFIGURATION, EXIT_INVALID_INPUT),
    Code.DOCKER_UNAVAILABLE: (Category.TOOL, EXIT_TOOL_FAILURE),
    Code.IMAGE_MISSING: (Category.TOOL, EXIT_TOOL_FAILURE),
    Code.CREDENTIALS_MISSING: (Category.CONFIGURATION, EXIT_INVALID_INPUT),
    Code.LLM_AUTH: (Category.LLM, EXIT_INVALID_INPUT),
    Code.LLM_CONFIG: (Category.LLM, EXIT_INVALID_INPUT),
    Code.LLM_NETWORK: (Category.LLM, EXIT_TOOL_FAILURE),
    Code.HOST_UNSUPPORTED: (Category.INPUT_LIMITATION, EXIT_PARTIAL),
    Code.SDD_GAP: (Category.INPUT_LIMITATION, EXIT_PARTIAL),
    Code.SDD_CONFLICT: (Category.INPUT_LIMITATION, EXIT_PARTIAL),
    Code.TEST_ASSERTION: (Category.TEST_FINDING, EXIT_TEST_FINDINGS),
    Code.TEST_HARNESS_INVALID: (Category.TOOL, EXIT_TOOL_FAILURE),
    Code.TOOL_FAILED: (Category.TOOL, EXIT_TOOL_FAILURE),
    Code.WORKFLOW_INTERRUPTED: (Category.WORKFLOW, EXIT_INTERRUPTED),
    Code.OPERATION_RUNNING: (Category.WORKFLOW, EXIT_OK),
    Code.EVIDENCE_INVALID: (Category.EVIDENCE, EXIT_TOOL_FAILURE),
    Code.OPERATION_FAILED: (Category.WORKFLOW, EXIT_TOOL_FAILURE),
    Code.NOT_IMPLEMENTED: (Category.WORKFLOW, EXIT_INVALID_INPUT),
}


@dataclass
class MtmError(Exception):
    """A structured, user-actionable error.

    Carries exactly the fields of the minimum error object in
    ``docs/architecture/workflow-failures.md``. ``retryable`` marks only
    transient infrastructure/network conditions.
    """

    code: str
    message: str
    action: str = ""
    stage: str = ""
    scope_id: str = ""
    cause: str = ""
    retryable: bool = False
    log_path: str = ""
    input_reference: str = ""
    tool_name: str = ""
    tool_version: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def category(self) -> str:
        return CONTRACT.get(self.code, (Category.WORKFLOW, EXIT_TOOL_FAILURE))[0]

    @property
    def exit_code(self) -> int:
        return CONTRACT.get(self.code, (Category.WORKFLOW, EXIT_TOOL_FAILURE))[1]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.message}"


def most_severe(codes: list[int]) -> int:
    """Return the most severe exit code from a set of observed codes."""
    if not codes:
        return EXIT_OK
    return max(codes, key=lambda c: SEVERITY.get(c, 0))
