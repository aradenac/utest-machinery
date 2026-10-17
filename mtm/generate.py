"""The ``generate`` operation: inventory → feasibility → plan → suite → smoke.

This module ties the PR 2 stages together for the CLI. It is deliberately a thin
orchestrator: every stage is a separate, testable module, and the whole pipeline
is reproducible offline with the mock provider (no token) as well as with the
in-image Claude Code route.

Stages (``docs/architecture/architecture.md`` logical workflow):

1. ``inventory``   — deterministic scan of the selected inputs (no LLM).
2. ``feasibility`` — READY/ADAPTED/BLOCKED from a real host compile (no LLM).
3. ``plan``        — LLM proposes; code grounds it against the inventory.
4. ``generate``    — deterministic Unity tests + private mocks from the plan.
5. ``smoke``       — compile/link/run the generated suite on the host.

Result statuses: ``SUCCEEDED`` (suite builds and runs), ``FINDINGS`` (it builds
and runs but a generated assertion contradicts the SDD - a finding, exit 10),
``BLOCKED`` (nothing feasible), or a raised :class:`MtmError` for a config/tool
failure. Nothing here modifies the selected inputs.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ccompile import HostCompiler
from .errors import Code, MtmError
from .feasibility import assess_inventory, summarize
from .gen import generate_suite
from .inventory import build_inventory
from .llm import LLMProvider, MockProvider
from .plan import Plan, build_prompt, parse_plan
from .paths import write_json_atomic

#: Artefacts written under ``<bench>/state/generate`` (host-side, reviewable).
INVENTORY_FILE = "inventory.json"
PLAN_FILE = "plan.json"
FEASIBILITY_FILE = "feasibility.json"
SUITE_FILE = "suite.json"
SMOKE_FILE = "smoke.json"


@dataclass
class GenerateResult:
    status: str
    exit_code: int
    inventory: Path
    feasibility: list[dict[str, Any]] = field(default_factory=list)
    plan: Path | None = None
    suite: dict[str, Any] = field(default_factory=dict)
    smoke: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    findings: list[dict[str, Any]] = field(default_factory=list)
    model: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False


def _state_dir(bench_root: Path) -> Path:
    path = bench_root / ".mtm" / "state" / "generate"
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_generate(
    bench_root: Path,
    selected: list[dict[str, Any]],
    *,
    provider: LLMProvider | None = None,
    compiler: HostCompiler | None = None,
    dry_run: bool = False,
) -> GenerateResult:
    """Run the full generation pipeline for a bench."""
    state = _state_dir(bench_root)
    inventory = build_inventory(bench_root, selected)
    inventory_path = write_json_atomic(state / INVENTORY_FILE, inventory.to_dict())

    compiler = compiler or HostCompiler()
    verdicts = assess_inventory(bench_root, inventory, compiler)
    feasibility_path = write_json_atomic(
        state / FEASIBILITY_FILE,
        {"units": [v.to_dict() for v in verdicts], "counts": summarize(verdicts)},
    )

    counts = summarize(verdicts)
    if counts.get("BLOCKED", 0) and not (counts.get("READY", 0) or counts.get("ADAPTED", 0)):
        return GenerateResult(
            status="BLOCKED", exit_code=20, inventory=inventory_path,
            feasibility=[v.to_dict() for v in verdicts], counts=counts,
        )

    if dry_run:
        return GenerateResult(
            status="PLANNED", exit_code=0, inventory=inventory_path,
            feasibility=[v.to_dict() for v in verdicts], counts=counts, dry_run=True,
        )

    provider = provider or MockProvider()
    prompt = build_prompt(inventory)
    response = provider.complete(prompt)
    plan: Plan = parse_plan(response, inventory, provider.identity)
    plan_path = write_json_atomic(state / PLAN_FILE, plan.to_dict())

    if not compiler.available:
        raise MtmError(
            code=Code.TOOL_FAILED,
            message="No host C compiler is available to build the generated suite.",
            action="Install a C compiler (the in-image toolchain arrives in PR 3).",
            stage="generate-smoke",
            tool_name="cc",
        )

    generated = generate_suite(bench_root, plan, inventory)
    suite_manifest = generated.to_dict()
    suite_path = write_json_atomic(state / SUITE_FILE, suite_manifest)

    smoke = _compile_and_run(bench_root, plan, inventory, compiler, generated)
    smoke_path = write_json_atomic(state / SMOKE_FILE, smoke)

    findings = smoke.get("findings", [])
    limitations = suite_manifest.get("limitations", [])
    if not smoke.get("built"):
        status, exit_code = "TOOL_FAILURE", 30
    elif findings:
        status, exit_code = "FINDINGS", 10
    elif limitations:
        # A planned scenario that could not be materialised faithfully is a
        # coverage shortfall, not a clean success: the plan asked for more than
        # the suite delivered, and the caller must see that.
        status, exit_code = "FINDINGS", 10
    else:
        status, exit_code = "SUCCEEDED", 0

    return GenerateResult(
        status=status, exit_code=exit_code, inventory=inventory_path,
        feasibility=[v.to_dict() for v in verdicts], plan=plan_path,
        suite=suite_manifest, smoke=smoke, counts=counts, findings=findings,
        model=plan.model,
    )


def _compile_and_run(bench_root: Path, plan: Plan, inventory, compiler: HostCompiler,
                     generated) -> dict[str, Any]:
    """Compile the original units, generated tests, mocks and Unity; run it."""
    unity_dir = Path(__file__).resolve().parent / "vendor" / "unity"
    build = Path(bench_root) / ".mtm" / "state" / "generate" / "build"
    if build.exists():
        shutil.rmtree(build)
    build.mkdir(parents=True, exist_ok=True)

    # Sources: original units (paths preserved), generated tests/mocks, the Unity
    # framework and its runner. Inputs are never copied over or edited.
    sources: list[Path] = []
    include_dirs: list[Path] = []
    for unit in inventory.units:
        source = bench_root / unit.path
        if source.is_file():
            sources.append(source)
            include_dirs.append(source.parent)
    for entry in inventory.selected:
        candidate = bench_root / str(entry.get("path", ""))
        if candidate.is_dir():
            include_dirs.append(candidate)
        elif candidate.suffix.lower() == ".h" and candidate.parent.is_dir():
            include_dirs.append(candidate.parent)

    tests_dir = bench_root / "tests"
    mocks_dir = bench_root / "mocks"
    shims_dir = bench_root / "shims"
    sources += sorted(p for p in tests_dir.glob("*.c"))
    sources += sorted(p for p in mocks_dir.glob("*.c"))
    sources.append(unity_dir / "unity.c")

    include_dirs += [tests_dir, mocks_dir, unity_dir]
    if shims_dir.is_dir():
        # Shim headers take precedence for the missing-include case, so they come
        # first; they declare only the documented interface.
        include_dirs = [shims_dir, *include_dirs]
    # De-duplicate include dirs, preserving order.
    seen: set[str] = set()
    unique_includes: list[Path] = []
    for d in include_dirs:
        key = str(d.resolve())
        if key not in seen and d.is_dir():
            seen.add(key)
            unique_includes.append(d)

    binary = build / "suite"
    link = compiler.link(sources, binary, unique_includes)
    if not link.ok:
        return {
            "built": False, "ran": False,
            "compile_argv": link.argv, "diagnostics": link.diagnostics[-4000:],
            "findings": [],
        }

    run = compiler.run(binary, cwd=build)
    executed = _executed_tests(run.stdout + run.stderr)
    total, passed, failed = _unity_summary(run.stdout)
    findings: list[dict[str, Any]] = []
    if run.returncode != 0:
        findings = _failing_scenarios(run.stdout, run.stderr, plan)
    return {
        "built": True, "ran": True, "exit_code": run.returncode,
        "stdout": run.stdout[-4000:], "stderr": run.stderr[-2000:],
        "unity": {"total": total, "passed": passed, "failed": failed},
        "executed": executed,
        "findings": findings,
    }


def _unity_summary(stdout: str) -> tuple[int, int, int]:
    import re

    # Unity prints "-----------------------\nN Tests M Failures K Ignored".
    m = re.search(r"(\d+)\s+Tests\s+(\d+)\s+Failures\s+(\d+)\s+Ignored", stdout)
    if not m:
        return 0, 0, 0
    total = int(m.group(1))
    failures = int(m.group(2))
    return total, total - failures, failures


def _parse_failures(stdout: str) -> list[tuple[str, str]]:
    """Return ``(test_function, short_reason)`` for each Unity assertion failure.

    Unity prints ``<path>:<line>:<test>:FAIL: <reason>`` (the path itself contains
    ``:``), so the marker is split on the literal ``:FAIL`` token rather than on
    the last colon.
    """
    import re

    failures: list[tuple[str, str]] = []
    for line in stdout.splitlines():
        if ":FAIL" not in line or ":PASS" in line:
            continue
        head, _, tail = line.partition(":FAIL")
        reason = tail.lstrip(": ").strip()
        marker = re.search(r"([A-Za-z_]\w*)\s*$", head)
        if not marker:
            continue
        failures.append((marker.group(1), reason))
    return failures


def _executed_tests(stdout: str) -> list[str]:
    import re

    names: list[str] = []
    for line in stdout.splitlines():
        marker = re.search(r"(\w+?):(?:PASS|FAIL|IGNORE)", line)
        if marker:
            name = marker.group(1).split(":")[-1]
            if name not in names:
                names.append(name)
    return names


def _failing_scenarios(stdout: str, stderr: str, plan: Plan) -> list[dict[str, Any]]:
    """Map Unity failures to the plan scenarios that produced them.

    A generated test that fails means the **implementation contradicts the
    SDD-grounded oracle** — a finding (``IMPLEMENTATION_DEFECT``), preserved as
    evidence, never repaired by editing the oracle (R04).
    """
    failing: list[dict[str, Any]] = []
    for name, reason in _parse_failures(stdout):
        scenario_id = _scenario_for_test(name)
        failing.append({
            "test": name,
            "category": "IMPLEMENTATION_DEFECT",
            "scenario_id": scenario_id,
            "expected_behavior": _oracle_statement(plan, scenario_id),
            "observed": reason,
        })
    if not failing and stderr.strip():
        failing.append({"test": "<runner>", "category": "TOOL_FAILURE",
                        "detail": stderr.strip()[-500:]})
    return failing


def _scenario_for_test(test_name: str) -> str:
    # test_<unit>_<SID> -> <SID>
    parts = test_name.split("_")
    return parts[-1] if parts else test_name


def _oracle_statement(plan: Plan, scenario_id: str) -> str:
    for scenario in plan.scenarios:
        if scenario.id == scenario_id:
            return scenario.oracle.statement
    return ""
