"""The ``generate`` operation: inventory → feasibility → plan → suite → smoke.

This module ties the PR 2 stages together for the CLI. It is deliberately a thin
orchestrator: every stage is a separate, testable module, and the whole pipeline
is reproducible offline with the mock provider (no token) as well as with the
in-image Claude Code route.

Stages (``docs/architecture/architecture.md`` logical workflow):

1. ``inventory``   — deterministic scan of the selected inputs (no LLM).
2. ``feasibility`` — READY/ADAPTED/BLOCKED from a real **host** compile (no LLM).
3. ``plan``        — LLM proposes; code grounds it against the inventory.
4. ``suite``       — deterministic Ceedling/CMock project from the plan.
5. ``smoke``       — Ceedling compiles, links and executes the suite **in the
   product image** (the C toolchain and Ruby live there, not on the host).

Result statuses: ``SUCCEEDED`` (the suite builds, runs and every planned
scenario materialised), ``FINDINGS`` (it ran, but a generated assertion
contradicts the SDD — a finding, exit 10 — or a planned scenario could not be
materialised, which is a coverage shortfall), ``INCOMPLETE`` (no scenario could
be executed to a verdict, exit 20), ``BLOCKED`` (nothing feasible, exit 20), or a
raised :class:`~mtm.errors.MtmError` for a config/authorization failure (raised by
the provider/runner, not here). A **tool fault**
(timeout, crash, or a run with no Unity summary) is always ``TOOL_FAILURE``
(exit 30) and is never promoted to an implementation defect.

Nothing here modifies the selected inputs: the generated project lives under
``<bench>/.mtm/gen`` and is rebuilt on every run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import ctypes_clang
from .ccompile import HostCompiler
from .ceedling import CeedlingRun, docker_fault_reason, run_ceedling
from .errors import EXIT_TOOL_FAILURE
from .feasibility import BLOCKED, assess_inventory, summarize
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
    #: Which provider actually ran, and how it was selected (never a secret).
    provider: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False


def _state_dir(bench_root: Path) -> Path:
    path = bench_root / ".mtm" / "state" / "generate"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _clear_derived(state: Path) -> None:
    """Remove the derived artefacts a fresh run will rewrite.

    ``inventory.json``/``feasibility.json`` are re-written unconditionally at the
    start of a run, but ``plan.json``/``suite.json``/``smoke.json`` are not on the
    early-exit paths. Deleting them here means those paths leave no stale plan
    behind, so a later ``mtm plan`` never presents a previous input set's plan as
    the current one.
    """
    for name in (PLAN_FILE, SUITE_FILE, SMOKE_FILE):
        target = state / name
        try:
            target.unlink()
        except FileNotFoundError:
            pass


def run_generate(
    bench_root: Path,
    selected: list[dict[str, Any]],
    *,
    provider: LLMProvider | None = None,
    compiler: HostCompiler | None = None,
    dry_run: bool = False,
    image: str = "utest-machinery/mtm:1.0.0",
    runner=run_ceedling,
    resolve=None,
    build_context: dict[str, Any] | None = None,
) -> GenerateResult:
    """Run the full generation pipeline for a bench.

    *runner* is the Ceedling executor (injectable so tests can drive the
    classification logic without Docker); it defaults to the real in-image run.
    *resolve* is the libclang adapter seam (injectable for the same reason);
    it defaults to :func:`mtm.ctypes_clang.resolve` running in *image*.
    """
    state = _state_dir(bench_root)
    # Derived artefacts are cleared before every run, so a run that stops early
    # (BLOCKED, dry-run) cannot leave a previous input set's plan/suite/smoke
    # looking current for the new inputs. `plan`/`status` read these files without
    # a snapshot check, so a stale one would be presented as authoritative.
    _clear_derived(state)
    inventory = build_inventory(bench_root, selected)
    inventory_path = write_json_atomic(state / INVENTORY_FILE, inventory.to_dict())

    compiler = compiler or HostCompiler()
    verdicts = assess_inventory(bench_root, inventory, compiler)
    feasibility_path = write_json_atomic(
        state / FEASIBILITY_FILE,
        {"units": [v.to_dict() for v in verdicts], "counts": summarize(verdicts)},
    )

    counts = summarize(verdicts)
    feasibility = [v.to_dict() for v in verdicts]
    if counts.get(BLOCKED, 0) and not (counts.get("READY", 0) or counts.get("ADAPTED", 0)):
        return GenerateResult(
            status="BLOCKED", exit_code=20, inventory=inventory_path,
            feasibility=feasibility, counts=counts,
        )

    if dry_run:
        return GenerateResult(
            status="PLANNED", exit_code=0, inventory=inventory_path,
            feasibility=feasibility, counts=counts, dry_run=True,
            provider={"name": "none", "mode": "dry-run", "called": False},
        )

    provider = provider or MockProvider()
    prompt = build_prompt(inventory)
    response = provider.complete(prompt)
    plan: Plan = parse_plan(response, inventory, provider.identity)
    from .build_context import context_identity
    plan.build_context_hash = context_identity(bench_root, build_context)
    plan_path = write_json_atomic(state / PLAN_FILE, plan.to_dict())

    provider_record = _provider_record(provider)

    # B3: a BLOCKED unit is excluded from the executable project so it cannot
    # break the build of the independent, feasible units. It stays visible in
    # the feasibility report and the denominator (R03).
    excluded = {v.unit_id for v in verdicts if v.status == BLOCKED}
    # The image ref is threaded through so the libclang type/declaration adapter
    # resolves the selected C context (the semantic authority, owner decision
    # 0017). A resolution tool fault is a run-level tool fault, not a SUT defect.
    generated = generate_suite(bench_root, plan, inventory, excluded_units=excluded,
                               image=image, resolve=resolve,
                               build_context=build_context)
    suite_manifest = generated.to_dict()
    suite_path = write_json_atomic(state / SUITE_FILE, suite_manifest)

    # Compute a fingerprint of the generated suite so that offline verify can
    # authenticate the replay target against the plan (Invariant 1).
    from .qualify import execution_image_identity, frozen_suite_hash
    plan.suite_hash = frozen_suite_hash(bench_root)
    plan.suite_hash["image"] = execution_image_identity(image)
    # Rewrite the plan now that it carries the suite fingerprint.
    plan_path = write_json_atomic(state / PLAN_FILE, plan.to_dict())

    types_status = suite_manifest.get("types", {}).get("status")
    if types_status == ctypes_clang.TOOL_FAILED:
        # The C type resolver (libclang/docker/binding) could not be used at all:
        # no declaration can be typed faithfully, so this is a tool fault of the
        # *operation* — exit 30, never a SUT defect or a functional result (I4).
        return GenerateResult(
            status="TOOL_FAILURE", exit_code=EXIT_TOOL_FAILURE, inventory=inventory_path,
            feasibility=feasibility, plan=plan_path, suite=suite_manifest,
            smoke={"built": False, "ran": False,
                   "reason": suite_manifest.get("types", {}).get("message")
                   or "the C type resolver could not be used"},
            counts=counts, model=plan.model, provider=provider_record,
        )

    limitations = suite_manifest.get("limitations", [])
    if not generated.test_functions:
        # No scenario could be materialised faithfully. Running an empty project
        # would report a vacuous success, so stop here with the shortfall visible.
        return GenerateResult(
            status="INCOMPLETE", exit_code=20, inventory=inventory_path,
            feasibility=feasibility, plan=plan_path, suite=suite_manifest,
            smoke={"built": False, "ran": False,
                   "reason": "no planned scenario could be materialised"},
            counts=counts, model=plan.model, provider=provider_record,
        )

    smoke = _run_and_classify(bench_root, plan, generated, image=image, runner=runner)
    smoke_path = write_json_atomic(state / SMOKE_FILE, smoke)

    findings = smoke.get("findings", [])
    blocked = counts.get(BLOCKED, 0)
    partial = bool(limitations) or blocked > 0
    if not smoke.get("ran"):
        # A tool fault (timeout, crash, or a run without a Unity summary) is never
        # a C implementation defect: report it as such, with its own evidence.
        status, exit_code = "TOOL_FAILURE", 30
    elif partial:
        # A blocked unit or a scenario that could not be materialised faithfully
        # means verification is *incomplete*, not that the code passed or failed:
        # exit 20 (partial), with any findings preserved alongside it
        # (``workflow-failures.md``). Never report a clean SUCCEEDED when part of
        # the selected scope went unverified.
        status, exit_code = "PARTIAL", 20
    elif findings:
        status, exit_code = "FINDINGS", 10
    else:
        status, exit_code = "SUCCEEDED", 0

    return GenerateResult(
        status=status, exit_code=exit_code, inventory=inventory_path,
        feasibility=feasibility, plan=plan_path,
        suite=suite_manifest, smoke=smoke, counts=counts, findings=findings,
        model=plan.model, provider=provider_record,
    )


def _provider_record(provider: LLMProvider) -> dict[str, Any]:
    identity = provider.identity
    mode = {
        "mock": "recorded fixture (offline, token-free)",
        "claude-code": "in-image Claude Code + CCR (live model)",
    }.get(identity.provider, identity.provider)
    return {"name": identity.provider, "model": identity.model, "mode": mode,
            "prompt_version": identity.prompt_version, "called": True}


def _run_and_classify(bench_root: Path, plan: Plan, generated, *,
                      image: str, runner) -> dict[str, Any]:
    """Execute the generated project and classify the outcome (B2).

    The classification uses **two independent signals**: the runner's exit code
    and a parseable Unity summary. Only a Unity summary proves the suite ran;
    without one the run is a tool fault, whatever the exit code.
    """
    run: CeedlingRun = runner(bench_root, image_ref=image)
    smoke: dict[str, Any] = {
        "built": run.ran,
        "ran": run.ran,
        "exit_code": run.returncode,
        "timed_out": run.timed_out,
        "command": " ".join(run.argv),
        "stdout": run.stdout[-6000:],
        "stderr": run.stderr[-2000:],
        "unity": {"total": run.total, "passed": run.passed, "failed": run.failed,
                  "ignored": run.ignored, "executed": run.executed},
        "findings": [],
    }
    if not run.ran:
        # No Unity summary: the suite did not produce results. Never a defect.
        smoke["reason"] = _tool_fault_reason(run)
        smoke["diagnostics"] = run.diagnostics[-4000:]
        return smoke

    # An abnormal termination outranks *any* assertion also observed in the same
    # run: a tool fault must never be masked by a co-observed finding
    # (``workflow-failures.md``: surface the most severe operation-blocking
    # category and preserve all findings). The assertion findings are computed
    # first so they are carried alongside. ``docker_fault_reason`` carries the
    # *Docker* statuses (a killed container is 128 + signal, e.g. 137/143; the
    # reserved 125/126/127 are daemon/entrypoint errors) which the negative
    # parent-shell status never surfaces, so a container killed mid-run is not
    # mistaken for a plain assertion failure.
    smoke["findings"] = _findings(run, plan)

    docker_fault = docker_fault_reason(run.returncode)
    abnormal = (run.timed_out or run.returncode < 0
                or docker_fault is not None
                or (run.returncode != 0 and run.failed == 0))
    if abnormal:
        # The suite produced a summary, so the fault is in the *run*, not in the
        # build: state it in those terms rather than the no-summary wording.
        kind = ("the test run timed out" if run.timed_out
                else docker_fault if docker_fault is not None
                else f"the test runner was killed by signal {-run.returncode}"
                if run.returncode < 0
                else f"the runner exited {run.returncode} although Unity reported no "
                     "failures")
        smoke["ran"] = False
        smoke["reason"] = (
            f"{kind}; the suite did not complete normally"
            + (f" — {len(smoke['findings'])} assertion finding(s) were also observed "
               "and are preserved alongside this tool failure" if smoke["findings"]
               else "")
        )
        smoke["diagnostics"] = run.diagnostics[-4000:]
        return smoke

    # The generated manifest is the authority on how many tests must run. An
    # *ignored* test is enumerated by Unity but its body does not run, so it is
    # not executed coverage: require every generated test to have executed and
    # none to be ignored, or the run does not cover the plan.
    expected = len(generated.test_functions)
    smoke["expected_tests"] = expected
    if run.executed < expected:
        smoke["ran"] = False
        smoke["reason"] = (f"only {run.executed} of the {expected} generated test(s) "
                           f"executed ({run.ignored} ignored); the run does not cover "
                           "the plan")
        smoke["diagnostics"] = run.diagnostics[-4000:]
        return smoke
    if run.ignored:
        smoke["ran"] = False
        smoke["reason"] = (f"{run.ignored} test(s) were ignored, so the run does not "
                           "fully cover the plan")
        smoke["diagnostics"] = run.diagnostics[-4000:]
        return smoke

    if run.failed and not smoke["findings"]:
        # The summary counted failures but no ``:FAIL`` line could be parsed (an
        # unexpected Unity format). A counted failure is still a real defect, not
        # a pass: record it rather than let it fall through to SUCCEEDED.
        smoke["findings"].append({
            "test": "<unparsed>",
            "category": "IMPLEMENTATION_DEFECT",
            "scenario_id": "",
            "expected_behavior": "",
            "observed": (f"Unity reported {run.failed} failure(s) but no failing "
                         "test could be parsed from the output"),
        })
    return smoke


def _tool_fault_reason(run: CeedlingRun) -> str:
    if run.timed_out:
        return "the test run timed out before producing a Unity summary"
    if run.returncode < 0:
        return f"the test runner was killed by signal {-run.returncode}"
    if run.launch_error:
        return ("the Docker CLI could not be launched on the host; no Unity summary "
                "was produced, so the suite did not run")
    docker_fault = docker_fault_reason(run.returncode)
    if docker_fault is not None:
        return (f"{docker_fault}; no Unity summary was produced, so the "
                "suite did not run")
    if "BUILD FAILURE" in run.stdout:
        return ("the generated project failed to build (no Unity summary was "
                "produced); see the build diagnostics")
    return f"the test run produced no Unity summary (exit {run.returncode})"


def _findings(run: CeedlingRun, plan: Plan) -> list[dict[str, Any]]:
    """Map Unity assertion failures to the plan scenarios that produced them.

    A generated test that fails means the **implementation contradicts the
    SDD-grounded oracle** — a finding (``IMPLEMENTATION_DEFECT``), preserved as
    evidence, never repaired by editing the oracle (R04). The test-function name
    is resolved through the plan's own (unit_id, scenario_id) → name mapping, so a
    scenario id containing ``-`` or ``_`` is never mis-split and the oracle link
    stays correct.
    """
    from .gen import test_name_for

    by_name = {
        test_name_for(s.unit_id, s.id): s for s in plan.scenarios
    }
    findings: list[dict[str, Any]] = []
    for name, reason in run.failures:
        scenario = by_name.get(name)
        findings.append({
            "test": name,
            "category": "IMPLEMENTATION_DEFECT",
            "scenario_id": scenario.id if scenario else "",
            "expected_behavior": scenario.oracle.statement if scenario else "",
            "observed": reason,
        })
    return findings
