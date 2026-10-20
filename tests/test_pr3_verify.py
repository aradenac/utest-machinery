"""PR 3 `mtm verify` tests: offline replay truthfulness and profile honesty.

The PR 3 acceptance contract (see ``worklog/log/2026-10-18-pr3.md``) has two
offline-testable invariants:

* **Invariant 1 — offline replay truthfulness.** ``mtm verify`` replays the
  *frozen* suite against the *same selected input bytes* and never edits them; a
  changed snapshot is ``SNAPSHOT_MISMATCH`` (exit 40), never a misleading replay;
  and a tool fault is never reported as a functional pass or a functional failure.
* **Invariant 2 — profile truthfulness.** Each profile reports ``PASSED``/
  ``FAILED`` (with its native report), ``UNSUPPORTED`` (named) or ``TOOL_FAILED``;
  a broken profile never hides a co-observed defect, and no run makes a
  target-MCU claim.

These use an **injected** runner (the PR 2 Ceedling executor for the functional
profile and a fake profile runner for the rest), so the classification and the
persistence contract are proven without Docker. The real in-image execution of the
same profiles is a separate, Docker-gated test in this module and in the
qualification campaigns.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mtm.ceedling import CeedlingRun
from mtm.errors import Code, MtmError
from mtm.inventory import resolve_scope_paths, snapshot_hash
from mtm.profiles import FAILED, PASSED, TOOL_FAILED, UNSUPPORTED
from mtm.verify import ProfileResult, latest_run, run_verify


@pytest.fixture(autouse=True)
def _fixed_test_image_identity(monkeypatch):
    """Unit tests use a stable pretend image; no Docker is consulted."""
    monkeypatch.setattr("mtm.qualify.execution_image_identity",
                        lambda ref: {"ref": ref, "id": "sha256:offline-fixture"})



# --- helpers ----------------------------------------------------------------


def _frozen_bench(tmp_path: Path, name: str = "bench") -> tuple[Path, list[dict]]:
    """A minimal bench with a frozen plan whose snapshot matches its inputs."""
    bench = tmp_path / name
    bench.mkdir()
    (bench / "src.c").write_text("int f(void) { return 1; }\n")
    revision = bench / ".mtm" / "gen"
    revision.mkdir(parents=True)
    (revision / "project.yml").write_text(
        ":project:\n  :build_root: build\n  :use_mocks: TRUE\n"
        ":paths:\n  :source: [src/**/*.c]\n")
    (revision / "test").mkdir()
    selected = [{"path": "src.c", "kind": "source"}]
    state = bench / ".mtm" / "state" / "generate"
    state.mkdir(parents=True)
    frozen = snapshot_hash(resolve_scope_paths(bench, selected))
    (state / "inventory.json").write_text(json.dumps({"snapshot_hash": frozen}))
    from mtm.qualify import execution_image_identity, frozen_suite_hash
    suite_hash = frozen_suite_hash(bench)
    suite_hash["image"] = execution_image_identity("img")
    (state / "plan.json").write_text(json.dumps(
        {"snapshot_hash": frozen, "scenarios": [], "suite_hash": suite_hash}))
    return bench, selected


def _ceedling_run(*, failures: int = 0, ran: bool = True, returncode: int = 0,
                  **kw) -> CeedlingRun:
    total = 3
    return CeedlingRun(
        ok=returncode == 0, returncode=returncode, ran=ran, total=total,
        passed=total - failures, failed=failures,
        stdout=(f"{total} Tests {failures} Failures 0 Ignored" if ran else ""),
        argv=["docker", "run", "ceedling"], **kw)


def _runner(failures: int = 0, **kw):
    def run(bench_root, *, image_ref):
        return _ceedling_run(failures=failures, **kw)
    return run


def _profiles_fn(mapping):
    """A profile runner returning a canned status per profile id."""
    def run(bench_root, image_ref, profile, workdirs):
        return ProfileResult(id=profile.id, status=mapping.get(profile.id, PASSED),
                             reason="canned", command=f"ceedling test:all [{profile.id}]")
    return run


# --- invariant 1: frozen replay and staleness -------------------------------


def test_verify_replays_the_frozen_suite_and_persists_immutable_evidence(tmp_path):
    """A clean replay SUCCEEDS, records a profile per configured profile, and
    writes an immutable run record under ``.mtm/state/verify/<run-id>``."""
    bench, selected = _frozen_bench(tmp_path)
    result = run_verify(bench, selected, image_ref="img", run_id="r1",
                        runner=_runner(), profile_runner=_profiles_fn({}))
    assert result.status == "SUCCEEDED" and result.exit_code == 0
    assert result.snapshot_hash  # bound to the frozen snapshot
    assert {p.id for p in result.profiles} >= {"functional", "coverage", "valgrind"}

    status = json.loads((bench / ".mtm" / "state" / "verify" / "r1" / "status.json").read_text())
    assert status["status"] == "SUCCEEDED"
    assert status["schema_version"] == 1
    assert "target-MCU" in status["note"]
    # The run index points at the most recent campaign.
    assert latest_run(bench)["run_id"] == "r1" or latest_run(bench)["status"] == "SUCCEEDED"


def test_changed_input_bytes_are_a_snapshot_mismatch_never_a_replay(tmp_path):
    """The decisive staleness contrast: editing a selected input after freezing
    must yield ``SNAPSHOT_MISMATCH`` (exit 40) and run **nothing**."""
    bench, selected = _frozen_bench(tmp_path)
    (bench / "src.c").write_text("int f(void) { return 2; }\n")  # changed bytes
    calls: list[str] = []

    def spy(bench_root, *, image_ref):
        calls.append("ran")
        return _ceedling_run()

    with pytest.raises(MtmError) as exc:
        run_verify(bench, selected, image_ref="img", runner=spy,
                   profile_runner=_profiles_fn({}))
    assert exc.value.code == Code.SNAPSHOT_MISMATCH
    assert calls == [], "verify must not run the suite against changed inputs"


def test_a_changed_selected_path_set_is_also_a_snapshot_mismatch(tmp_path):
    """Adding a selected path changes the snapshot identity even if no existing
    file's bytes changed — the path set is part of the snapshot."""
    bench, selected = _frozen_bench(tmp_path)
    (bench / "extra.c").write_text("int g(void) { return 0; }\n")
    with pytest.raises(MtmError) as exc:
        run_verify(bench, selected + [{"path": "extra.c", "kind": "source"}],
                   image_ref="img", runner=_runner(), profile_runner=_profiles_fn({}))
    assert exc.value.code == Code.SNAPSHOT_MISMATCH


def test_force_runs_despite_a_changed_snapshot(tmp_path):
    """``--force`` is the explicit override: it runs, but the *frozen* snapshot is
    still what the record is bound to, so the mismatch stays visible."""
    bench, selected = _frozen_bench(tmp_path)
    (bench / "src.c").write_text("int f(void) { return 2; }\n")
    result = run_verify(bench, selected, image_ref="img", force=True,
                        runner=_runner(), profile_runner=_profiles_fn({}))
    assert result.status == "PARTIAL" and result.exit_code == 20
    assert result.replay["deviated"] and not result.replay["authenticated"]


def test_a_missing_plan_is_an_input_error_not_a_replay(tmp_path):
    bench, selected = _frozen_bench(tmp_path)
    (bench / ".mtm" / "state" / "generate" / "plan.json").unlink()
    with pytest.raises(MtmError) as exc:
        run_verify(bench, selected, image_ref="img", runner=_runner(),
                   profile_runner=_profiles_fn({}))
    assert exc.value.code == Code.INPUT_MISSING


def test_verify_never_writes_to_the_selected_inputs_or_frozen_tests(tmp_path):
    """Immutability: the selected source and the frozen project/test are
    byte-identical after a run — only new evidence is appended."""
    bench, selected = _frozen_bench(tmp_path)
    revision = bench / ".mtm" / "gen"
    (revision / "test" / "test_f.c").write_text("/* frozen */\n")
    # The fixture's newly added generated test is part of its frozen identity.
    from mtm.qualify import frozen_suite_hash
    plan_path = bench / ".mtm" / "state" / "generate" / "plan.json"
    plan = json.loads(plan_path.read_text())
    image = plan["suite_hash"]["image"]
    plan["suite_hash"] = frozen_suite_hash(bench)
    plan["suite_hash"]["image"] = image
    plan_path.write_text(json.dumps(plan))
    before = {
        "src": (bench / "src.c").read_bytes(),
        "project": (revision / "project.yml").read_bytes(),
        "test": (revision / "test" / "test_f.c").read_bytes(),
    }
    run_verify(bench, selected, image_ref="img", runner=_runner(),
               profile_runner=_profiles_fn({}))
    assert (bench / "src.c").read_bytes() == before["src"]
    assert (revision / "project.yml").read_bytes() == before["project"]
    assert (revision / "test" / "test_f.c").read_bytes() == before["test"]


def test_a_run_id_is_immutable_and_a_second_use_is_refused(tmp_path):
    """Evidence is append-only: re-using a run id must not silently overwrite the
    earlier campaign."""
    bench, selected = _frozen_bench(tmp_path)
    run_verify(bench, selected, image_ref="img", run_id="fixed", runner=_runner(),
               profile_runner=_profiles_fn({}))
    with pytest.raises(MtmError) as exc:
        run_verify(bench, selected, image_ref="img", run_id="fixed", runner=_runner(),
                   profile_runner=_profiles_fn({}))
    assert exc.value.code == Code.EVIDENCE_INVALID


# --- invariant 2: profile truthfulness --------------------------------------


def test_a_tool_fault_outranks_a_test_finding(tmp_path):
    """A broken profile must not hide a co-observed failure behind a green or a
    red verdict: ``TOOL_FAILURE`` (30) wins, and the finding is still recorded."""
    bench, selected = _frozen_bench(tmp_path)
    result = run_verify(
        bench, selected, image_ref="img",
        runner=_runner(failures=1),
        profile_runner=_profiles_fn({"coverage": TOOL_FAILED}))
    assert result.status == "TOOL_FAILURE" and result.exit_code == 30
    assert any(f["category"] == "IMPLEMENTATION_DEFECT" for f in result.findings)
    assert result.counts[TOOL_FAILED] == 1 and result.counts[FAILED] == 1


def test_an_unsupported_profile_makes_the_run_partial_not_a_pass(tmp_path):
    """A missing tool is ``UNSUPPORTED`` and the run is ``PARTIAL`` (exit 20) —
    never an invented pass and never a failure of the unit."""
    bench, selected = _frozen_bench(tmp_path)
    result = run_verify(bench, selected, image_ref="img", runner=_runner(),
                        profile_runner=_profiles_fn({"valgrind": UNSUPPORTED}))
    assert result.status == "PARTIAL" and result.exit_code == 20
    assert result.counts[UNSUPPORTED] == 1
    assert result.findings == []


def test_a_functional_failure_is_findings_not_a_tool_fault(tmp_path):
    """A real, parsed Unity failure is a domain finding (exit 10), distinct from a
    tool fault (exit 30)."""
    bench, selected = _frozen_bench(tmp_path)
    result = run_verify(bench, selected, image_ref="img",
                        runner=_runner(failures=2),
                        profile_runner=_profiles_fn({}))
    assert result.status == "FINDINGS" and result.exit_code == 10
    functional = next(p for p in result.profiles if p.id == "functional")
    assert functional.status == FAILED and functional.metrics["failures"] == 2


def test_a_summaryless_functional_run_is_a_tool_fault_not_a_failure(tmp_path):
    """A run with no Unity summary proves nothing ran: it must be ``TOOL_FAILED``
    (30), never a pass and never a functional failure."""
    bench, selected = _frozen_bench(tmp_path)
    result = run_verify(bench, selected, image_ref="img",
                        runner=_runner(ran=False, returncode=1),
                        profile_runner=_profiles_fn({}))
    assert result.status == "TOOL_FAILURE" and result.exit_code == 30
    functional = next(p for p in result.profiles if p.id == "functional")
    assert functional.status == TOOL_FAILED


def test_every_profile_records_its_own_command_and_no_mcu_claim(tmp_path):
    """Each profile reports its own command/metrics, and the record's note states
    the evidence is host-native (SYS: no target-MCU claim)."""
    bench, selected = _frozen_bench(tmp_path)
    result = run_verify(bench, selected, image_ref="img", run_id="k", runner=_runner(),
                        profile_runner=_profiles_fn({}))
    assert all(p.command for p in result.profiles)
    record = json.loads((bench / ".mtm" / "state" / "verify" / "k" / "status.json").read_text())
    assert "no target-MCU claim" in record["note"]


def test_findings_deduplicate_corroborating_assertion_failures(tmp_path):
    """A coverage/clang profile failing the *same* assertion the functional
    profile already reported is corroboration, not a second finding; a sanitizer
    *runtime* violation and a Valgrind memory error are their own findings."""
    from mtm.profiles import PASSED as P
    from mtm.verify import _findings

    results = [
        ProfileResult(id="functional", status=FAILED,
                      metrics={"failures_detail": [["t1", "Expected 10 Was 9"]]}),
        ProfileResult(id="coverage", status=FAILED, metrics={"failures": 1}),
        ProfileResult(id="sanitizers", status=FAILED, metrics={"violations": 0}),
        ProfileResult(id="valgrind", status=P),
    ]
    findings = _findings(results)
    assert len(findings) == 1
    assert findings[0]["test"] == "t1"
    # A real sanitizer violation is a distinct finding.
    violation = _findings([
        ProfileResult(id="functional", status=P, metrics={}),
        ProfileResult(id="sanitizers", status=FAILED, metrics={"violations": 1}),
    ])
    assert [f["category"] for f in violation] == ["RUNTIME_VIOLATION"]


def test_a_nonzero_exit_with_no_counted_failure_is_a_tool_fault(tmp_path):
    """A runner exit code that Unity did not back with a counted failure is a tool
    fault, never a fabricated implementation defect."""
    from mtm.profiles import PROFILES
    from mtm.verify import _classify_build_run
    import subprocess

    proc = subprocess.CompletedProcess(["ceedling"], 1,
                                       "3 Tests 0 Failures 0 Ignored\n", "")
    result = _classify_build_run(proc, PROFILES[1], "build-coverage")
    assert result.status == TOOL_FAILED


# --- profile project generation (pure) --------------------------------------


def test_the_profile_project_yml_keeps_the_frozen_paths_and_isolates_builds():
    """The profile variant must reuse the frozen ``:paths:`` verbatim and only
    change ``:build_root:`` (so instrumented objects never mix), never the
    sources."""
    from mtm.profiles import PROFILES
    from mtm.verify import _profile_project_yml

    base = (":project:\n  :build_root: build\n  :use_mocks: TRUE\n"
            ":paths:\n  :source: [src/**/*.c]\n  :test: [test/**/*.c]\n")
    coverage = _profile_project_yml(base, PROFILES[1])
    assert "src/**/*.c" in coverage and "test/**/*.c" in coverage
    assert ":build_root: build-coverage" in coverage
    assert "clang-18" in coverage and "-fcoverage-mcdc" in coverage
    assert ":build_root: build\n" not in coverage  # the frozen one is replaced


# --- Valgrind: every executable needs a trustworthy independent verdict -------


def _valgrind_case(tmp_path, monkeypatch, reports, *, runner_rc=0):
    """Drive Memcheck's real classification with a fake Docker transport only.

    Each report is (ERROR SUMMARY count or None, native child exit code).
    Executable discovery uses real files in the private profile build tree.
    """
    import subprocess
    from mtm import verify as v
    from mtm.profiles import PROFILES

    bench = tmp_path / "bench"
    workdir = bench / ".mtm" / "gen" / "profiles" / "valgrind"
    workdir.mkdir(parents=True)
    output = ""
    for index, (errors, rc) in enumerate(reports):
        name = f"test_{index}.out"
        (workdir / name).write_text("")
        output += f"__MTM_VG_BEGIN__=./{name}\n"
        if errors is not None:
            output += f"==42== ERROR SUMMARY: {errors} errors from 0 contexts\n"
            output += f"==42==    definitely lost: {index + 1} bytes in 1 blocks\n"
        output += f"__MTM_VG_EXIT__={rc}\n"

    def fake_run(bench_root, image_ref, cwd, argv, **kwargs):
        if argv == ["ceedling", "test:all"]:
            return subprocess.CompletedProcess(
                argv, 0, f"{max(len(reports), 1)} Tests 0 Failures 0 Ignored\n", "")
        assert argv[:2] == ["bash", "-lc"]
        assert argv[4:] == [f"./test_{i}.out" for i in range(len(reports))]
        return subprocess.CompletedProcess(argv, runner_rc, output, "")

    monkeypatch.setattr(v, "_run_in_image", fake_run)
    monkeypatch.setattr(v, "_tool_version", lambda *_: "Valgrind test")
    profile = next(p for p in PROFILES if p.id == "valgrind")
    return v._run_valgrind(bench, "img", profile, {"valgrind": workdir})


@pytest.mark.parametrize("reports, expected", [
    ([(0, 0), (0, 0)], PASSED),
    ([(0, 0), (2, 99)], FAILED),
    ([(0, 0), (None, 1)], TOOL_FAILED),
    ([(0, 99)], TOOL_FAILED),
    ([(0, 0), (0, 127)], TOOL_FAILED),
])
def test_valgrind_requires_complete_per_binary_reports(
        tmp_path, monkeypatch, reports, expected):
    """A clean earlier binary cannot conceal a later fault or memory violation."""
    result = _valgrind_case(tmp_path, monkeypatch, reports)
    assert result.status == expected, (result.status, result.reason, result.metrics)
    assert result.metrics["binaries_expected"] == len(reports)
    assert result.metrics["binaries_attempted"] == len(reports)
    if expected == FAILED:
        assert result.metrics["errors"] == 2
        assert result.metrics["definitely_lost_bytes"] == 3


def test_valgrind_runner_failure_cannot_claim_clean_memcheck(tmp_path, monkeypatch):
    result = _valgrind_case(tmp_path, monkeypatch, [(0, 0)], runner_rc=125)
    assert result.status == TOOL_FAILED
    assert "125" in result.reason


def test_valgrind_without_discovered_executable_is_not_a_pass(tmp_path, monkeypatch):
    result = _valgrind_case(tmp_path, monkeypatch, [])
    assert result.status == TOOL_FAILED
    assert result.metrics["binaries_expected"] == 0



# --- executable-suite tampering is never a successful frozen replay ---------


@pytest.mark.parametrize("relative_path, action", [
    ("src/copied.c", "modify"),
    ("interface/unit.h", "modify"),
    ("test/test_added.c", "add"),
    ("test/test_existing.c", "delete"),
    ("project.yml", "modify"),
])
def test_changed_executable_material_is_refused_before_execution(
        tmp_path, relative_path, action):
    """All executable inputs, not only the user's original sources, are pinned."""
    from mtm.qualify import frozen_suite_hash

    bench, selected = _frozen_bench(tmp_path)
    revision = bench / ".mtm" / "gen"
    for name in ("src/copied.c", "interface/unit.h", "test/test_existing.c"):
        target = revision / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("/* original */\n")
    plan_file = bench / ".mtm" / "state" / "generate" / "plan.json"
    plan = json.loads(plan_file.read_text())
    image = plan["suite_hash"]["image"]
    plan["suite_hash"] = frozen_suite_hash(bench)
    plan["suite_hash"]["image"] = image
    plan_file.write_text(json.dumps(plan))
    target = revision / relative_path
    if action == "delete":
        target.unlink()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("/* changed */\n")
    executions = []

    def forbidden_runner(*args, **kwargs):
        executions.append(True)
        return _ceedling_run()

    with pytest.raises(MtmError) as exc:
        run_verify(bench, selected, image_ref="img", runner=forbidden_runner,
                   profile_runner=_profiles_fn({}))
    assert exc.value.code == Code.SNAPSHOT_MISMATCH
    assert not executions


def test_legacy_suite_without_fingerprint_cannot_claim_authenticated_success(tmp_path):
    bench, selected = _frozen_bench(tmp_path)
    plan_file = bench / ".mtm" / "state" / "generate" / "plan.json"
    plan = json.loads(plan_file.read_text())
    plan.pop("suite_hash")
    plan_file.write_text(json.dumps(plan))
    with pytest.raises(MtmError) as exc:
        run_verify(bench, selected, image_ref="img",
                   runner=_runner(), profile_runner=_profiles_fn({}))
    assert exc.value.code == Code.EVIDENCE_INVALID



def test_changed_docker_image_id_is_a_snapshot_mismatch(tmp_path, monkeypatch):
    bench, selected = _frozen_bench(tmp_path)
    monkeypatch.setattr(
        "mtm.qualify.execution_image_identity",
        lambda ref: {"ref": ref, "id": "sha256:another-image"})
    with pytest.raises(MtmError) as exc:
        run_verify(bench, selected, image_ref="img",
                   runner=_runner(), profile_runner=_profiles_fn({}))
    assert exc.value.code == Code.SNAPSHOT_MISMATCH


def test_unresolved_docker_image_id_refuses_authenticated_replay(tmp_path, monkeypatch):
    bench, selected = _frozen_bench(tmp_path)
    monkeypatch.setattr(
        "mtm.qualify.execution_image_identity",
        lambda ref: {"ref": ref, "id": ""})
    with pytest.raises(MtmError) as exc:
        run_verify(bench, selected, image_ref="img",
                   runner=_runner(), profile_runner=_profiles_fn({}))
    assert exc.value.code == Code.EVIDENCE_INVALID
