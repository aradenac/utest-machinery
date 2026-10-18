"""PR 2 runner-contract tests: the result classification and revision isolation.

These are the contrasting regressions for the PR #14 review findings:

* **B2** — a crashed / timed-out / summary-less run must be a ``TOOL_FAILURE``,
  never a clean ``SUCCEEDED`` suite, and never an ``IMPLEMENTATION_DEFECT``;
* **B3** — a ``BLOCKED`` unit is excluded from the executable project but stays
  visible in the feasibility denominator, and does not stop an independent unit;
* **B5** — each generation rebuilds the revision directory, so a scope change
  leaves no stale generated test/mock behind.

The classification is exercised with an injected runner (no Docker); a separate,
Docker-gated test executes a real suite end to end.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from conftest import REPO, fake_resolve as _fake_resolve, run_mtm_json

EXAMPLES = REPO / "examples"


@pytest.fixture
def fixture_bench(tmp_path: Path):
    def _make(name: str) -> Path:
        bench = tmp_path / name
        bench.mkdir()
        for source in sorted((EXAMPLES / name).iterdir()):
            if source.is_file():
                shutil.copy(source, bench / source.name)
        record, code = run_mtm_json("init", str(bench))
        assert code == 0, record
        for source in sorted(bench.iterdir()):
            if source.suffix in (".c", ".h", ".md"):
                kind = "source" if source.suffix in (".c", ".h") else "spec"
                run_mtm_json("scope", "add", source.name, "--kind", kind, cwd=bench)
        return bench

    return _make


def _scopes(bench: Path) -> list[dict]:
    import yaml
    return yaml.safe_load((bench / "project.yaml").read_text()).get("scopes", [])


def _generate(bench: Path, runner):
    """Run generate with an injected runner, returning the GenerateResult."""
    import yaml
    from mtm.generate import run_generate
    from mtm.llm import MockProvider

    scopes = yaml.safe_load((bench / "project.yaml").read_text()).get("scopes", [])
    return run_generate(bench, scopes, provider=MockProvider(), image="img:test",
                        runner=runner, resolve=_fake_resolve)


def _run(ok: bool, returncode: int, *, stdout="", stderr="", timed_out=False,
         ran=False, total=0, passed=0, failed=0, ignored=0, failures=None):
    from mtm.ceedling import CeedlingRun

    return CeedlingRun(
        ok=ok, returncode=returncode, stdout=stdout, stderr=stderr,
        argv=["docker", "run", "img:test", "ceedling", "test:all"],
        timed_out=timed_out, ran=ran, total=total, passed=passed, failed=failed,
        ignored=ignored, failures=failures or [],
    )


def _generated_count(bench_root: Path) -> int:
    """The number of test functions the generated revision would run.

    A stub runner must report a realistic total: the classifier now requires the
    executed count to cover the generated manifest, so a hard-coded 1 would mask
    the very coverage check under test.
    """
    import re
    revision = Path(bench_root) / ".mtm" / "gen" / "test"
    total = 0
    for path in sorted(revision.glob("test_*.c")):
        total += len(re.findall(r"\nvoid (test_\w+)\(void\)", path.read_text()))
    return total


# --- B2: a tool fault is never a clean success or a defect -------------------


def test_unity_summary_aggregates_every_test_executable():
    """Ceedling runs one executable per test file, so a multi-unit scope emits
    several per-file Unity summaries and no grand total. The parser must sum them,
    not report only the first file, or a multi-unit bench is under-counted."""
    from mtm.ceedling import _unity_summary, parse_failures

    stdout = (
        "test_b.c:3:test_x:PASS\n"
        "2 Tests 1 Failures 0 Ignored\nFAIL\n"
        "test_a.c:5:test_y:PASS\n"
        "test_a.c:9:test_z:FAIL: Expected 5 Was 2\n"
        "3 Tests 0 Failures 0 Ignored\nOK\n"
    )
    assert _unity_summary(stdout) == (5, 4, 1, 0)  # total, passed, failed, ignored
    assert _unity_summary("no summary here") is None
    failures = parse_failures(stdout)
    assert len(failures) == 1 and failures[0][1] == "Expected 5 Was 2"


def test_timeout_is_a_tool_failure_not_a_success(fixture_bench):
    bench = fixture_bench("mocked-sensor")
    result = _generate(bench, lambda *a, **k: _run(False, 124, timed_out=True,
                                                   stderr="timed out"))
    assert result.status == "TOOL_FAILURE"
    assert result.exit_code == 30
    assert result.smoke["ran"] is False
    assert result.smoke["timed_out"] is True
    assert result.findings == []


def test_signal_kill_with_no_summary_is_a_tool_failure(fixture_bench):
    bench = fixture_bench("mocked-sensor")
    result = _generate(bench, lambda *a, **k: _run(False, -11, stdout=""))
    assert result.status == "TOOL_FAILURE" and result.exit_code == 30
    assert "signal" in result.smoke["reason"].lower()


def test_timeout_output_is_decoded_and_serialisable(fixture_bench, monkeypatch):
    """C4: `subprocess.TimeoutExpired.stdout` is bytes even under `text=True`.
    The runner must decode it, or the result cannot be published as JSON."""
    import subprocess as sp
    from mtm import ceedling

    def fake_run(*a, **k):
        raise sp.TimeoutExpired(cmd=a[0], timeout=1,
                                output=b"...running test_abc...\n")  # bytes on purpose

    monkeypatch.setattr(ceedling.subprocess, "run", fake_run)
    run = ceedling.run_ceedling(Path("/tmp/bench"), image_ref="img:test")
    assert run.timed_out is True
    assert isinstance(run.stdout, str) and "running test_abc" in run.stdout
    # The result must serialize (the old bytes payload raised TypeError).
    from mtm.paths import write_json_atomic
    import tempfile
    target = Path(tempfile.mkdtemp()) / "smoke.json"
    write_json_atomic(target, {"stdout": run.stdout, "stderr": run.stderr})


def test_ceedling_mounts_the_bench_read_only_and_only_the_revision_writable(
        fixture_bench, monkeypatch):
    """C3: the container must not be able to write selected inputs. The bench is
    mounted read-only with only the generated revision writable (SYS-004)."""
    captured = {}
    from mtm import ceedling

    bench = fixture_bench("mocked-sensor")

    def fake_run(argv, **k):
        captured["argv"] = argv

        class _P:
            returncode = 0
            stdout = "1 Tests 0 Failures 0 Ignored\n"
            stderr = ""
        return _P()

    monkeypatch.setattr(ceedling.subprocess, "run", fake_run)
    ceedling.run_ceedling(bench, image_ref="img:test")
    argv = captured["argv"]
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    bench_str = str(bench.resolve())
    assert f"{bench_str}:/bench:ro" in mounts           # whole bench read-only
    assert f"{bench_str}/.mtm/gen:/bench/.mtm/gen" in mounts  # only the revision RW


def test_nonzero_exit_without_a_unity_summary_is_a_tool_failure(fixture_bench):
    bench = fixture_bench("mocked-sensor")
    result = _generate(bench, lambda *a, **k: _run(
        False, 1, stdout="Compiling...\nBUILD FAILURE\n", failed=0))
    assert result.status == "TOOL_FAILURE" and result.exit_code == 30
    assert result.findings == []  # never promoted to a defect


def test_nonzero_exit_with_an_all_pass_summary_is_a_tool_failure(fixture_bench):
    """A summary that reports no failures but a non-zero exit is not a verified
    result: something outside the assertions failed. It must not be SUCCEEDED."""
    bench = fixture_bench("mocked-sensor")
    summary = "-----------------------\n3 Tests 0 Failures 0 Ignored\nOK\n"
    result = _generate(bench, lambda *a, **k: _run(
        False, 1, stdout=summary, ran=True, total=3, passed=3, failed=0))
    assert result.status == "TOOL_FAILURE" and result.exit_code == 30


def test_valid_asserted_failure_is_a_finding_not_a_tool_failure(fixture_bench):
    """Contrast: a parsed Unity FAIL with a summary is a real SDD contradiction."""
    bench = fixture_bench("saturation")

    def runner(bench_root, *, image_ref):
        total = _generated_count(bench_root)
        summary = ("test_saturation.c:12:test_u_abc_S1:FAIL: Expected 10 Was 9\n"
                   f"-----------------------\n{total} Tests 1 Failures 0 Ignored\nFAIL\n")
        return _run(False, 1, stdout=summary, ran=True, total=total,
                    passed=total - 1, failed=1,
                    failures=[("test_u_abc_S1", "Expected 10 Was 9")])

    result = _generate(bench, runner)
    assert result.status == "FINDINGS" and result.exit_code == 10
    assert result.findings and result.findings[0]["category"] == "IMPLEMENTATION_DEFECT"
    assert "Expected 10 Was 9" == result.findings[0]["observed"]


def test_clean_pass_is_succeeded(fixture_bench):
    bench = fixture_bench("mocked-sensor")

    def runner(bench_root, *, image_ref):
        total = _generated_count(bench_root)
        summary = f"-----------------------\n{total} Tests 0 Failures 0 Ignored\nOK\n"
        return _run(True, 0, stdout=summary, ran=True, total=total, passed=total, failed=0)

    result = _generate(bench, runner)
    assert result.status == "SUCCEEDED" and result.exit_code == 0


def test_counted_failure_with_unparseable_detail_is_still_a_finding(fixture_bench):
    """If the summary counts failures but no `:FAIL` line parses (an unexpected
    Unity format), the run must not fall through to SUCCEEDED — a counted failure
    is a defect, recorded even without a per-test detail line."""
    bench = fixture_bench("saturation")

    def runner(bench_root, *, image_ref):
        total = _generated_count(bench_root)
        summary = f"Something odd happened\n{total} Tests 1 Failures 0 Ignored\nFAIL\n"
        return _run(False, 1, stdout=summary, ran=True, total=total,
                    passed=total - 1, failed=1, failures=[])

    result = _generate(bench, runner)
    assert result.status == "FINDINGS" and result.exit_code == 10
    assert result.findings and "no failing test could be parsed" in result.findings[0]["observed"]


def test_no_materialisable_scenario_is_incomplete_not_a_vacuous_success(fixture_bench):
    """When every planned scenario is refused (unfaithful), there is nothing to
    execute. Running an empty project would be a vacuous success: report INCOMPLETE
    (exit 20) with the shortfall visible instead."""
    import json as _json
    import yaml
    from mtm.generate import run_generate
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity, MockProvider

    class _Stub(MockProvider):
        def complete(self, prompt, *, timeout=300):
            return _json.dumps({"scenarios": [{
                "id": "S1", "title": "t", "function": "sample_sensor",
                "requirement_ids": ["SEN-003"], "mocks": ["platform_read_sensor"],
                "cases": [{"args": [1], "expected": "SENSOR_OK",
                           "mocks": {"platform_read_sensor": {"sequence": [0, 0]}}}],
                "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"},
            }]})

        def __init__(self):
            super().__init__()
            self.identity = LLMIdentity(provider="mock", model="stub")

    bench = fixture_bench("mocked-sensor")
    scopes = yaml.safe_load((bench / "project.yaml").read_text()).get("scopes", [])
    called = {"runner": False}

    def runner(*a, **k):
        called["runner"] = True
        return _run(True, 0, stdout="", ran=False)

    result = run_generate(bench, scopes, provider=_Stub(), image="img:test",
                          runner=runner, resolve=_fake_resolve)
    assert result.status == "INCOMPLETE" and result.exit_code == 20
    assert result.suite["test_functions"] == []
    assert result.suite["limitations"]  # the refusals stay visible
    assert called["runner"] is False  # no empty project was executed


# --- B3: a BLOCKED unit does not break the independent feasible unit ---------


def test_blocked_unit_is_excluded_from_the_project_but_stays_in_denominator(
        fixture_bench, tmp_path):
    """A mixed READY + BLOCKED bench builds and runs the READY unit only.

    The blocked unit must not appear in the compiled scope (it would break the
    whole build) yet must remain visible in the feasibility counts and the
    recorded limitations (R03).
    """
    bench = fixture_bench("mocked-sensor")
    # Add a genuinely BLOCKED unit: a source calling an undefined type/function
    # that no shim can satisfy (a hard compile error, not a missing header).
    (bench / "broken.c").write_text(
        "#include \"broken.h\"\n"
        "int broken_fn(void) { return ??? ; }\n", encoding="utf-8")
    (bench / "broken.h").write_text(
        "#ifndef BROKEN_H\n#define BROKEN_H\nint broken_fn(void);\n#endif\n",
        encoding="utf-8")
    for name in ("broken.c", "broken.h"):
        run_mtm_json("scope", "add", name, "--kind", "source", cwd=bench)

    captured = {}

    def runner(bench_root, *, image_ref):
        revision = bench_root / ".mtm" / "gen"
        captured["srcs"] = sorted(p.name for p in (revision / "src").glob("*.c"))
        captured["shim"] = sorted(p.name for p in (revision / "shim").glob("*.h"))
        total = _generated_count(bench_root)
        summary = f"-----------------------\n{total} Tests 0 Failures 0 Ignored\nOK\n"
        return _run(True, 0, stdout=summary, ran=True, total=total, passed=total, failed=0)

    result = _generate(bench, runner)
    assert result.counts.get("BLOCKED", 0) >= 1
    assert result.counts.get("READY", 0) + result.counts.get("ADAPTED", 0) >= 1
    # The blocked source never entered the compiled scope, and the suite ran.
    assert "broken.c" not in captured["srcs"]
    # The run is PARTIAL (exit 20): the independent unit executed, but part of
    # the selected scope is unverified. It is never a clean SUCCEEDED.
    assert result.status == "PARTIAL"
    assert result.exit_code == 20
    assert any("BLOCKED" in lim for lim in result.suite["limitations"])


# --- review pass: basename collision, oracle mapping, stale artifacts --------


def test_two_units_sharing_a_basename_are_refused_not_silently_overwritten(
        fixture_bench):
    """A flat C revision cannot hold two `sensor.c`/`sensor.h`; a C `#include` cannot
    disambiguate them either. Colliding units must be refused with a recorded
    limitation rather than one silently overwriting the other while the manifest
    reports an inflated scenario count."""
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = fixture_bench("mocked-sensor")
    import yaml
    scopes = yaml.safe_load((bench / "project.yaml").read_text()).get("scopes", [])
    other = bench / "beta"
    other.mkdir()
    shutil.copy(bench / "sensor.c", other / "sensor.c")
    shutil.copy(bench / "sensor.h", other / "sensor.h")
    run_mtm_json("scope", "add", "beta/sensor.c", "--kind", "source", cwd=bench)
    run_mtm_json("scope", "add", "beta/sensor.h", "--kind", "source", cwd=bench)

    inventory = build_inventory(bench, _scopes(bench))
    plan = parse_plan(json.dumps({
        "scenarios": [{
            "id": "S1", "title": "t", "function": "sample_sensor",
            "requirement_ids": ["SEN-001"], "mocks": ["platform_read_sensor"],
            "cases": [{"args": [1], "expected": "SENSOR_OK",
                       "mocks": {"platform_read_sensor": {"ret": 0, "reading": 1}}}],
            "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"},
        }],
    }), inventory, LLMIdentity(provider="test", model="t"))
    generated = generate_suite(bench, plan, inventory)
    # Neither colliding unit is emitted (names cannot be disambiguated): the
    # shortfall is recorded, never silently absorbed into one unit's suite.
    assert list((bench / ".mtm" / "gen" / "test").glob("test_sensor.c")) == []
    assert any("basename" in lim for lim in generated.limitations)
    assert generated.test_functions == []


def test_directory_scope_materialises_its_files_and_detects_inner_collisions(
        fixture_bench):
    """A scope entry may name a *directory*; its files must be expanded before the
    revision is built. Iterating the raw scope entries would silently copy nothing
    (an empty `src/`) while still emitting a test file, and would also miss a
    collision between a directory's file and a file selected elsewhere."""
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = fixture_bench("mocked-sensor")
    # Move the fixture's C/H into a subdirectory and select only that directory.
    pkg = bench / "pkg"
    pkg.mkdir()
    for name in ("sensor.c", "sensor.h"):
        shutil.move(bench / name, pkg / name)
        run_mtm_json("scope", "remove", name, cwd=bench)
    run_mtm_json("scope", "add", "pkg", "--kind", "source", cwd=bench)

    inventory = build_inventory(bench, _scopes(bench))
    plan = parse_plan(json.dumps({
        "scenarios": [{
            "id": "S1", "title": "t", "function": "sample_sensor",
            "requirement_ids": ["SEN-001"], "mocks": ["platform_read_sensor"],
            "cases": [{"args": [1], "expected": "SENSOR_OK",
                       "mocks": {"platform_read_sensor": {"ret": 0, "reading": 1}}}],
            "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"},
        }],
    }), inventory, LLMIdentity(provider="test", model="t"))
    generated = generate_suite(bench, plan, inventory)
    src = bench / ".mtm" / "gen" / "src"
    # The directory's sources are copied (not silently dropped).
    assert sorted(p.name for p in src.iterdir()) == ["sensor.c", "sensor.h"]
    assert generated.test_functions  # the suite is real, not vacuous

    # A second unit whose basename also exists inside the directory is still a
    # collision: the guard must see it after expansion.
    clash = bench / "other"
    clash.mkdir()
    shutil.copy(pkg / "sensor.c", clash / "sensor.c")
    run_mtm_json("scope", "add", "other/sensor.c", "--kind", "source", cwd=bench)
    inventory2 = build_inventory(bench, _scopes(bench))
    generated2 = generate_suite(bench, plan, inventory2)
    assert any("basename" in lim for lim in generated2.limitations)


def test_scenario_ids_normalising_to_one_test_name_are_refused_not_duplicated(
        fixture_bench):
    """Two scenario ids distinct in the plan (`S1-a`, `S1_a`) normalise to one C
    symbol. Emitting both is a duplicate definition; the second must be refused
    with a recorded limitation, never compiled into an opaque link error."""
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = fixture_bench("mocked-sensor")
    inventory = build_inventory(bench, _scopes(bench))
    payload = {
        "scenarios": [
            {"id": sid, "title": "t", "function": "sample_sensor",
             "requirement_ids": ["SEN-001"], "mocks": ["platform_read_sensor"],
             "cases": [{"args": [1], "expected": "SENSOR_OK",
                        "mocks": {"platform_read_sensor": {"ret": 0, "reading": 1}}}],
             "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}
            for sid in ("S1-a", "S1_a")
        ],
    }
    plan = parse_plan(json.dumps(payload), inventory,
                      LLMIdentity(provider="test", model="t"))
    generated = generate_suite(bench, plan, inventory)
    # Exactly one test function survives, and the other is recorded, not lost.
    assert len(generated.test_functions) == 1
    assert any("normalisation" in lim for lim in generated.limitations)


def _plan_for(bench, scenarios: list[dict]):
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    inventory = build_inventory(bench, _scopes(bench))
    plan = parse_plan(json.dumps({"scenarios": scenarios}), inventory,
                      LLMIdentity(provider="test", model="t"))
    return plan, inventory


def test_argument_count_mismatch_is_refused_not_emitted_as_a_broken_call(fixture_bench):
    """A case must supply exactly one value per parameter. A mismatch would emit
    an uncompilable call and kill the whole bench as an opaque TOOL_FAILURE while
    still counting the scenario as generated — an unsupported shape is refused
    with a limitation, like every other unmaterialisable case."""
    from mtm.gen import generate_suite

    bench = fixture_bench("saturation")

    def scen(cases):
        return {"id": "S1", "title": "t", "function": "saturate_u16",
                "requirement_ids": ["SAT-002"], "mocks": [], "cases": cases,
                "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}

    # Too many, and too few, both refused with the arity named.
    for bad in ({"args": [11, 10, 999], "expected": 10}, {"args": [], "expected": 10}):
        plan, inventory = _plan_for(bench, [scen([bad])])
        out = generate_suite(bench, plan, inventory)
        assert out.test_functions == []
        assert any("argument" in lim for lim in out.limitations)

    # The exact-arity contrast materialises and runs as a real test.
    plan, inventory = _plan_for(bench, [scen([{"args": [11, 10], "expected": 10}])])
    out = generate_suite(bench, plan, inventory)
    assert out.test_functions == ["test_u52800a7b1b2b_S1"] or len(out.test_functions) == 1
    assert not out.limitations


def test_a_declared_mock_without_a_per_case_expectation_is_refused(fixture_bench):
    """A determined case that lists a dependency in `mocks` but sets no
    expectation for it would leave CMock's strict mock unarmed; the first real
    call then fails as "called more times than expected", which would be
    misreported as an IMPLEMENTATION_DEFECT. It is refused instead. The
    documented `expect_calls: 0` (assert not called) remains valid."""
    from mtm.gen import generate_suite

    bench = fixture_bench("mocked-sensor")

    def scen(cases):
        return {"id": "S1", "title": "t", "function": "sample_sensor",
                "requirement_ids": ["SEN-001"], "mocks": ["platform_read_sensor"],
                "cases": cases,
                "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}

    # Declared but unconfigured -> refused, and told why.
    plan, inventory = _plan_for(bench, [scen([{"args": [1], "expected": "SENSOR_OK"}])])
    out = generate_suite(bench, plan, inventory)
    assert out.test_functions == []
    assert any("platform_read_sensor" in lim and "expectation" in lim
               for lim in out.limitations)

    # Configured -> materialised; and `expect_calls: 0` (not called) is accepted.
    plan, inventory = _plan_for(bench, [scen([{
        "args": [1], "expected": "SENSOR_OK",
        "mocks": {"platform_read_sensor": {"ret": 0, "reading": 1}}}])])
    out = generate_suite(bench, plan, inventory)
    assert len(out.test_functions) == 1 and not out.limitations

    plan, inventory = _plan_for(bench, [scen([{
        "args": [1], "expected": "SENSOR_OK", "expect_calls": {"platform_read_sensor": 0}}])])
    out = generate_suite(bench, plan, inventory)
    assert len(out.test_functions) == 1 and not out.limitations


# --- PR #14 re-review findings C1–C5 ----------------------------------------


def test_expect_calls_zero_with_a_configured_mock_emits_no_expectation(fixture_bench):
    """C1: `expect_calls: 0` must mean *zero* calls, even when the case also
    supplies `mocks` config for the symbol. The old truthy-only guard turned 0
    into one expected call, so a forbidden dependency call could pass."""
    from mtm.gen import generate_suite

    bench = fixture_bench("mocked-sensor")

    def src_for(case: dict) -> str:
        scen = {"id": "S1", "title": "t", "function": "sample_sensor",
                "requirement_ids": ["SEN-001"], "mocks": ["platform_read_sensor"],
                "cases": [case],
                "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}
        plan, inventory = _plan_for(bench, [scen])
        generate_suite(bench, plan, inventory)
        return (bench / ".mtm" / "gen" / "test" / "test_sensor.c").read_text()

    zero = src_for({"args": [1], "expected": "SENSOR_OK",
                    "mocks": {"platform_read_sensor": {"ret": 0}},
                    "expect_calls": {"platform_read_sensor": 0}})
    assert "platform_read_sensor_ExpectAnyArgsAndReturn(" not in zero
    assert "platform_read_sensor_ExpectAnyArgs(" not in zero
    # Contrast: an explicit positive count emits exactly that many expectations.
    two = src_for({"args": [1], "expected": "SENSOR_OK",
                   "mocks": {"platform_read_sensor": {"ret": 0}},
                   "expect_calls": {"platform_read_sensor": 2}})
    assert two.count("platform_read_sensor_ExpectAnyArgsAndReturn(") == 2


def test_expect_calls_for_an_unmocked_symbol_is_refused(fixture_bench):
    """C1: a call-count planned for a symbol the scenario does not mock would be
    silently dropped (no expectation emitted). It is refused instead."""
    from mtm.gen import generate_suite

    bench = fixture_bench("mocked-sensor")
    scen = {"id": "S1", "title": "t", "function": "sample_sensor",
            "requirement_ids": ["SEN-001"], "mocks": ["platform_read_sensor"],
            "cases": [{"args": [1], "expected": "SENSOR_OK",
                       "mocks": {"platform_read_sensor": {"ret": 0}},
                       "expect_calls": {"not_a_mock": 3}}],
            "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}
    plan, inventory = _plan_for(bench, [scen])
    out = generate_suite(bench, plan, inventory)
    assert out.test_functions == []
    assert any("not_a_mock" in lim for lim in out.limitations)


def test_a_scenario_with_no_cases_is_refused_not_a_vacuous_pass(fixture_bench):
    """C2: a scenario with `cases: []` would emit an empty test function Unity
    counts as a pass ("1 Tests 0 Failures") without exercising any C."""
    from mtm.gen import generate_suite

    bench = fixture_bench("saturation")
    scen = {"id": "S1", "title": "t", "function": "saturate_u16",
            "requirement_ids": ["SAT-002"], "mocks": [], "cases": [],
            "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}
    plan, inventory = _plan_for(bench, [scen])
    out = generate_suite(bench, plan, inventory)
    assert out.test_functions == []
    assert any("no cases" in lim for lim in out.limitations)


def test_fewer_executed_tests_than_generated_is_a_tool_failure(fixture_bench):
    """C2: the generated manifest is the authority on how many tests must run. A
    summary covering fewer tests than were generated is an incomplete execution,
    never a clean SUCCEEDED."""
    bench = fixture_bench("mocked-sensor")  # generates 3 tests

    def runner(bench_root, *, image_ref):
        # Report only one of the three generated tests as executed.
        return _run(True, 0, stdout="-----------------------\n1 Tests 0 Failures 0 Ignored\nOK\n",
                    ran=True, total=1, passed=1, failed=0)

    result = _generate(bench, runner)
    assert result.status == "TOOL_FAILURE" and result.exit_code == 30
    assert result.findings == []


def test_a_unit_without_a_selected_header_gets_a_private_declaration(fixture_bench,
                                                                    tmp_path):
    """C5: a standalone `foo.c` with no `foo.h` must not include a fabricated
    header. A private declaration header, grounded in the scanner's prototypes,
    is materialised under the interface directory instead."""
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = tmp_path / "headerless"
    bench.mkdir()
    (bench / "foo.c").write_text("int foo_add(int a, int b) { return a + b; }\n")
    (bench / "sdd.md").write_text(
        "# SDD\n- **FOO-001**: `foo_add` SHALL return the sum of its arguments.\n")
    run_mtm_json("init", str(bench))
    run_mtm_json("scope", "add", "foo.c", "--kind", "source", cwd=bench)
    run_mtm_json("scope", "add", "sdd.md", "--kind", "spec", cwd=bench)

    inventory = build_inventory(bench, _scopes(bench))
    plan = parse_plan(json.dumps({"scenarios": [{
        "id": "S1", "title": "t", "function": "foo_add",
        "requirement_ids": ["FOO-001"], "mocks": [],
        "cases": [{"args": [2, 3], "expected": 5}],
        "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}]}),
        inventory, LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory)
    assert len(out.test_functions) == 1
    # A private declaration header exists and is included; no fabricated foo.h.
    # It carries the source stem (`foo.h`) so Ceedling links `foo.c` into the
    # test's link line; a synthetic name would leave the SUT undefined.
    private = bench / ".mtm" / "gen" / "interface" / "foo.h"
    assert private.is_file()
    assert "int foo_add(int a, int b);" in private.read_text()
    assert '"foo.h"' in (bench / ".mtm" / "gen" / "test" / "test_foo.c").read_text()
    # The declaration header must live in the private interface directory, never
    # in the copied source directory (SYS-004: inputs are never written to).
    assert not (bench / ".mtm" / "gen" / "src" / "foo.h").exists()


# --- PR #14 third review findings D1–D3 -------------------------------------


def test_all_ignored_tests_are_not_a_complete_success(fixture_bench):
    """D1: an ignored Unity test is enumerated (`N Tests ... K Ignored`) but its
    body does not run. `executed = total - ignored`, so all-ignored must not pass
    the generated-count gate — zero test bodies ran."""
    from mtm.ceedling import _unity_summary

    # `passed` excludes ignored tests: an all-ignored run reports 0 passes, not 3.
    assert _unity_summary("3 Tests 0 Failures 3 Ignored") == (3, 0, 0, 3)

    bench = fixture_bench("mocked-sensor")  # generates 3 tests

    def runner(bench_root, *, image_ref):
        # Unity enumerates 3 but ignores all 3 (none executed).
        return _run(True, 0, stdout="-----------------------\n3 Tests 0 Failures 3 Ignored\nOK\n",
                    ran=True, total=3, passed=3, failed=0, ignored=3)

    result = _generate(bench, runner)
    assert result.status == "TOOL_FAILURE" and result.exit_code == 30
    assert "ignored" in result.smoke["reason"].lower()
    assert result.findings == []


def test_a_partially_ignored_run_is_incomplete(fixture_bench):
    """D1 contrast: 2 of 3 executed, 1 ignored -> not full coverage, not SUCCEEDED."""
    bench = fixture_bench("mocked-sensor")  # generates 3 tests

    def runner(bench_root, *, image_ref):
        return _run(True, 0, stdout="-----------------------\n3 Tests 0 Failures 1 Ignored\nOK\n",
                    ran=True, total=3, passed=3, failed=0, ignored=1)

    result = _generate(bench, runner)
    assert result.status == "TOOL_FAILURE" and result.exit_code == 30


def test_assertion_finding_with_a_signal_is_a_tool_failure_not_a_finding(fixture_bench):
    """D2: a tool fault (signal/timeout) must outrank a co-observed assertion. A
    parseable failure summary plus a killed runner is TOOL_FAILURE/30, with the
    assertion finding preserved — never just FINDINGS/10."""
    bench = fixture_bench("saturation")  # generates 2 tests

    def runner(bench_root, *, image_ref):
        total = _generated_count(bench_root)
        summary = ("test_saturation.c:12:test_u_abc_S1:FAIL: Expected 10 Was 9\n"
                   f"-----------------------\n{total} Tests 1 Failures 0 Ignored\nFAIL\n")
        return _run(False, -9, stdout=summary, ran=True, total=total,
                    passed=total - 1, failed=1,
                    failures=[("test_u_abc_S1", "Expected 10 Was 9")])

    result = _generate(bench, runner)
    assert result.status == "TOOL_FAILURE" and result.exit_code == 30
    assert "signal" in result.smoke["reason"].lower()
    # The assertion evidence is preserved alongside the tool failure.
    assert result.smoke["findings"] and result.smoke["findings"][0]["observed"] == "Expected 10 Was 9"


def test_ordinary_asserted_failure_stays_a_finding_contrast(fixture_bench):
    """D2 contrast: a *normal* Unity failure exit (nonzero, no signal/timeout) with
    a covering summary stays a FINDINGS/10, not a tool failure."""
    bench = fixture_bench("saturation")

    def runner(bench_root, *, image_ref):
        total = _generated_count(bench_root)
        summary = ("test_saturation.c:12:test_u_abc_S1:FAIL: Expected 10 Was 9\n"
                   f"-----------------------\n{total} Tests 1 Failures 0 Ignored\nFAIL\n")
        return _run(False, 1, stdout=summary, ran=True, total=total,
                    passed=total - 1, failed=1,
                    failures=[("test_u_abc_S1", "Expected 10 Was 9")])

    result = _generate(bench, runner)
    assert result.status == "FINDINGS" and result.exit_code == 10


def test_a_source_with_an_unrelated_local_include_still_gets_its_own_declaration(
        tmp_path):
    """D3: `foo.c` includes `dep.h`, which declares only a dependency (no `foo.h`
    exists). The chosen include must not be `dep.h` (it lacks the SUT prototype);
    a scanner-grounded private declaration for `foo` is materialised instead."""
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = tmp_path / "unrelated-include"
    bench.mkdir()
    (bench / "foo.c").write_text(
        '#include "dep.h"\nint foo(int x) { return dep(x) + 1; }\n')
    (bench / "dep.h").write_text(
        "#ifndef DEP_H\n#define DEP_H\nint dep(int x);\n#endif\n")
    (bench / "sdd.md").write_text(
        "# SDD\n- **FOO-001**: `foo` SHALL increment its dependency's result.\n")
    run_mtm_json("init", str(bench))
    for name, kind in (("foo.c", "source"), ("dep.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", name, "--kind", kind, cwd=bench)

    inventory = build_inventory(bench, _scopes(bench))
    plan = parse_plan(json.dumps({"scenarios": [{
        "id": "S1", "title": "t", "function": "foo",
        "requirement_ids": ["FOO-001"], "mocks": [],
        "cases": [{"args": [4], "expected": 5}],
        "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}]}),
        inventory, LLMIdentity(provider="test", model="t"))
    generate_suite(bench, plan, inventory)
    test_src = (bench / ".mtm" / "gen" / "test" / "test_foo.c").read_text()
    # The test must see a declaration of `foo`, not only of `dep` — and it must be
    # named after the source stem so Ceedling links `foo.c`.
    assert '"foo.h"' in test_src
    assert '"dep.h"' not in test_src
    decl = (bench / ".mtm" / "gen" / "interface" / "foo.h").read_text()
    assert "int foo(int x);" in decl


def test_a_sut_declared_by_a_non_stem_header_still_includes_the_stem_header(
        tmp_path):
    """D3 residual: `sensor.c` includes `api.h`, which declares the SUT
    (`sensor_read`), and there is no `sensor.h`.

    Choosing `api.h` alone would let the test *compile* but Ceedling would never
    link `sensor.c` (it links `<stem>.c` only when the test includes `<stem>.h`),
    so the run dies as an opaque `undefined reference`. Both headers must appear:
    `api.h` for the real declaration, `sensor.h` (synthesised, stem-named) to
    force the link."""
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = tmp_path / "non-stem-header"
    bench.mkdir()
    (bench / "sensor.c").write_text(
        '#include "api.h"\nint sensor_read(int x) { return x + 1; }\n')
    (bench / "api.h").write_text(
        "#ifndef API_H\n#define API_H\nint sensor_read(int x);\n#endif\n")
    (bench / "sdd.md").write_text(
        "# SDD\n- **SEN-001**: `sensor_read` SHALL return x+1.\n")
    run_mtm_json("init", str(bench))
    for name, kind in (("sensor.c", "source"), ("api.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", name, "--kind", kind, cwd=bench)

    inventory = build_inventory(bench, _scopes(bench))
    plan = parse_plan(json.dumps({"scenarios": [{
        "id": "S1", "title": "t", "function": "sensor_read",
        "requirement_ids": ["SEN-001"], "mocks": [],
        "cases": [{"args": [2], "expected": 3}],
        "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}]}),
        inventory, LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory)
    assert out.test_functions, out.limitations
    test_src = (bench / ".mtm" / "gen" / "test" / "test_sensor.c").read_text()
    assert '"api.h"' in test_src          # the real declaration (fidelity)
    assert '"sensor.h"' in test_src       # the stem header that forces the link
    assert (bench / ".mtm" / "gen" / "interface" / "sensor.h").is_file()
    assert not (bench / ".mtm" / "gen" / "src" / "sensor.h").exists()


def test_a_real_non_declaring_stem_header_is_refused_not_shadowed(tmp_path):
    """D3 contrast: a *selected* `foo.h` that does not declare the SUT cannot be
    worked around (a private `foo.h` would shadow it), so the unit is refused with
    a limitation rather than mislinked — and the rest of the scope still runs."""
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = tmp_path / "non-declaring-stem"
    bench.mkdir()
    (bench / "foo.c").write_text("int foo(int x) { return x + 1; }\n")
    # A `foo.h` exists but declares an unrelated symbol, not `foo`.
    (bench / "foo.h").write_text(
        "#ifndef FOO_H\n#define FOO_H\nint other(int x);\n#endif\n")
    (bench / "sdd.md").write_text(
        "# SDD\n- **FOO-001**: `foo` SHALL return x+1.\n")
    run_mtm_json("init", str(bench))
    for name, kind in (("foo.c", "source"), ("foo.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", name, "--kind", kind, cwd=bench)

    inventory = build_inventory(bench, _scopes(bench))
    plan = parse_plan(json.dumps({"scenarios": [{
        "id": "S1", "title": "t", "function": "foo",
        "requirement_ids": ["FOO-001"], "mocks": [],
        "cases": [{"args": [1], "expected": 2}],
        "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}]}),
        inventory, LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory)
    assert out.test_functions == []
    assert any("cannot be linked" in lim for lim in out.limitations)



def test_finding_maps_back_to_its_oracle_for_ids_with_dashes_and_underscores():
    """The finding→scenario link must survive an id containing `-` or `_`; the old
    reverse-split returned a truncated id and an empty oracle statement."""
    from mtm.gen import test_name_for
    from mtm.plan import Scenario, Oracle, TestCase, Plan
    from mtm.generate import _findings

    plan = Plan(snapshot_hash="x", model={}, scenarios=[
        Scenario(id="S1_boundary", title="t", function="f", unit_id="unit-abc",
                 requirement_ids=[], technique="", mocks=[], mock_declarations={},
                 cases=[TestCase()],
                 oracle=Oracle(kind="SPECIFIED", statement="the ceiling applies")),
        Scenario(id="S2-a", title="t", function="f", unit_id="unit-abc",
                 requirement_ids=[], technique="", mocks=[], mock_declarations={},
                 cases=[TestCase()],
                 oracle=Oracle(kind="SPECIFIED", statement="the floor applies")),
    ])

    class _Run:
        failures = [
            (test_name_for("unit-abc", "S1_boundary"), "Expected 10 Was 9"),
            (test_name_for("unit-abc", "S2-a"), "Expected 1 Was 0"),
        ]

    findings = _findings(_Run(), plan)
    assert findings[0]["scenario_id"] == "S1_boundary"
    assert findings[0]["expected_behavior"] == "the ceiling applies"
    assert findings[1]["scenario_id"] == "S2-a"
    assert findings[1]["expected_behavior"] == "the floor applies"


def test_an_early_exit_clears_the_previous_plans_derived_artifacts(fixture_bench):
    """A BLOCKED / dry-run run must not leave a previous input set's plan.json
    looking current for the new inputs."""
    from mtm.generate import run_generate
    from mtm.llm import MockProvider

    bench = fixture_bench("mocked-sensor")
    first = run_generate(bench, _scopes(bench), provider=MockProvider(),
                         image="img:test", resolve=_fake_resolve, runner=lambda *a, **k: _run(
                             True, 0, stdout="1 Tests 0 Failures 0 Ignored\nOK\n",
                             ran=True, total=1, passed=1, failed=0))
    assert first.plan is not None
    state = bench / ".mtm" / "state" / "generate"
    assert (state / "plan.json").is_file()

    (bench / "sensor.c").write_text("int sample_sensor(void) { return ??? ; }\n")
    second = run_generate(bench, _scopes(bench), provider=MockProvider(),
                          image="img:test", resolve=_fake_resolve,
                          runner=lambda *a, **k: _run(True, 0))
    assert second.status == "BLOCKED"
    assert not (state / "plan.json").is_file()


# --- B5: a scope change rebuilds the revision (no stale artefacts) -----------

def test_revision_is_rebuilt_so_a_scope_change_leaves_no_stale_test(fixture_bench):
    """Generate, then narrow the scope and generate again.

    The second revision must not contain the first run's test file or mock
    interface: the project is rebuilt from the current plan, never compiled from
    an accumulating directory (B5).
    """
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = fixture_bench("mocked-sensor")
    import yaml
    scopes = yaml.safe_load((bench / "project.yaml").read_text()).get("scopes", [])

    inventory = build_inventory(bench, scopes)
    plan = parse_plan(json.dumps({
        "scenarios": [{
            "id": "S1", "title": "t", "function": "sample_sensor",
            "requirement_ids": ["SEN-001"], "mocks": ["platform_read_sensor"],
            "cases": [{"args": [1], "expected": "SENSOR_OK",
                       "mocks": {"platform_read_sensor": {"ret": 0, "reading": 1}}}],
            "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"},
        }],
    }), inventory, LLMIdentity(provider="test", model="t"))
    generate_suite(bench, plan, inventory)
    assert list((bench / ".mtm" / "gen" / "test").glob("test_sensor.c"))
    assert list((bench / ".mtm" / "gen" / "interface").glob("mtm_dep_*.h"))

    # Narrow the scope: the unit is now excluded (e.g. it became BLOCKED). A
    # rebuilt revision must not leave the previous test or interface behind.
    unit_id = inventory.units[0].id
    generate_suite(bench, plan, inventory, excluded_units={unit_id})
    assert not list((bench / ".mtm" / "gen" / "test").glob("test_sensor.c"))
    assert not list((bench / ".mtm" / "gen" / "interface").glob("mtm_dep_*.h"))


# --- Docker-gated: a real end-to-end execution -------------------------------


@pytest.mark.docker
def test_real_ceedling_run_executes_the_suite(fixture_bench, product_image):
    """AP-02: the actually-generated project runs in the image, to a result."""
    from mtm.ceedling import run_ceedling

    bench = fixture_bench("mocked-sensor")
    # The real in-image model needs a token; use the recorded provider to produce
    # the plan, then execute the generated project for real.
    record, code = run_mtm_json("generate", "--provider", "mock", cwd=bench)
    assert code in (0, 10), record
    run = run_ceedling(bench, image_ref=product_image)
    assert run.ran, f"no Unity summary: {run.diagnostics[-2000:]}"
    assert run.total >= 1



# --- PR #14 fourth review findings E1–E2 ------------------------------------


def _failure_summary_runner(bench_root, returncode):
    """A runner that printed a real Unity failure summary, then exited *returncode*."""
    from test_pr2_runner import _generated_count, _run  # noqa: F811

    total = _generated_count(bench_root)
    summary = ("test_saturation.c:12:test_u_abc_S1:FAIL: Expected 10 Was 9\n"
               f"-----------------------\n{total} Tests 1 Failures 0 Ignored\nFAIL\n")
    return _run(returncode < 0, returncode, stdout=summary, ran=True, total=total,
                passed=total - 1, failed=1,
                failures=[("test_u_abc_S1", "Expected 10 Was 9")])


@pytest.mark.parametrize("returncode,label", [
    (137, "SIGKILL"),
    (143, "SIGTERM"),
    (134, "SIGABRT"),
    (139, "SIGSEGV"),
    (125, "daemon error"),
    (126, "command not invokable"),
    (127, "command not found"),
])
def test_docker_abnormal_exit_with_a_failure_summary_is_a_tool_failure(
        fixture_bench, returncode, label):
    """E1: ``docker run`` returns its own statuses for a run that is not a test
    result — 128+signal for a killed container (137=SIGKILL, 143=SIGTERM,
    134=SIGABRT, 139=SIGSEGV), and 125/126/127 for a daemon/entrypoint error —
    *not* the negative parent-shell status the previous classifier tested. A kill
    that happens after the runner already printed a parseable Unity failure summary
    must still be a tool fault that outranks (and preserves) the assertion finding,
    never a ``FINDINGS/10`` that hides the interrupted run."""
    bench = fixture_bench("saturation")

    def runner(bench_root, *, image_ref):
        return _failure_summary_runner(bench_root, returncode)

    result = _generate(bench, runner)
    assert result.status == "TOOL_FAILURE" and result.exit_code == 30, result.status
    # The assertion evidence is preserved alongside the tool failure.
    assert result.smoke["findings"] and \
        result.smoke["findings"][0]["observed"] == "Expected 10 Was 9"
    assert str(returncode) in result.smoke["reason"]


def test_a_killed_container_names_the_signal(fixture_bench):
    """E1: a 137/143 status is reported as a killed container, not as a plain
    non-zero exit — the reason must name the signal."""
    bench = fixture_bench("saturation")

    def runner(bench_root, *, image_ref):
        return _failure_summary_runner(bench_root, 137)

    result = _generate(bench, runner)
    assert "SIGKILL" in result.smoke["reason"] and "killed" in result.smoke["reason"]


def test_a_docker_abnormal_exit_without_a_summary_is_a_tool_failure(fixture_bench):
    """E1 adjacent: the same status with no summary keeps its honest wording."""
    bench = fixture_bench("mocked-sensor")

    def runner(bench_root, *, image_ref):
        return _run(False, 137, stdout="Running...", stderr="Killed")

    result = _generate(bench, runner)
    assert result.status == "TOOL_FAILURE" and result.exit_code == 30
    assert result.smoke["ran"] is False


def test_ordinary_ceedling_assertion_exit_stays_a_finding_not_a_tool_fault(
        fixture_bench):
    """E1 contrast: a *genuine* Ceedling assertion exit is a small positive status
    (1), not a Docker reserved status, so it must stay ``FINDINGS/10`` — the
    abnormal-status check must not swallow every nonzero exit."""
    bench = fixture_bench("saturation")

    def runner(bench_root, *, image_ref):
        return _failure_summary_runner(bench_root, 1)

    result = _generate(bench, runner)
    assert result.status == "FINDINGS" and result.exit_code == 10


def test_a_stem_header_declaring_only_some_functions_still_declares_the_rest(
        tmp_path):
    """E2: ``foo.c`` defines ``foo_a`` and ``foo_b``; the selected ``foo.h``
    declares only ``foo_a`` and the plan tests both.

    Including ``foo.h`` alone leaves the ``foo_b(...)`` call with no visible
    prototype (implicit declaration under older C, or a hard error) while the
    generator records no limitation. The test must additionally include a private
    helper header declaring exactly the missing function, and the whole suite must
    still link."""
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = tmp_path / "partial-stem-header"
    bench.mkdir()
    (bench / "foo.c").write_text(
        "int foo_a(int x) { return x + 1; }\nint foo_b(int x) { return x + 2; }\n")
    (bench / "foo.h").write_text(
        "#ifndef FOO_H\n#define FOO_H\nint foo_a(int x);\n#endif\n")
    (bench / "sdd.md").write_text(
        "# SDD\n- **FOO-001**: `foo_a` SHALL return x+1.\n"
        "- **FOO-002**: `foo_b` SHALL return x+2.\n")
    run_mtm_json("init", str(bench))
    for name, kind in (("foo.c", "source"), ("foo.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", name, "--kind", kind, cwd=bench)

    inventory = build_inventory(bench, _scopes(bench))
    plan = parse_plan(json.dumps({"scenarios": [
        {"id": "S1", "title": "a", "function": "foo_a", "requirement_ids": ["FOO-001"],
         "mocks": [], "cases": [{"args": [1], "expected": 2}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}},
        {"id": "S2", "title": "b", "function": "foo_b", "requirement_ids": ["FOO-002"],
         "mocks": [], "cases": [{"args": [1], "expected": 3}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}]}),
        inventory, LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory)
    assert out.test_functions, out.limitations
    test_src = (bench / ".mtm" / "gen" / "test" / "test_foo.c").read_text()
    assert '"foo.h"' in test_src                      # the real, selected header
    assert '"mtm_unit_foo_extra.h"' in test_src        # declares the omitted foo_b
    helper = (bench / ".mtm" / "gen" / "interface" / "mtm_unit_foo_extra.h")
    assert helper.is_file()
    body = helper.read_text()
    assert "int foo_b(int x);" in body
    assert "foo_a" not in body                         # only the missing function
    # The selected `foo.h` is never shadowed or overwritten.
    assert (bench / "foo.h").read_text().count("foo_a") == 1


def test_a_stem_header_declaring_every_function_needs_no_helper(tmp_path):
    """E2 contrast: when the selected stem header declares all functions under
    test, no helper header is added (the common, faithful case is unchanged)."""
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = tmp_path / "full-stem-header"
    bench.mkdir()
    (bench / "foo.c").write_text(
        "int foo_a(int x) { return x + 1; }\nint foo_b(int x) { return x + 2; }\n")
    (bench / "foo.h").write_text(
        "#ifndef FOO_H\n#define FOO_H\nint foo_a(int x);\nint foo_b(int x);\n#endif\n")
    (bench / "sdd.md").write_text(
        "# SDD\n- **FOO-001**: `foo_a`.\n- **FOO-002**: `foo_b`.\n")
    run_mtm_json("init", str(bench))
    for name, kind in (("foo.c", "source"), ("foo.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", name, "--kind", kind, cwd=bench)

    inventory = build_inventory(bench, _scopes(bench))
    plan = parse_plan(json.dumps({"scenarios": [
        {"id": "S1", "title": "a", "function": "foo_a", "requirement_ids": ["FOO-001"],
         "mocks": [], "cases": [{"args": [1], "expected": 2}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}},
        {"id": "S2", "title": "b", "function": "foo_b", "requirement_ids": ["FOO-002"],
         "mocks": [], "cases": [{"args": [1], "expected": 3}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}]}),
        inventory, LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory)
    test_src = (bench / ".mtm" / "gen" / "test" / "test_foo.c").read_text()
    assert '"foo.h"' in test_src
    assert "mtm_unit_foo_extra.h" not in test_src
    assert not (bench / ".mtm" / "gen" / "interface" / "mtm_unit_foo_extra.h").exists()


def test_any_omitted_function_subset_is_declared_and_units_get_distinct_helpers(
        tmp_path):
    """E2 adjacent: the helper declares *exactly* the omitted functions, whatever
    the subset (`foo.h` declares the middle function `foo_b` only), and two units
    in the same bench each get a helper named after their own stem (no collision)."""
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = tmp_path / "three-fn-two-unit"
    bench.mkdir()
    (bench / "foo.c").write_text(
        "int foo_a(int x) { return x + 1; }\nint foo_b(int x) { return x + 2; }\n"
        "int foo_c(int x) { return x + 3; }\n")
    (bench / "bar.c").write_text(
        "int bar_a(int x) { return x; }\nint bar_b(int x) { return x; }\n")
    # foo.h declares only the *middle* function under test; bar.h the first only.
    (bench / "foo.h").write_text(
        "#ifndef FOO_H\n#define FOO_H\nint foo_b(int x);\n#endif\n")
    (bench / "bar.h").write_text(
        "#ifndef BAR_H\n#define BAR_H\nint bar_a(int x);\n#endif\n")
    (bench / "sdd.md").write_text(
        "# SDD\n- **F1**: a\n- **F2**: b\n- **F3**: c\n- **B1**: d\n- **B2**: e\n")
    run_mtm_json("init", str(bench))
    for name, kind in (("foo.c", "source"), ("foo.h", "source"), ("bar.c", "source"),
                       ("bar.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", name, "--kind", kind, cwd=bench)

    inventory = build_inventory(bench, _scopes(bench))
    fns = [("foo_a", "F1"), ("foo_b", "F2"), ("foo_c", "F3"),
           ("bar_a", "B1"), ("bar_b", "B2")]
    plan = parse_plan(json.dumps({"scenarios": [
        {"id": f"S{i}", "title": "t", "function": f, "requirement_ids": [r], "mocks": [],
         "cases": [{"args": [1], "expected": 2}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}
        for i, (f, r) in enumerate(fns, 1)]}),
        inventory, LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory)
    assert out.test_functions, out.limitations

    iface = bench / ".mtm" / "gen" / "interface"
    foo_helper = (iface / "mtm_unit_foo_extra.h").read_text()
    # `foo.h` declares `foo_b`, so the helper must declare the other two only.
    assert "int foo_a(int x);" in foo_helper and "int foo_c(int x);" in foo_helper
    assert "foo_b" not in foo_helper
    bar_helper = (iface / "mtm_unit_bar_extra.h").read_text()
    assert "int bar_b(int x);" in bar_helper and "bar_a" not in bar_helper
    # Distinct helper names, so the two units never collide.
    assert (bench / ".mtm" / "gen" / "test" / "test_foo.c").read_text().count(
        "mtm_unit_foo_extra.h") == 1
    assert (bench / ".mtm" / "gen" / "test" / "test_bar.c").read_text().count(
        "mtm_unit_bar_extra.h") == 1


@pytest.mark.docker
def test_real_image_links_and_executes_a_partial_stem_header_unit(tmp_path, product_image):
    """AP-02 for E2: the multi-function/partial-stem-header bench actually compiles,
    links and executes in the product image.

    Honest scope: the image's default ``gcc`` (gnu17, no ``-Werror``) only *warns*
    on an implicit declaration, so this run alone does not prove the helper is
    necessary — it proves the post-fix suite is genuinely buildable and executable.
    The discriminating assertions for the fix are the string/limitation checks in
    the offline tests above (helper emitted with exactly the omitted declarations,
    a conflicting stem header refused)."""
    from mtm.ceedling import run_ceedling
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = tmp_path / "real-partial-stem"
    bench.mkdir()
    (bench / "foo.c").write_text(
        "int foo_a(int x) { return x + 1; }\nint foo_b(int x) { return x + 2; }\n")
    (bench / "foo.h").write_text(
        "#ifndef FOO_H\n#define FOO_H\nint foo_a(int x);\n#endif\n")
    (bench / "sdd.md").write_text(
        "# SDD\n- **FOO-001**: `foo_a` SHALL return x+1.\n"
        "- **FOO-002**: `foo_b` SHALL return x+2.\n")
    run_mtm_json("init", str(bench))
    for name, kind in (("foo.c", "source"), ("foo.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", name, "--kind", kind, cwd=bench)

    inventory = build_inventory(bench, _scopes(bench))
    plan = parse_plan(json.dumps({"scenarios": [
        {"id": "S1", "title": "a", "function": "foo_a", "requirement_ids": ["FOO-001"],
         "mocks": [], "cases": [{"args": [1], "expected": 2}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}},
        {"id": "S2", "title": "b", "function": "foo_b", "requirement_ids": ["FOO-002"],
         "mocks": [], "cases": [{"args": [1], "expected": 3}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}]}),
        inventory, LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory)
    assert out.test_functions, out.limitations
    run = run_ceedling(bench, image_ref=product_image)
    assert run.ran, f"no Unity summary: {run.diagnostics[-2000:]}"
    assert run.total == 2 and run.failed == 0, (run.total, run.failed, run.stdout[-1000:])


def test_a_selected_file_named_like_the_helper_is_refused_not_shadowed(tmp_path):
    """E2 adjacent (same contract class): if a selected file already bears the
    private helper's name (`mtm_unit_<stem>_extra.h`), writing our interface header
    would shadow it — `interface/` precedes `src/` on the include path. The unit is
    refused with a limitation rather than silently shadowing a selected input."""
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = tmp_path / "helper-name-collision"
    bench.mkdir()
    (bench / "foo.c").write_text(
        "int foo_a(int x) { return x; }\nint foo_b(int x) { return x; }\n")
    (bench / "foo.h").write_text(
        "#ifndef FOO_H\n#define FOO_H\nint foo_a(int x);\n#endif\n")
    (bench / "mtm_unit_foo_extra.h").write_text(
        "#ifndef OTHER_H\n#define OTHER_H\nint unrelated(int x);\n#endif\n")
    (bench / "sdd.md").write_text("# SDD\n- **A**: a\n- **B**: b\n")
    run_mtm_json("init", str(bench))
    for name, kind in (("foo.c", "source"), ("foo.h", "source"),
                       ("mtm_unit_foo_extra.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", name, "--kind", kind, cwd=bench)

    inventory = build_inventory(bench, _scopes(bench))
    plan = parse_plan(json.dumps({"scenarios": [
        {"id": "S1", "title": "t", "function": "foo_a", "requirement_ids": ["A"],
         "mocks": [], "cases": [{"args": [1], "expected": 2}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}},
        {"id": "S2", "title": "t", "function": "foo_b", "requirement_ids": ["B"],
         "mocks": [], "cases": [{"args": [1], "expected": 2}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}]}),
        inventory, LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory)
    assert out.test_functions == []
    assert any("cannot be shadowed" in lim for lim in out.limitations), out.limitations
    # The selected file is untouched and never overwritten by the generator.
    assert (bench / "mtm_unit_foo_extra.h").read_text().count("unrelated") == 1
    assert not (bench / ".mtm" / "gen" / "interface" / "mtm_unit_foo_extra.h").exists()


def test_a_stem_header_declaring_an_incompatible_signature_is_refused(tmp_path):
    """E2 adjacent (same contract class, found by the test-challenger): a selected
    stem header that declares a function under test with a different arity/parameter
    type is *not* "covered" — the test would see the header's prototype and call it
    with the plan's arguments, a hard compile error, and we cannot work around it
    (a private declaration would clash with the header). The unit is refused with a
    limitation instead of emitting uncompilable code."""
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = tmp_path / "incompatible-stem-header"
    bench.mkdir()
    (bench / "foo.c").write_text("int foo_a(int x) { return x + 1; }\n")
    # The header declares the same NAME with a different arity.
    (bench / "foo.h").write_text("#ifndef FOO_H\n#define FOO_H\nint foo_a(void);\n#endif\n")
    (bench / "sdd.md").write_text("# SDD\n- **A**: a\n")
    run_mtm_json("init", str(bench))
    for name, kind in (("foo.c", "source"), ("foo.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", name, "--kind", kind, cwd=bench)

    inventory = build_inventory(bench, _scopes(bench))
    plan = parse_plan(json.dumps({"scenarios": [
        {"id": "S1", "title": "t", "function": "foo_a", "requirement_ids": ["A"],
         "mocks": [], "cases": [{"args": [1], "expected": 2}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}]}),
        inventory, LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory)
    assert out.test_functions == []
    assert any("incompatible" in lim for lim in out.limitations), out.limitations


def test_a_stem_header_with_a_different_param_name_is_still_used(tmp_path):
    """E2 contrast: a stem header that repeats the same declaration with different
    parameter *names* is compatible and must not be falsely refused."""
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = tmp_path / "param-name-differs"
    bench.mkdir()
    (bench / "foo.c").write_text("int foo_a(int x) { return x + 1; }\n")
    (bench / "foo.h").write_text(
        "#ifndef FOO_H\n#define FOO_H\nint foo_a(int value);\n#endif\n")
    (bench / "sdd.md").write_text("# SDD\n- **A**: a\n")
    run_mtm_json("init", str(bench))
    for name, kind in (("foo.c", "source"), ("foo.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", name, "--kind", kind, cwd=bench)

    inventory = build_inventory(bench, _scopes(bench))
    plan = parse_plan(json.dumps({"scenarios": [
        {"id": "S1", "title": "t", "function": "foo_a", "requirement_ids": ["A"],
         "mocks": [], "cases": [{"args": [1], "expected": 2}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}]}),
        inventory, LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory)
    assert out.test_functions, out.limitations
    src = (bench / ".mtm" / "gen" / "test" / "test_foo.c").read_text()
    assert '"foo.h"' in src and "mtm_unit_foo_extra.h" not in src


def test_a_host_launch_error_is_reported_as_such_not_as_an_in_container_127(
        fixture_bench, monkeypatch):
    """E1 adjacent: when ``docker`` cannot be launched on the *host*, the diagnostic
    must name that cause, not the in-container 'command not found (127)' wording."""
    import subprocess
    from mtm.ceedling import run_ceedling

    bench = fixture_bench("mocked-sensor")
    run_mtm_json("generate", "--provider", "mock", cwd=bench)

    def boom(*a, **k):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", boom)
    run = run_ceedling(bench, image_ref="img:test")
    assert run.launch_error and not run.ran
    assert run.returncode == 127


def test_a_pointer_returning_stem_header_with_an_incompatible_arity_is_refused(tmp_path):
    """E2 residual (found by the fresh final verifier): ``split_declaration`` refuses
    pointer-returning signatures for *generator* reasons, so a pointer-returning
    prototype must still be comparable for the conflict check — otherwise the
    incompatible call is emitted with no limitation and dies as an opaque
    build failure (the exact class E2 removes)."""
    from mtm.gen import _signature_key, generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    # The key must exist (not None) for pointer returns, and separate an arity
    # mismatch while accepting a matching declaration (behavioural, not tied to
    # the key's internal format).
    assert _signature_key("int *foo_a(int x)") is not None
    assert _signature_key("int *foo_a(int x)") != _signature_key("int *foo_a(void)")
    assert _signature_key("int *foo_a(int x)") == _signature_key("int *foo_a(int y)")

    bench = tmp_path / "ptr-return-incompatible"
    bench.mkdir()
    (bench / "foo.c").write_text("int *foo_a(int x) { return 0; }\n")
    (bench / "foo.h").write_text("#ifndef FOO_H\n#define FOO_H\nint *foo_a(void);\n#endif\n")
    (bench / "sdd.md").write_text("# SDD\n- **A**: a\n")
    run_mtm_json("init", str(bench))
    for name, kind in (("foo.c", "source"), ("foo.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", name, "--kind", kind, cwd=bench)

    inventory = build_inventory(bench, _scopes(bench))
    plan = parse_plan(json.dumps({"scenarios": [
        {"id": "S1", "title": "t", "function": "foo_a", "requirement_ids": ["A"],
         "mocks": [], "cases": [{"args": [1], "expected": 2}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}]}),
        inventory, LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory)
    assert out.test_functions == []
    assert any("incompatible" in lim for lim in out.limitations), out.limitations


def test_a_non_stem_declaring_header_with_an_incompatible_signature_is_refused(tmp_path):
    """E2 residual adjacent: the same conflict check must also cover a *non-stem*
    declaring header (`sensor.c` includes `api.h`), whose incompatible prototype
    would clash with the synthesised stem header."""
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = tmp_path / "non-stem-incompatible"
    bench.mkdir()
    (bench / "sensor.c").write_text(
        '#include "api.h"\nint sensor_read(int x) { return x; }\n')
    (bench / "api.h").write_text(
        "#ifndef API_H\n#define API_H\nint sensor_read(void);\n#endif\n")
    (bench / "sdd.md").write_text("# SDD\n- **S**: s\n")
    run_mtm_json("init", str(bench))
    for name, kind in (("sensor.c", "source"), ("api.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", name, "--kind", kind, cwd=bench)

    inventory = build_inventory(bench, _scopes(bench))
    plan = parse_plan(json.dumps({"scenarios": [
        {"id": "S1", "title": "t", "function": "sensor_read",
         "requirement_ids": ["S"], "mocks": [],
         "cases": [{"args": [1], "expected": 2}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}]}),
        inventory, LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory)
    assert out.test_functions == []
    assert any("incompatible" in lim for lim in out.limitations), out.limitations


def test_compatible_signature_spellings_are_never_falsely_refused(tmp_path):
    """E2 contrast: pointer returns with a differing parameter *name* / pointer
    spacing, and `(void)` vs `()` are compatible and must still generate."""
    from mtm.gen import _signature_key

    assert _signature_key("int *foo_a(int x)") == _signature_key("int *foo_a(int y)")
    assert _signature_key("int *foo_a(int x)") == _signature_key("int * foo_a(int x)")
    assert _signature_key("int foo_a(void)") == _signature_key("int foo_a()")


def test_signature_compatibility_rule_matrix(tmp_path):
    """E2-residual: the ONE rule the conflict check implements, as a documented
    matrix — so it is not re-litigated case by case (a convergence-guardian and a
    fresh verifier each caught a distinct over-reach while it was ad hoc).

    Rule (C 6.7.6.3p15): a *parameter* is taken as having the unqualified version
    of its OUTERMOST type, so a top-level qualifier (by-value, or on the pointer
    itself) is ignored; every other level keeps its qualifier **by identity and
    position**. Storage class is ignored everywhere and base tokens compare
    order-insensitively. A return type is not a parameter, so it keeps even its
    outermost qualifier.

    Textual approximation (recorded limitation): a typedef alias of the same type
    (`size_t` vs `unsigned long`) is treated as a difference.
    """
    from mtm.gen import _signature_key as key

    # (a, b, same?)
    matrix = [
        # ignored: storage class, and the OUTERMOST qualifier of a parameter
        ("int f(int x)", "int f(const int x)", True),           # by-value const
        ("int f(int x)", "int f(volatile int x)", True),        # by-value volatile
        ("int f(int x)", "extern int f(int x);", True),         # storage class
        ("int f(int x)", "static int f(int x);", True),
        ("int f(int *p)", "int f(int * const p)", True),        # pointer-level const
        ("int f(int *p)", "int f(int * restrict p)", True),     # pointer-level restrict
        # compatible: name, token order, qualifier order
        ("int f(int a)", "int f(int b)", True),
        ("int f(unsigned long n)", "int f(long unsigned n)", True),
        ("int f(const volatile int *p)", "int f(volatile const int *p)", True),
        ("int f(int)", "int f(int x)", True),                   # unnamed param
        # significant: any non-outermost qualifier, by identity and position
        ("int f(char *p)", "int f(const char *p)", False),      # pointee const
        ("int f(const int *p)", "int f(volatile int *p)", False),  # pointee identity
        ("int f(char **p)", "int f(char * const *p)", False),   # INNER pointer const
        ("int f(int *p)", "int f(int **p)", False),             # pointer depth
        # significant: arity, base type, return type
        ("int f(int x)", "int f(long x)", False),
        ("int f(int x)", "int f(int x, int y)", False),
        ("int f(int x)", "long f(int x)", False),
        ("int f(int *p)", "long f(int *p)", False),
    ]
    for a, b, same in matrix:
        got = key(a) == key(b)
        assert got == same, f"{a!r} vs {b!r}: same={got}, expected {same}"

    # End to end: the `const int` bench generates (outermost qualifier ignored)…
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = tmp_path / "const-param-compatible"
    bench.mkdir()
    (bench / "sensor.c").write_text("int sensor_read(int x) { return x; }\n")
    (bench / "sensor.h").write_text(
        "#ifndef SENSOR_H\n#define SENSOR_H\nint sensor_read(const int x);\n#endif\n")
    (bench / "sdd.md").write_text("# SDD\n- **S**: s\n")
    run_mtm_json("init", str(bench))
    for name, kind in (("sensor.c", "source"), ("sensor.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", name, "--kind", kind, cwd=bench)
    inventory = build_inventory(bench, _scopes(bench))
    plan = parse_plan(json.dumps({"scenarios": [
        {"id": "S1", "title": "t", "function": "sensor_read", "requirement_ids": ["S"],
         "mocks": [], "cases": [{"args": [1], "expected": 1}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}]}),
        inventory, LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory)
    assert out.test_functions, out.limitations


def test_a_variadic_signature_is_refused_not_emitted_as_an_invalid_call(tmp_path):
    """E2-residual (fresh PR_RECHECK, HIGH): a variadic function signature is a
    shape the generator cannot render faithfully — ``split_params`` turns the
    trailing ``...`` into a parameter, so the emitted call is ``foo_a((int)1,
    (...)2)`` (not valid C) and the whole bench dies as an *opaque* build failure
    with no recorded limitation. The scenario must be refused with a limitation
    instead, whether the variadic parameter is miscounted as one argument or two."""
    from mtm.gen import func_params, generate_suite, _unsupported_param_shape
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    # The scanner's synthetic `...` parameter is detected regardless of arity.
    assert "..." in func_params("int foo_a(int x, ...)")[1].type
    assert _unsupported_param_shape(func_params("int foo_a(int x, ...)"))
    assert not _unsupported_param_shape(func_params("int foo_a(int x, int y)"))

    bench = tmp_path / "variadic-sut"
    bench.mkdir()
    (bench / "foo.c").write_text("int foo_a(int x, ...) { return x; }\n")
    (bench / "sdd.md").write_text("# SDD\n- **A**: a\n")
    run_mtm_json("init", str(bench))
    for name, kind in (("foo.c", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", name, "--kind", kind, cwd=bench)
    inventory = build_inventory(bench, _scopes(bench))
    for args in ([1], [1, 2]):
        plan = parse_plan(json.dumps({"scenarios": [
            {"id": "S1", "title": "t", "function": "foo_a", "requirement_ids": ["A"],
             "mocks": [], "cases": [{"args": args, "expected": 1}],
             "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}]}),
            inventory, LLMIdentity(provider="test", model="t"))
        out = generate_suite(bench, plan, inventory)
        assert out.test_functions == [], (args, out.test_functions)
        assert any("variadic" in lim for lim in out.limitations), out.limitations


def test_an_array_or_function_pointer_parameter_is_refused_not_misrendered(tmp_path):
    """E2-residual (fresh PR_RECHECK, HIGH): an array parameter (``int a[3]``) or a
    function-pointer parameter (``int (*cb)(int)``) cannot be materialised
    faithfully — the argument is rendered as a scalar cast, so a *pointer*
    parameter receives an ``int``. The SUT then runs with a nonsense argument and
    the run is misreported as a fabricated ``IMPLEMENTATION_DEFECT`` (or crashes).
    It must instead be refused with a recorded limitation.

    Honest scope: the deterministic scanner's function-definition regex excludes
    ``()`` from the parameter list, so a **function-pointer-parameter** definition
    is never surfaced (the plan rejects it as an unknown function); that branch of
    the guard is therefore only asserted at the parameter-shape level, defensively.
    The array case is reached end to end."""
    from mtm.gen import _unsupported_param_shape, func_params, generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    assert _unsupported_param_shape(func_params("int foo_a(int xs[4])"))
    assert _unsupported_param_shape(func_params("int foo_a(int (*cb)(int))"))
    # A plain pointer parameter with a supplied pointed-to value stays supported.
    assert not _unsupported_param_shape(func_params("int foo_a(int *p)"))
    # An UNNAMED parameter is legal C17 and renders fine — never refused (a false
    # refusal of this form was found by the convergence-guardian).
    for sig in ("int foo_a(int)", "int foo_a(char *)", "int foo_a(const char *)",
                "int foo_a(uint8_t)", "int foo_a(void)"):
        assert not _unsupported_param_shape(func_params(sig)), sig

    bench = tmp_path / "unsupported-array"
    bench.mkdir()
    (bench / "foo.c").write_text("int foo_a(int xs[4]) { return xs[0]; }\n")
    (bench / "sdd.md").write_text("# SDD\n- **A**: a\n")
    run_mtm_json("init", str(bench))
    for name, kind in (("foo.c", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", name, "--kind", kind, cwd=bench)
    inventory = build_inventory(bench, _scopes(bench))
    plan = parse_plan(json.dumps({"scenarios": [
        {"id": "S1", "title": "t", "function": "foo_a", "requirement_ids": ["A"],
         "mocks": [], "cases": [{"args": [1], "expected": 1}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}]}),
        inventory, LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory)
    assert out.test_functions == [], out.test_functions
    assert any("function-pointer or array" in lim for lim in out.limitations), \
        out.limitations


@pytest.mark.docker
def test_real_image_builds_a_previously_refused_compatible_declaration(
        tmp_path, product_image):
    """AP-02 for the E2-residual contrast: the `const int` parameter bench that the
    pre-fix conflict check falsely refused now compiles and executes in the product
    image (it is C-compatible: a top-level `const` on a parameter does not change
    the function type)."""
    from mtm.ceedling import run_ceedling
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = tmp_path / "real-const-compatible"
    bench.mkdir()
    (bench / "sensor.c").write_text("int sensor_read(int x) { return x; }\n")
    (bench / "sensor.h").write_text(
        "#ifndef SENSOR_H\n#define SENSOR_H\nint sensor_read(const int x);\n#endif\n")
    (bench / "sdd.md").write_text("# SDD\n- **S**: s\n")
    run_mtm_json("init", str(bench))
    for name, kind in (("sensor.c", "source"), ("sensor.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", name, "--kind", kind, cwd=bench)
    inventory = build_inventory(bench, _scopes(bench))
    plan = parse_plan(json.dumps({"scenarios": [
        {"id": "S1", "title": "t", "function": "sensor_read", "requirement_ids": ["S"],
         "mocks": [], "cases": [{"args": [1], "expected": 1}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}]}),
        inventory, LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory)
    assert out.test_functions, out.limitations
    run = run_ceedling(bench, image_ref=product_image)
    assert run.ran, f"no Unity summary: {run.diagnostics[-2000:]}"
    assert run.total == 1 and run.failed == 0, (run.total, run.failed, run.stdout[-1000:])


def test_a_pointer_parameter_qualifier_conflict_is_refused(tmp_path):
    """E2-residual contrast (found by the convergence-guardian, then the fresh
    verifier): a qualifier below the parameter's outermost level is part of the
    type, so a header disagreeing with the source on it is a genuine conflict
    (gcc: "conflicting types") and must be refused with a limitation. Normalising
    a pointee qualifier away — or collapsing all pointee qualifiers to one token,
    or dropping an inner-pointer qualifier — admitted a real conflict that then
    died as the opaque build failure E2 removes."""
    from mtm.gen import _signature_key, generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    assert _signature_key("int foo_a(char *p)") != \
        _signature_key("int foo_a(const char *p)")
    assert _signature_key("int foo_a(const int *p)") != \
        _signature_key("int foo_a(volatile int *p)")
    assert _signature_key("int foo_a(char **p)") != \
        _signature_key("int foo_a(char * const *p)")

    for label, source, header in (
            ("pointee-const", "int foo_a(const char *p) { return p[0]; }\n",
             "int foo_a(char *p);"),
            ("pointee-identity", "int foo_a(const int *p) { return *p; }\n",
             "int foo_a(volatile int *p);"),
            ("inner-pointer", "int foo_a(char **p) { return 0; }\n",
             "int foo_a(char * const *p);")):
        bench = tmp_path / f"qualifier-conflict-{label}"
        bench.mkdir()
        (bench / "foo.c").write_text(source)
        (bench / "foo.h").write_text(
            f"#ifndef FOO_H\n#define FOO_H\n{header}\n#endif\n")
        (bench / "sdd.md").write_text("# SDD\n- **A**: a\n")
        run_mtm_json("init", str(bench))
        for name, kind in (("foo.c", "source"), ("foo.h", "source"), ("sdd.md", "spec")):
            run_mtm_json("scope", "add", name, "--kind", kind, cwd=bench)
        inventory = build_inventory(bench, _scopes(bench))
        plan = parse_plan(json.dumps({"scenarios": [
            {"id": "S1", "title": "t", "function": "foo_a", "requirement_ids": ["A"],
             "mocks": [], "cases": [{"args": ["x"], "expected": 1}],
             "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}]}),
            inventory, LLMIdentity(provider="test", model="t"))
        out = generate_suite(bench, plan, inventory)
        assert out.test_functions == [], (label, out.test_functions)
        assert any("incompatible" in lim for lim in out.limitations), (label, out.limitations)


def test_a_matching_pointee_qualified_declaration_generates(tmp_path):
    """Contrast for the conflict above: an *outermost* qualifier stays ignorable
    while a matching non-outermost qualifier is not a conflict, so the same
    declarations that are refused when they disagree must generate when they
    agree."""
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = tmp_path / "matching-pointee-qualifier"
    bench.mkdir()
    (bench / "foo.c").write_text("int foo_a(volatile int *p) { return *p; }\n")
    (bench / "foo.h").write_text(
        "#ifndef FOO_H\n#define FOO_H\nint foo_a(volatile int *p);\n#endif\n")
    (bench / "sdd.md").write_text("# SDD\n- **A**: a\n")
    run_mtm_json("init", str(bench))
    for name, kind in (("foo.c", "source"), ("foo.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", name, "--kind", kind, cwd=bench)
    inventory = build_inventory(bench, _scopes(bench))
    plan = parse_plan(json.dumps({"scenarios": [
        {"id": "S1", "title": "t", "function": "foo_a", "requirement_ids": ["A"],
         "mocks": [], "cases": [{"args": ["x"], "expected": 1}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}]}),
        inventory, LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory)
    assert out.test_functions, out.limitations


# --- F1 (PR #14 fifth review, owner-arbitrated): multi-level and `void *` ------


def _ptr_bench(tmp_path, name, source, header, sdd, scenarios):
    """Build a one-unit bench from in-test C/header/SDD text and generate."""
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = tmp_path / name
    bench.mkdir()
    (bench / "foo.c").write_text(source)
    (bench / "foo.h").write_text(header)
    (bench / "sdd.md").write_text(sdd)
    run_mtm_json("init", str(bench))
    for n, kind in (("foo.c", "source"), ("foo.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", n, "--kind", kind, cwd=bench)
    inventory = build_inventory(bench, _scopes(bench))
    plan = parse_plan(json.dumps({"scenarios": scenarios}), inventory,
                      LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory, resolve=_fake_resolve)
    return bench, out, (bench / ".mtm" / "gen" / "test" / "test_foo.c").read_text()


def _one_scenario(fn, args, expected, req="FOO-001"):
    return {"id": "S1", "title": "t", "function": fn, "requirement_ids": [req],
            "mocks": [], "cases": [{"args": args, "expected": expected}],
            "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}


def test_a_multi_level_pointer_argument_is_passed_at_its_declared_depth(tmp_path):
    """F1 (owner-arbitrated, HIGH): an ``int **`` parameter must receive an
    ``int **`` whose target holds the plan's value — not the ``int *`` the
    previous emitter produced by collapsing the declarator to a boolean and
    passing ``&scalar``. The generated C is asserted directly (the discriminating
    check); a Docker-gated test below executes the same shape for real."""
    from mtm.gen import func_params

    # The parser keeps the pointer depth (the root cause of the defect).
    params = func_params("int foo_a(int **pp)")
    assert [p.depth for p in params] == [2] and params[0].type == "int"

    bench, out, source = _ptr_bench(
        tmp_path, "int-pp",
        "int foo_a(int **pp) { return **pp; }\n",
        "#ifndef FOO_H\n#define FOO_H\nint foo_a(int **pp);\n#endif\n",
        "# SDD\n- **FOO-001**: `foo_a` SHALL return the doubly-pointed value.\n",
        [_one_scenario("foo_a", [42], 42)])
    assert out.test_functions, out.limitations
    assert not out.limitations, out.limitations
    # The pointee scalar is backed, one pointer level is built, and the call
    # passes that pointer's *address* — an `int **`, matching the declaration.
    assert "int out0_0_v0 = (int)42;" in source
    assert "int * out0_0_p1 = &out0_0_v0;" in source
    assert "foo_a(&out0_0_p1)" in source
    # The oracle must really assert: a bare substring `foo_a(&out0_0_p1)` also
    # appears in the vacuous `(void)foo_a(...)` form an ungrounded scenario emits,
    # so assert the full assertion — self-defending against a grounding regression.
    assert "TEST_ASSERT_EQUAL(42, foo_a(&out0_0_p1))" in source


def test_a_void_pointer_argument_is_backed_and_cast_not_an_invalid_void_local(tmp_path):
    """F1 (owner-arbitrated, HIGH): a ``void *`` parameter previously emitted the
    invalid ``void out0_0 = (void)7;`` (a ``void`` object cannot exist), which died
    as an opaque ``TOOL_FAILURE``. It must instead be backed by a real object and
    passed as a correctly cast ``void *``."""
    from mtm.gen import func_params, _unsupported_param_shape

    params = func_params("int foo_a(void *p)")
    assert [p.depth for p in params] == [1] and params[0].type == "void"
    assert _unsupported_param_shape(params) is None  # supported, never refused

    bench, out, source = _ptr_bench(
        tmp_path, "void-star",
        "int foo_a(void *p) { return *(int *)p; }\n",
        "#ifndef FOO_H\n#define FOO_H\nint foo_a(void *p);\n#endif\n",
        "# SDD\n- **FOO-001**: `foo_a` SHALL return the pointed-to int.\n",
        [_one_scenario("foo_a", [7], 7)])
    assert out.test_functions, out.limitations
    assert not out.limitations, out.limitations
    assert "int out0_0_v0 = (int)7;" in source
    assert "foo_a((void *)&out0_0_v0)" in source
    assert "void out0_0" not in source  # the invalid declaration is gone


def test_a_plain_pointer_argument_is_unchanged_by_the_depth_fix(tmp_path):
    """Contrast for F1: the depth-1 emission (the common case) is byte-identical
    to the contract the earlier findings exercised — ``T v0 = (T)value;`` and
    ``&v0`` — so the fix does not regress a supported single-indirection form."""
    bench, out, source = _ptr_bench(
        tmp_path, "int-star",
        "int foo_a(int *p) { return *p; }\n",
        "#ifndef FOO_H\n#define FOO_H\nint foo_a(int *p);\n#endif\n",
        "# SDD\n- **FOO-001**: `foo_a` SHALL return the pointed-to int.\n",
        [_one_scenario("foo_a", [5], 5)])
    assert out.test_functions, out.limitations
    assert "int out0_0 = (int)5;" in source
    assert "foo_a(&out0_0)" in source


def test_a_qualified_void_pointer_is_backed_like_plain_void(tmp_path):
    """F1 neighbour of the same defect class: a *qualified* void pointee
    (``const void *``, ``volatile void *`` — very common in C APIs) is the same
    "void names no object" case as ``void *``, but the base type arrives as
    ``"const void"``/``"volatile void"``. Detecting void by string equality let
    these fall through to the scalar path and re-emit the invalid
    ``const void out0_0 = (const void)7;`` the F1 fix removed, while still being
    reported as supported. The void test must look past the qualifiers, and the
    cast must carry the declared pointee so a ``const void **`` parameter accepts
    it."""
    from mtm.gen import func_params, _pointer_arg, _unsupported_param_shape

    for decl, base in (("const void *p", "const void"),
                       ("volatile void *p", "volatile void"),
                       ("const void * const p", "const void")):
        params = func_params(f"int foo_a({decl})")
        assert _unsupported_param_shape(params) is None, decl  # still supported
        lines, arg, _ = _pointer_arg(params[0], "o0", 7)
        assert lines == ["    int o0_v0 = (int)7;"], (decl, lines)
        assert arg == f"({base} *)&o0_v0", (decl, arg)

    # A qualified *multi-level* void pointer casts to its own declared base, not a
    # bare `void **` (a `const void **` parameter does not accept a `void **`).
    p2 = func_params("int foo_a(const void **p)")[0]
    lines2, arg2, _ = _pointer_arg(p2, "o0", 7)
    assert lines2 == ["    int o0_v0 = (int)7;", "    int * o0_p1 = &o0_v0;"], lines2
    assert arg2 == "(const void * *)&o0_p1", arg2

    # End to end: the generated test is not only valid C but asserts the SUT result.
    bench, out, source = _ptr_bench(
        tmp_path, "const-void-star",
        "int foo_a(const void *p) { return *(const int *)p; }\n",
        "#ifndef FOO_H\n#define FOO_H\nint foo_a(const void *p);\n#endif\n",
        "# SDD\n- **FOO-001**: `foo_a` SHALL return the pointed-to int.\n",
        [_one_scenario("foo_a", [7], 7)])
    assert out.test_functions, out.limitations
    assert not out.limitations, out.limitations
    assert "const void out0_0" not in source  # the invalid declaration is gone
    assert "int out0_0_v0 = (int)7;" in source
    assert "foo_a((const void *)&out0_0_v0)" in source
    assert "TEST_ASSERT_EQUAL(7, foo_a((const void *)&out0_0_v0))" in source


def test_a_string_argument_for_a_pointer_is_refused_not_truncated(tmp_path):
    """Same admit-then-fabricate class as F1: a pointer argument is materialised as
    a *pointed-to scalar*, so a plan value that renders as a C string literal
    cannot be it. It previously emitted ``const char out0_0 = (const char)"hi
    there";`` — a pointer-to-int cast (uncompilable under ``-Werror``) that
    silently dropped the planned value and let the case assert a fabricated
    result. Refuse it with a named limitation; identifiers/enums/numbers and the
    ``{"expr": …}`` escape hatch still work."""
    bench, out, _ = _ptr_bench(
        tmp_path, "char-string",
        "#include <string.h>\nint foo_a(const char *s) { return (int)strlen(s); }\n",
        "#ifndef FOO_H\n#define FOO_H\nint foo_a(const char *s);\n#endif\n",
        "# SDD\n- **FOO-001**: `foo_a` SHALL return the length.\n",
        [_one_scenario("foo_a", ["hi there"], 8)])
    assert out.test_functions == [], out.test_functions
    assert any("string for a pointer parameter" in lim for lim in out.limitations), \
        out.limitations
    # An enum/identifier or an explicit expression is still accepted for a pointer.
    _, ok, _ = _ptr_bench(
        tmp_path, "char-enum",
        "int foo_a(const char *s) { return (int)strlen(s); }\n",
        "#ifndef FOO_H\n#define FOO_H\nint foo_a(const char *s);\n#endif\n",
        "# SDD\n- **FOO-001**: `foo_a` SHALL return the length.\n",
        [_one_scenario("foo_a", [{"expr": "kMessage"}], 8)])
    assert ok.test_functions, ok.limitations


def test_unsupported_parameter_shapes_stay_refused_after_the_depth_fix(tmp_path):
    """F1 negative contrast (unchanged scope): variadic, array and
    function-pointer parameters remain refused with a named limitation — the F1
    fix widens support to multi-level/``void`` pointers only."""
    from mtm.gen import func_params, _unsupported_param_shape

    for sig in ("int foo_a(int xs[4])", "int foo_a(int (*cb)(int))",
                "int foo_a(int x, ...)"):
        assert _unsupported_param_shape(func_params(sig)), sig

    bench, out, _ = _ptr_bench(
        tmp_path, "still-refused",
        "int foo_a(int xs[4]) { return xs[0]; }\n",
        "#ifndef FOO_H\n#define FOO_H\nint foo_a(int xs[4]);\n#endif\n",
        "# SDD\n- **FOO-001**: a\n",
        [_one_scenario("foo_a", [1], 1)])
    assert out.test_functions == [], out.test_functions
    assert any("function-pointer or array" in lim for lim in out.limitations), \
        out.limitations


@pytest.mark.docker
def test_real_image_executes_multi_level_and_void_pointer_suts(tmp_path, product_image):
    """AP-02 for F1: the generated ``int **`` and ``void *`` benches actually
    compile **and execute** in the product image, with a meaningful oracle — the
    owner arbitration requires real generation *and* execution, not merely a
    compile. The SUT dereferences through the exact pointer depth, so a
    misrendered argument would crash or return the wrong value rather than pass."""
    from mtm.ceedling import run_ceedling

    cases = [
        ("int-pp-real", "int foo_a(int **pp) { return **pp; }\n",
         "int foo_a(int **pp);\n", [_one_scenario("foo_a", [42], 42)]),
        ("void-star-real", "int foo_a(void *p) { return *(int *)p; }\n",
         "int foo_a(void *p);\n", [_one_scenario("foo_a", [7], 7)]),
    ]
    for name, source, decl, scenarios in cases:
        bench, out, _ = _ptr_bench(
            tmp_path, name, source,
            f"#ifndef FOO_H\n#define FOO_H\n{decl}\n#endif\n",
            "# SDD\n- **FOO-001**: the SUT SHALL return the pointed-to value.\n",
            scenarios)
        assert out.test_functions, (name, out.limitations)
        run = run_ceedling(bench, image_ref=product_image)
        assert run.ran, (name, run.diagnostics[-2000:])
        assert run.total == 1 and run.failed == 0, \
            (name, run.total, run.failed, run.stdout[-1000:])


def test_a_pointer_level_qualifier_is_not_hoisted_onto_the_pointee(tmp_path):
    """F1 neighbour (reviewer counterexample): `int * const *pp` and
    `const int **pp` are *different* C types. A top-level pointer qualifier is
    ignored by C for compatibility and must be dropped, not moved onto the
    pointee — hoisting it (`const int **`) would emit C that does not compile
    against an `int * const *` declaration, i.e. the same opaque-failure class F1
    removes. The parser keeps the *base* segment's qualifiers and drops the
    pointer-level ones, so each shape is emitted for the type it declares."""
    from mtm.gen import func_params, _pointer_arg

    p_const_star = func_params("int foo_a(int * const *pp)")[0]
    assert (p_const_star.type, p_const_star.depth) == ("int", 2)
    p_pointee_const = func_params("int foo_a(const int **pp)")[0]
    assert (p_pointee_const.type, p_pointee_const.depth) == ("const int", 2)

    # `int * const *` → a plain `int *` backing (no `const`), so it compiles.
    _, arg, _ = _pointer_arg(p_const_star, "o0", 5)
    assert arg == "&o0_p1"
    lines, _, _ = _pointer_arg(p_const_star, "o0", 5)
    assert any("int * o0_p1" in l for l in lines) and \
        not any("const" in l for l in lines)
    # `const int **` keeps the pointee `const`.
    lines2, _, _ = _pointer_arg(p_pointee_const, "o0", 5)
    assert any("const int * o0_p1" in l for l in lines2)
    # `void **` is a named F1 contract shape and takes its own branch: the backing
    # is `int`-shaped and the argument is cast to `void **` (the pointer-to-void
    # exception does not recurse, so the level above the first is cast explicitly).
    p_void_star_star = func_params("int foo_a(void **pp)")[0]
    assert (p_void_star_star.type, p_void_star_star.depth) == ("void", 2)
    lines3, arg3, _ = _pointer_arg(p_void_star_star, "o0", 5)
    assert lines3 == ["    int o0_v0 = (int)5;", "    int * o0_p1 = &o0_v0;"], lines3
    assert arg3 == "(void * *)&o0_p1", arg3


def test_a_mock_write_through_to_a_void_or_multi_level_pointer_is_refused(tmp_path):
    """F1 neighbour on the *mock* path (reviewer finding): CMock's `ReturnThruPtr`
    passes the address of a local of the pointee type, which is faithful only for a
    single-indirection, non-void pointee. A `void *` out-parameter would declare an
    invalid `void var`, and an `int **` out-parameter would receive an `int *`. That
    control must be refused with a named limitation rather than emit wrong C; the
    mock's ordinary scalar out-parameter is unaffected."""
    from mtm.gen import mock_controls, _case_problem
    from mtm.plan import TestCase

    ctrl_void = mock_controls("int dep(void *p);", "dep")
    assert _case_problem(TestCase(mocks={"dep": {"ret": 0, "p": 5}}),
                         {"dep": ctrl_void}, [])
    ctrl_pp = mock_controls("int dep(int **pp);", "dep")
    assert _case_problem(TestCase(mocks={"dep": {"ret": 0, "pp": 5}}),
                         {"dep": ctrl_pp}, [])
    # Contrast: a scalar out-parameter is still perfectly supported.
    ctrl_int = mock_controls("int dep(int *p);", "dep")
    assert _case_problem(TestCase(mocks={"dep": {"ret": 0, "p": 5}}),
                         {"dep": ctrl_int}, []) is None


# --- F1 residual (PR #14 sixth review): the post-call observable for `T **` ---


def _out_bench(tmp_path, name, source, header, scenario):
    """Build a one-unit bench whose SDD is grounded and generate its suite."""
    return _ptr_bench(
        tmp_path, name, source, header,
        "# SDD\n- **FOO-001**: the SUT SHALL behave as documented.\n", [scenario])


def test_a_reassigned_multi_level_pointer_is_observed_through_the_argument(tmp_path):
    """F1 residual (owner-arbitrated HIGH, sixth review): ``expect_out`` asserts
    the value the pointer points at **after** the call, but for ``int **pp`` the
    emitter returned the pre-call backing local ``_v0`` as the observable. A SUT
    that *reassigns* ``*pp`` (``*pp = &replacement``) leaves ``_v0`` untouched, so
    the generated test asserted the stale 42 against a correct ``99`` and reported
    a fabricated ``IMPLEMENTATION_DEFECT``. The observable must be read **through
    the argument the callee received** (``*pp``). Fail-before: on the pre-fix
    emitter this source emitted ``TEST_ASSERT_EQUAL(99, out0_0_v0)``."""
    bench, out, source = _out_bench(
        tmp_path, "repoint",
        "static int replacement = 99;\n"
        "int repoint(int **pp) { *pp = &replacement; return **pp; }\n",
        "#ifndef FOO_H\n#define FOO_H\nint repoint(int **pp);\n#endif\n",
        {"id": "S1", "title": "t", "function": "repoint",
         "requirement_ids": ["FOO-001"], "mocks": [],
         "cases": [{"args": [42], "expected": 99, "expect_out": {"0": 99}}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    assert out.test_functions, out.limitations
    assert not out.limitations, out.limitations
    # The observable is read through the argument, not the stale backing local.
    assert "TEST_ASSERT_EQUAL(99, *out0_0_p1);" in source, source
    assert "TEST_ASSERT_EQUAL(99, out0_0_v0);" not in source, source


def test_a_write_through_multi_level_pointer_is_observed_through_the_argument(tmp_path):
    """Contrast for the residual fix: a SUT that writes *through* the pointer
    without reassigning it (``**pp = 99``) leaves ``*pp`` pointing at the same
    object, so both the old backing local and the argument read the same value.
    This guards a fix that reads the wrong object: the observable must still be
    the ultimate pointed-to scalar (here 99), never a pointer value."""
    bench, out, source = _out_bench(
        tmp_path, "write-thru",
        "int write_thru(int **pp) { **pp = 99; return **pp; }\n",
        "#ifndef FOO_H\n#define FOO_H\nint write_thru(int **pp);\n#endif\n",
        {"id": "S1", "title": "t", "function": "write_thru",
         "requirement_ids": ["FOO-001"], "mocks": [],
         "cases": [{"args": [42], "expected": 99, "expect_out": {"0": 99}}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    assert out.test_functions, out.limitations
    assert not out.limitations, out.limitations
    assert "TEST_ASSERT_EQUAL(99, *out0_0_p1);" in source, source


def test_a_three_level_pointer_reassignment_is_observed_at_its_depth(tmp_path):
    """Depth is not special-cased at 2: for ``int ***ppp`` the observable must be
    ``**ppp`` (two dereferences of the passed argument). Read through the argument
    so a reassignment at any level is still observed."""
    from mtm.gen import func_params, _pointer_arg

    ppp = func_params("int f(int ***ppp)")[0]
    lines, arg, obs = _pointer_arg(ppp, "o0", 42)
    assert arg == "&o0_p2" and obs == "**o0_p2", (arg, obs)

    bench, out, source = _out_bench(
        tmp_path, "three-level",
        "static int replacement = 99;\n"
        "int f(int ***ppp) { **ppp = &replacement; return ***ppp; }\n",
        "#ifndef FOO_H\n#define FOO_H\nint f(int ***ppp);\n#endif\n",
        {"id": "S1", "title": "t", "function": "f",
         "requirement_ids": ["FOO-001"], "mocks": [],
         "cases": [{"args": [42], "expected": 99, "expect_out": {"0": 99}}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    assert out.test_functions, out.limitations
    assert not out.limitations, out.limitations
    assert "TEST_ASSERT_EQUAL(99, **out0_0_p2);" in source, source


def test_the_depth_one_void_pointer_observable_is_the_backing_local(tmp_path):
    """A depth-1 ``void *`` has no intermediate pointer level to read, so its
    observable stays the int-backed ``_v0`` local (the parameter receives
    ``&_v0``, so ``_v0`` *is* the object the callee can write). The reassignment
    fix must not emit ``*arg`` here — that would not compile. This is the only
    void case that keeps the backing local; depth ≥ 2 is covered below."""
    from mtm.gen import func_params, _pointer_arg

    for sig in ("int f(void *p)", "int f(const void *p)"):
        _, _, obs = _pointer_arg(func_params(sig)[0], "o0", 7)
        assert obs == "o0_v0", (sig, obs)
    bench, out, source = _out_bench(
        tmp_path, "void-obs",
        "int f(void *p) { return *(int *)p; }\n",
        "#ifndef FOO_H\n#define FOO_H\nint f(void *p);\n#endif\n",
        {"id": "S1", "title": "t", "function": "f",
         "requirement_ids": ["FOO-001"], "mocks": [],
         "cases": [{"args": [7], "expected": 7, "expect_out": {"0": 7}}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    assert out.test_functions, out.limitations
    assert "TEST_ASSERT_EQUAL(7, out0_0_v0);" in source, source
    assert "*out0_0_v0" not in source, source


def test_a_reassigned_multilevel_void_pointer_is_observed_through_the_argument(tmp_path):
    """F1 residual, in-scope neighbour (reviewer + challenger): a ``void **``
    pointee at depth ≥ 2 previously kept the *pre-call* ``_v0`` observable — the
    same fabricated-defect class the depth-2 ``int **`` fix removes — because the
    fix branched the void case to ``_v0``. But the backing chain is ``int``-typed
    (``int *p1``), so ``*p1`` reads the reassigned int even though the parameter is
    cast to ``void **``: the observable must follow the same rule. (Only depth-1
    ``void *`` keeps ``_v0``.)"""
    from mtm.gen import func_params, _pointer_arg

    _, arg, obs = _pointer_arg(func_params("int f(void **pp)")[0], "o0", 7)
    assert obs == "*o0_p1" and arg == "(void * *)&o0_p1", (arg, obs)

    bench, out, source = _out_bench(
        tmp_path, "void-pp-reassign",
        "static int replacement = 99;\n"
        "int f(void **pp) { *pp = &replacement; return *(int *)*pp; }\n",
        "#ifndef FOO_H\n#define FOO_H\nint f(void **pp);\n#endif\n",
        {"id": "S1", "title": "t", "function": "f",
         "requirement_ids": ["FOO-001"], "mocks": [],
         "cases": [{"args": [42], "expected": 99, "expect_out": {"0": 99}}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    assert out.test_functions, out.limitations
    assert not out.limitations, out.limitations
    assert "TEST_ASSERT_EQUAL(99, *out0_0_p1);" in source, source
    assert "TEST_ASSERT_EQUAL(99, out0_0_v0);" not in source, source


def test_a_string_expect_out_is_refused_like_a_string_argument(tmp_path):
    """Same fabricated-assertion class as the F1 argument guard, on the
    ``expect_out`` path: a free-text value renders as a C string literal, which
    cannot be a scalar pointee — it emitted ``TEST_ASSERT_EQUAL("hi there",
    out0_0);`` with ``limitations == []``. Refuse it with a named limitation;
    numbers, grounded enum identifiers and the ``{"expr": …}`` hatch still work."""
    _, out_bad, _ = _out_bench(
        tmp_path, "str-expect",
        "int f(int *p) { (void)p; return 1; }\n",
        "#ifndef FOO_H\n#define FOO_H\nint f(int *p);\n#endif\n",
        {"id": "S1", "title": "t", "function": "f",
         "requirement_ids": ["FOO-001"], "mocks": [],
         "cases": [{"args": [5], "expected": 1, "expect_out": {"0": "hi there"}}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    assert out_bad.test_functions == [], out_bad.test_functions
    assert any("string for a scalar pointee" in l for l in out_bad.limitations), \
        out_bad.limitations
    # An explicit expression for the same pointee is still accepted.
    _, ok, _ = _out_bench(
        tmp_path, "expr-expect",
        "int f(int *p) { (void)p; return 1; }\n",
        "#ifndef FOO_H\n#define FOO_H\nint f(int *p);\n#endif\n",
        {"id": "S1", "title": "t", "function": "f",
         "requirement_ids": ["FOO-001"], "mocks": [],
         "cases": [{"args": [5], "expected": 1, "expect_out": {"0": {"expr": "kSentinel"}}}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    assert ok.test_functions, ok.limitations


def test_an_aggregate_expect_out_is_refused_not_emitted_uncompilable(tmp_path):
    """Same-class defect on the aggregate ``expect_out`` path: `_case_problem`
    *demands* the ``{"expr": …}`` escape hatch for an aggregate pointee, but the
    emitter rendered it with ``_literal`` — stringifying the Python dict to
    ``TEST_ASSERT_EQUAL("{'expr': '(S){7}'}", out0_0)``: an uncompilable assertion
    that drops the planned outcome, with ``limitations == []``. Switching to
    ``_expression`` alone only moves the failure: the single-assertion emitter
    emits ``TEST_ASSERT_EQUAL((S){7}, out0_0)``, which Unity maps to
    ``UNITY_TEST_ASSERT_EQUAL_INT`` and casts the aggregate to an integer —
    ``error: aggregate value used where an integer was expected`` (verified with
    the project's Unity). The plan documents the ``{"expr": …}`` hatch for
    ``args``/``mocks``, not for ``expect_out``, so there is no faithful rendering:
    refuse it with a named limitation instead of emitting uncompilable C that
    claims ``limitations == []``."""
    bench, out, _ = _out_bench(
        tmp_path, "agg-expect",
        "typedef struct { int v; } S;\nint f(S *p) { (void)p; return 1; }\n",
        "#ifndef FOO_H\n#define FOO_H\ntypedef struct { int v; } S;\nint f(S *p);\n#endif\n",
        {"id": "S1", "title": "t", "function": "f",
         "requirement_ids": ["FOO-001"], "mocks": [],
         "cases": [{"args": [{"expr": "(S){1}"}], "expected": 1,
                    "expect_out": {"0": {"expr": "(S){7}"}}}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    assert out.test_functions == [], out.test_functions
    assert any("aggregate" in l and "expect_out" in l for l in out.limitations), \
        out.limitations
    # A scalar expect_out on the same aggregate-arg scenario is unaffected.
    _, ok, _ = _out_bench(
        tmp_path, "agg-arg-scalar-out",
        "typedef struct { int v; } S;\nint f(S *p) { (void)p; return 1; }\n",
        "#ifndef FOO_H\n#define FOO_H\ntypedef struct { int v; } S;\nint f(S *p);\n#endif\n",
        {"id": "S1", "title": "t", "function": "f",
         "requirement_ids": ["FOO-001"], "mocks": [],
         "cases": [{"args": [{"expr": "(S){1}"}], "expected": 1,
                    "expect_out": {}}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    assert ok.test_functions, ok.limitations


def test_a_qualified_aggregate_pointee_scalar_is_refused_not_emitted_uncompilable(tmp_path):
    """End-to-end for the qualifier+alias class (PR #14 recheck #10).

    ``is_aggregate_base`` tested the raw base against the recorded aliases, so a
    *qualified* alias pointee (``const Sample *p``) was admitted with a scalar and
    emitted ``const Sample out0_0 = (const Sample)9;`` — ``conversion to non-scalar
    type requested`` under ``-Werror``, an opaque ``TOOL_FAILURE`` with
    ``limitations == []``. The bare ``Sample *`` form was already refused by the
    same predicate, so the two spellings must behave alike.
    """
    bench, out, source = _out_bench(
        tmp_path, "const-agg",
        "typedef struct { int v; } Sample;\n"
        "int f(const Sample *p) { return p->v; }\n",
        "#ifndef FOO_H\n#define FOO_H\ntypedef struct { int v; } Sample;\n"
        "int f(const Sample *p);\n#endif\n",
        {"id": "S1", "title": "t", "function": "f",
         "requirement_ids": ["FOO-001"], "mocks": [],
         "cases": [{"args": [9], "expected": 9}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    assert out.test_functions == [], out.test_functions
    assert any("aggregate" in l for l in out.limitations), out.limitations
    assert "const Sample" not in source  # nothing emitted an aggregate initialisation
    # The explicit-initializer hatch is still admitted (the case becomes generatable).
    _, ok, ok_src = _out_bench(
        tmp_path, "const-agg-expr",
        "typedef struct { int v; } Sample;\n"
        "int f(const Sample *p) { return p->v; }\n",
        "#ifndef FOO_H\n#define FOO_H\ntypedef struct { int v; } Sample;\n"
        "int f(const Sample *p);\n#endif\n",
        {"id": "S1", "title": "t", "function": "f",
         "requirement_ids": ["FOO-001"], "mocks": [],
         "cases": [{"args": [{"expr": "(Sample){7}"}], "expected": 7}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    assert ok.test_functions, ok.limitations
    assert "const Sample out0_0 = (const Sample)(Sample){7};" in ok_src, ok_src



@pytest.mark.docker
def test_real_image_observes_a_reassigned_multi_level_pointer(tmp_path, product_image):
    """AP-02 for the F1 residual: the generated ``int **`` and ``void **``
    reassignment benches must compile **and execute** in the product image with a
    meaningful oracle. Each SUT repoints ``*pp`` to a static object and returns the
    new value; the pre-fix emitter asserted the stale ``_v0`` (42), so these tests
    would have reported a fabricated defect instead of a pass. They assert the
    observed value is really read through the argument, not merely that the bench
    compiles."""
    from mtm.ceedling import run_ceedling

    cases = [
        ("repoint-int-pp", "int foo_a(int **pp) { *pp = &replacement; return **pp; }\n",
         "int foo_a(int **pp);\n"),
        ("repoint-void-pp",
         "int foo_a(void **pp) { *pp = &replacement; return *(int *)*pp; }\n",
         "int foo_a(void **pp);\n"),
    ]
    for name, source, decl in cases:
        bench, out, _ = _ptr_bench(
            tmp_path, name,
            "static int replacement = 99;\n" + source,
            f"#ifndef FOO_H\n#define FOO_H\n{decl}\n#endif\n",
            "# SDD\n- **FOO-001**: the SUT SHALL repoint pp and return the new value.\n",
            [{"id": "S1", "title": "t", "function": "foo_a",
              "requirement_ids": ["FOO-001"], "mocks": [],
              "cases": [{"args": [42], "expected": 99, "expect_out": {"0": 99}}],
              "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}])
        assert out.test_functions, (name, out.limitations)
        run = run_ceedling(bench, image_ref=product_image)
        assert run.ran, (name, run.diagnostics[-2000:])
        assert run.total == 1 and run.failed == 0, \
            (name, run.total, run.failed, run.stdout[-1500:])


def test_an_array_or_function_pointer_typedef_parameter_is_a_named_limitation(tmp_path):
    """F1-review (challenger, CONFIRMED MANDATORY): a parameter whose type is a
    ``typedef`` alias of an **array** or **function-pointer** type must be refused
    with a named limitation, never emitted as ``first((Arr)5);`` with
    ``limitations == []`` — C that does not compile (``error: cast specifies array
    type``, verified with gcc -Werror), i.e. an opaque ``TOOL_FAILURE``.

    Re-expressed to the clang contract (owner decision 0017): the ``opaque`` marker
    now comes from libclang facts, charted into the generator's type map by
    :func:`mtm.gen._chart_type_map` — not from the retired ``scan_typedefs``. The
    invariant asserted is unchanged: an unsupported alias shape is a named
    limitation before any verdict, never broken C with an empty limitation list.
    """
    from mtm.ctypes_clang import AliasFact, CTypeResolution
    from mtm.gen import _chart_type_map, _unsupported_param_shape, func_params

    facts = CTypeResolution(status="RESOLVED", aliases={
        "Arr": AliasFact("Arr", "h.h", 1, base="", depth=0, opaque=True),
        "CB": AliasFact("CB", "h.h", 2, base="", depth=0, opaque=True),
    })
    td = _chart_type_map(facts)
    assert td["Arr"].opaque and td["CB"].opaque, td
    # Used as a parameter type, each is refused (a named limitation), never emitted.
    assert _unsupported_param_shape(func_params("int first(Arr a)", td), td)
    assert _unsupported_param_shape(func_params("int first(CB cb)", td), td)

    header = ("#ifndef FOO_H\n#define FOO_H\n#include <stdint.h>\n"
              "typedef int Arr[4];\ntypedef void (*CB)(int);\n"
              "int first(Arr a);\n#endif\n")
    bench, out, _ = _ptr_bench(
        tmp_path, "arr-typedef",
        "int first(Arr a) { return a[0]; }\n", header,
        "# SDD\n- **FOO-001**: `first` SHALL take Arr.\n",
        [_one_scenario("first", [5], 5)])
    assert out.test_functions == [], out.test_functions
    assert any("typedef" in lim and "not supported" in lim for lim in out.limitations), \
        out.limitations


def _mock_bench(tmp_path, name, source, header, scenario, *, resolve=None):
    """Build a one-unit bench with a mocked dependency, then generate it."""
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = tmp_path / name
    bench.mkdir()
    (bench / "foo.c").write_text(source)
    (bench / "foo.h").write_text(header)
    (bench / "sdd.md").write_text("# SDD\n- **FOO-001**: the SUT SHALL behave.\n")
    run_mtm_json("init", str(bench))
    for n, kind in (("foo.c", "source"), ("foo.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", n, "--kind", kind, cwd=bench)
    inventory = build_inventory(bench, _scopes(bench))
    plan = parse_plan(json.dumps({"scenarios": [scenario]}), inventory,
                      LLMIdentity(provider="test", model="t"))
    return bench, generate_suite(bench, plan, inventory,
                                 resolve=resolve or _fake_resolve)


def test_a_mocked_dependency_through_a_pointer_typedef_is_refused(tmp_path):
    """F2-review (challenger, CONFIRMED MANDATORY): the mock path did not resolve a
    pointer ``typedef``, so ``typedef int *P; void dep_write(P *pp);`` (a real
    ``int **``) was reported at depth 1 and emitted ``P v = (P)5;`` plus a
    ``ReturnThruPtr`` at the wrong indirection — an unjustified pointer cast and
    uncompilable C. With the alias resolved the depth is 2, so the multi-level
    write-through is refused with the named limitation the SUT path already uses."""
    from mtm import ctypes_clang
    from mtm.gen import _chart_type_map, mock_controls, split_declaration

    td = _chart_type_map(ctypes_clang.CTypeResolution(
        status="RESOLVED",
        aliases={"P": ctypes_clang.AliasFact("P", "foo.h", 4, base="int", depth=1)}))
    ctrl = mock_controls("void dep_write(P *pp);", "dep_write", None, td)
    assert ctrl["out_depth"] == {"pp": 2}, ctrl
    split = split_declaration("void dep_write(P *pp);", td)
    assert split[1][0].depth == 2, split

    bench, out = _mock_bench(
        tmp_path, "ptr-typedef-mock",
        "int use(int a) { int v; dep_write(&v); return a; }\n",
        "#ifndef FOO_H\n#define FOO_H\n#include <stdint.h>\ntypedef int *P;\n"
        "void dep_write(P *pp);\nint use(int a);\n#endif\n",
        {"id": "S1", "title": "t", "function": "use", "requirement_ids": ["FOO-001"],
         "mocks": ["dep_write"],
         "cases": [{"args": [1], "expected": 1, "mocks": {"dep_write": {"pp": 5}}}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    assert out.test_functions == [], out.test_functions
    assert any("multi-level pointer" in lim for lim in out.limitations), out.limitations


def test_a_mocked_dependency_using_a_typedef_is_declared_in_its_interface_header(tmp_path):
    """F3-review (challenger, CONFIRMED MANDATORY): the private CMock interface
    header carried only the raw declaration, so ``void dep_write(P p);`` compiled
    with ``unknown type name 'P'`` (also for a plain alias like
    ``vendor_adc_sample_t``) and every mock scenario died as an opaque build
    failure. The header must now make the alias visible, so it compiles — the
    actual proof is a real gcc compile below.

    The alias is brought in by ``#include``-ing the copied header that defines it
    (one guarded definition per TU), *not* by re-declaring it: a duplicate alias
    the test's own unit header also defines is legal for a scalar/pointer typedef
    but a hard ``conflicting types`` error for an anonymous aggregate."""
    import shutil
    import subprocess

    bench, out = _mock_bench(
        tmp_path, "typedef-dep-header",
        "int use(int a) { dep_write(0); return a; }\n",
        "#ifndef FOO_H\n#define FOO_H\n#include <stdint.h>\ntypedef int *P;\n"
        "typedef unsigned short vendor_adc_sample_t;\n"
        "void dep_write(P p);\nint use(int a);\n#endif\n",
        {"id": "S1", "title": "t", "function": "use", "requirement_ids": ["FOO-001"],
         "mocks": ["dep_write"],
         "cases": [{"args": [1], "expected": 1, "mocks": {"dep_write": {"ret": 0}}}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    src_dir = bench / ".mtm" / "gen" / "src"
    header_path = bench / ".mtm" / "gen" / "interface" / "mtm_dep_dep_write.h"
    assert header_path.is_file()
    body = header_path.read_text()
    # The alias is brought in through the defining copied header, not re-declared.
    assert '#include "foo.h"' in body, body
    assert "typedef int *P;" not in body, body
    # The decisive proof: the header compiles on its own under -Werror with the
    # copied sources on the include path (so its `#include "foo.h"` resolves).
    # Compile a copy in an isolated temp dir so gcc's precompiled-header side file
    # never lands in the bench.
    if shutil.which("gcc"):
        comp = tmp_path / "compile"
        comp.mkdir()
        copy = comp / "hdr.c"
        copy.write_text(body)
        proc = subprocess.run(
            ["gcc", "-std=c99", "-Wall", "-Werror", "-I", str(src_dir),
             "-c", str(copy), "-o", str(comp / "hdr.o")],
            capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


def test_an_anonymous_aggregate_alias_is_shared_not_redeclared_in_the_interface_header(tmp_path):
    """Same-class contract as F3: the interface header must make a mocked
    dependency's alias visible **without** re-declaring it. Emitting the verbatim
    ``typedef struct { int v; } S;`` broke the TU that also includes the unit
    header, whose identical-looking alias names a *different* anonymous type —
    gcc: ``conflicting types for 'S'``. Sharing the one guarded definition via
    ``#include`` fixes it (verified mechanical fact: duplicate scalar/pointer
    typedefs compile, duplicate anonymous-aggregate typedefs do not)."""
    import shutil
    import subprocess

    bench, out = _mock_bench(
        tmp_path, "anon-agg-dep-header",
        "typedef struct { int v; } S;\n"
        "int use(int a) { S s = {0}; dep_send(&s); return a; }\n",
        "#ifndef FOO_H\n#define FOO_H\n#include <stdint.h>\ntypedef struct { int v; } S;\n"
        "void dep_send(S *s);\nint use(int a);\n#endif\n",
        {"id": "S1", "title": "t", "function": "use", "requirement_ids": ["FOO-001"],
         "mocks": ["dep_send"],
         "cases": [{"args": [1], "expected": 1,
                    "mocks": {"dep_send": {"s": {"expr": "(S){7}"}}}}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    src_dir = bench / ".mtm" / "gen" / "src"
    header_path = bench / ".mtm" / "gen" / "interface" / "mtm_dep_dep_send.h"
    body = header_path.read_text()
    assert '#include "foo.h"' in body, body
    assert "typedef struct" not in body, body
    # The regression that motivated this: the test TU includes both the mock's
    # interface header and the unit header. Emulating that inclusion order must
    # compile cleanly (it did not when the alias was re-declared).
    if shutil.which("gcc"):
        comp = tmp_path / "compile"
        comp.mkdir()
        tu = comp / "tu.c"
        tu.write_text('#include "mtm_dep_dep_send.h"\n#include "foo.h"\n'
                      "static S g;\nint main(void) { (void)g; return 0; }\n")
        iface_dir = bench / ".mtm" / "gen" / "interface"
        proc = subprocess.run(
            ["gcc", "-std=c99", "-Wall", "-Werror", "-I", str(src_dir),
             "-I", str(iface_dir), "-c", str(tu), "-o", str(comp / "tu.o")],
            capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


def test_a_guardless_aggregate_alias_header_is_a_named_limitation(tmp_path):
    """The include-based sharing needs the defining header's own guard to collapse
    the second inclusion. A header that defines an *anonymous-aggregate* alias
    **without** a guard cannot be shared: the test includes it both directly (as
    the unit header) and transitively (via the mock's interface header), and the
    repeat declaration is a hard ``conflicting types`` error. It must be refused
    with a precise limitation — not emitted as an opaque build failure whose
    ``limitations`` is empty (plan §C: record the limit before execution). A guard
    is what makes the ordinary case work; a guard-less *scalar* alias stays admitted
    because a duplicate scalar typedef is legal C."""
    bench, out = _mock_bench(
        tmp_path, "guardless-agg",
        "typedef struct { int v; } S;\n"
        "int use(int a) { S s = {0}; dep_send(&s); return a; }\n",
        "#include <stdint.h>\ntypedef struct { int v; } S;\n"  # deliberately no guard
        "void dep_send(S *s);\nint use(int a);\n",
        {"id": "S1", "title": "t", "function": "use", "requirement_ids": ["FOO-001"],
         "mocks": ["dep_send"],
         "cases": [{"args": [1], "expected": 1,
                    "mocks": {"dep_send": {"s": {"expr": "(S){7}"}}}}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    assert out.test_functions == [], out.test_functions
    assert any("cannot be included twice" in lim or "cannot be shared" in lim
                for lim in out.limitations), out.limitations
    # No interface header was written for the refused mock, so nothing dangling.
    assert not (bench / ".mtm" / "gen" / "interface" / "mtm_dep_dep_send.h").exists()
    # A guard-less *scalar* alias is still admitted (duplicate scalar typedef is OK).
    _, ok = _mock_bench(
        tmp_path, "guardless-scalar",
        "int use(int a) { dep_write((P)0); return a; }\n",
        "#include <stdint.h>\ntypedef int *P;\nvoid dep_write(P p);\nint use(int a);\n",
        {"id": "S1", "title": "t", "function": "use", "requirement_ids": ["FOO-001"],
         "mocks": ["dep_write"],
         "cases": [{"args": [1], "expected": 1, "mocks": {"dep_write": {}}}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    assert ok.test_functions, ok.limitations


def test_a_guardless_enum_alias_header_is_a_named_limitation(tmp_path):
    """The neighbouring shape of the guard-less aggregate case: an anonymous
    **enum** alias with an inline body is exactly as unsharable as an anonymous
    struct/union (a repeat names the enumerators twice — ``redeclaration of
    enumerator``). The classifier only recognised ``struct|union`` inline bodies,
    so the guard check missed it and emitted an interface header that ``#include``d
    the unguarded header — uncompilable C with ``limitations == []``. It must be a
    named limitation, like the struct/union case."""
    bench, out = _mock_bench(
        tmp_path, "guardless-enum",
        "typedef enum { E_A, E_B } e_t;\nint use(int a) { return a; }\n",
        "#include <stdint.h>\ntypedef enum { E_A, E_B } e_t;\n"  # deliberately no guard
        "void dep_send(e_t e);\nint use(int a);\n",
        {"id": "S1", "title": "t", "function": "use", "requirement_ids": ["FOO-001"],
         "mocks": ["dep_send"],
         "cases": [{"args": [1], "expected": 1, "mocks": {"dep_send": {}}}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    assert out.test_functions == [], out.test_functions
    assert any("cannot be included twice" in lim or "cannot be shared" in lim
                for lim in out.limitations), out.limitations
    assert not (bench / ".mtm" / "gen" / "interface" / "mtm_dep_dep_send.h").exists()
    # A *guarded* enum alias is shared via `#include` and must compile (a real
    # gcc compile of the include order the generated test uses).
    import shutil
    import subprocess

    bench2, ok = _mock_bench(
        tmp_path, "guarded-enum",
        "int use(int a) { dep_send(E_A); return a; }\n",
        "#ifndef FOO_H\n#define FOO_H\n#include <stdint.h>\n"
        "typedef enum { E_A, E_B } e_t;\nvoid dep_send(e_t e);\nint use(int a);\n#endif\n",
        {"id": "S1", "title": "t", "function": "use", "requirement_ids": ["FOO-001"],
         "mocks": ["dep_send"],
         "cases": [{"args": [1], "expected": 1, "mocks": {"dep_send": {}}}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    assert ok.test_functions, ok.limitations
    body = (bench2 / ".mtm" / "gen" / "interface" / "mtm_dep_dep_send.h").read_text()
    assert '#include "foo.h"' in body and "typedef enum" not in body, body
    if shutil.which("gcc"):
        comp = tmp_path / "compile-enum"
        comp.mkdir()
        tu = comp / "tu.c"
        tu.write_text('#include "mtm_dep_dep_send.h"\n#include "foo.h"\n'
                      "static e_t g;\nint main(void) { (void)g; return 0; }\n")
        proc = subprocess.run(
            ["gcc", "-std=c99", "-Wall", "-Werror",
             "-I", str(bench2 / ".mtm" / "gen" / "src"),
             "-I", str(bench2 / ".mtm" / "gen" / "interface"),
             "-c", str(tu), "-o", str(comp / "tu.o")],
            capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


def test_a_guardless_header_with_an_inline_tag_body_is_a_named_limitation(tmp_path):
    """The header-level invariant: it is the **defining header**, not the alias's
    own statement, that decides whether a double inclusion is safe. ``struct S { int
    v; }; typedef struct S S_t;`` — a *tag-reference* alias whose own statement has
    no ``{`` — is still unsharable when that ``struct`` body lives in the same
    guard-less header: the repeat is ``redefinition of 'struct S'``. Keying only on
    the alias statement's ``{`` regressed exactly this shape (an earlier revision's
    aggregate predicate caught it); the fix is a header-level check. A guarded
    header with the same content shares cleanly."""
    bench, out = _mock_bench(
        tmp_path, "guardless-tag-body",
        "struct S { int v; };\ntypedef struct S S_t;\nint use(int a) { return a; }\n",
        "#include <stdint.h>\nstruct S { int v; };\ntypedef struct S S_t;\n"  # no guard
        "void dep_send(S_t s);\nint use(int a);\n",
        {"id": "S1", "title": "t", "function": "use", "requirement_ids": ["FOO-001"],
         "mocks": ["dep_send"],
         "cases": [{"args": [1], "expected": 1, "mocks": {"dep_send": {}}}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    assert out.test_functions == [], out.test_functions
    assert any("cannot be included twice" in lim or "cannot be shared" in lim
                for lim in out.limitations), out.limitations
    assert not (bench / ".mtm" / "gen" / "interface" / "mtm_dep_dep_send.h").exists()
    # The guarded form shares and compiles.
    import shutil
    import subprocess

    bench2, ok = _mock_bench(
        tmp_path, "guarded-tag-body",
        "int use(int a) { dep_send((S_t){0}); return a; }\n",
        "#ifndef FOO_H\n#define FOO_H\n#include <stdint.h>\nstruct S { int v; };\n"
        "typedef struct S S_t;\nvoid dep_send(S_t s);\nint use(int a);\n#endif\n",
        {"id": "S1", "title": "t", "function": "use", "requirement_ids": ["FOO-001"],
         "mocks": ["dep_send"],
         "cases": [{"args": [1], "expected": 1, "mocks": {"dep_send": {}}}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    assert ok.test_functions, ok.limitations
    if shutil.which("gcc"):
        comp = tmp_path / "compile-tag"
        comp.mkdir()
        tu = comp / "tu.c"
        tu.write_text('#include "mtm_dep_dep_send.h"\n#include "foo.h"\n'
                      "static S_t g;\nint main(void) { (void)g; return 0; }\n")
        proc = subprocess.run(
            ["gcc", "-std=c99", "-Wall", "-Werror",
             "-I", str(bench2 / ".mtm" / "gen" / "src"),
             "-I", str(bench2 / ".mtm" / "gen" / "interface"),
             "-c", str(tu), "-o", str(comp / "tu.o")],
            capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


def test_an_enum_by_value_parameter_generates_with_empty_limitations(tmp_path):
    """I5 regression (TC-1): an **enum-typed by-value parameter** is a scalar and
    must generate — the regression (lot B charting clang's ``record.kind == ENUM``
    into ``aggregate``) refused it with "argument N points at an aggregate type".
    The generated call casts each argument to its declared enum type, which the
    real compiler accepts below."""
    bench, out, _ = _ptr_bench(
        tmp_path, "enum-by-value",
        '#include "foo.h"\n'
        "state_t transition(state_t s, event_t e) { return (e == EV_START) ? s : ST_STOPPED; }\n",
        "#ifndef FOO_H\n#define FOO_H\n"
        "typedef enum { ST_STOPPED, ST_RUNNING } state_t;\n"
        "typedef enum { EV_START, EV_STOP } event_t;\n"
        "state_t transition(state_t s, event_t e);\n#endif\n",
        "# SDD\n- **FOO-001**: `transition` SHALL keep its state on EV_START.\n",
        [_one_scenario("transition", ["ST_STOPPED", "EV_START"], "ST_STOPPED")])
    assert out.limitations == [], out.limitations
    assert out.test_functions, out.limitations
    body = (bench / ".mtm" / "gen" / "test" / "test_foo.c").read_text()
    assert "(state_t)ST_STOPPED" in body and "(event_t)EV_START" in body, body
    if shutil.which("gcc"):
        _compile_enum_call_shapes(
            bench, tmp_path / "enum-by-value-tu",
            "state_t call_shape(void) { return transition((state_t)ST_STOPPED,"
            " (event_t)EV_START); }")


def test_an_enum_pointer_out_parameter_generates_with_empty_limitations(tmp_path):
    """The ``enum *`` out-param half of TC-1: a pointer to a scalar enum must be
    materialised as ``status_t out0_0 = (status_t)…;`` and observed, never refused
    as an aggregate. The old scanner chased only ``struct``/``union``; an enum
    pointee is a scalar and was generatable at base ``755d27e``."""
    bench, out, _ = _ptr_bench(
        tmp_path, "enum-out-param",
        '#include "foo.h"\n'
        "int poll(sensor_status_t *out) { *out = SENSOR_OK; return 0; }\n",
        "#ifndef FOO_H\n#define FOO_H\n"
        "typedef enum { SENSOR_OK, SENSOR_ERR } sensor_status_t;\n"
        "int poll(sensor_status_t *out);\n#endif\n",
        "# SDD\n- **FOO-001**: `poll` SHALL report SENSOR_OK.\n",
        [{"id": "S1", "title": "t", "function": "poll", "requirement_ids": ["FOO-001"],
          "mocks": [],
          "cases": [{"args": ["SENSOR_OK"], "expected": 0,
                     "expect_out": {0: "SENSOR_OK"}}],
          "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}])
    assert out.limitations == [], out.limitations
    assert out.test_functions, out.limitations
    body = (bench / ".mtm" / "gen" / "test" / "test_foo.c").read_text()
    assert "sensor_status_t out0_0 = (sensor_status_t)SENSOR_OK;" in body, body
    assert "TEST_ASSERT_EQUAL(SENSOR_OK, out0_0);" in body, body
    if shutil.which("gcc"):
        _compile_enum_call_shapes(
            bench, tmp_path / "enum-out-param-tu",
            "int call_shape(void) { sensor_status_t out0_0 ="
            " (sensor_status_t)SENSOR_OK; return poll(&out0_0); }")


def _compile_enum_call_shapes(bench: Path, comp: Path, call_shape: str) -> None:
    """Compile *call_shape* — the exact cast/call shapes the generated test emits —
    against the generated header with the real gcc. ``unity.h`` is not available
    offline, so this compiles the C the generator produced (proving the cast to a
    scalar enum and the ``enum *`` pointer are valid C) rather than the test file
    verbatim; the full compile-and-run is the Docker-gated test below."""
    import subprocess

    comp.mkdir()
    tu = comp / "tu.c"
    tu.write_text(f'#include "foo.h"\n{call_shape}\n')
    proc = subprocess.run(
        ["gcc", "-std=c99", "-Wall", "-Werror",
         "-I", str(bench / ".mtm" / "gen" / "src"),
         "-c", str(tu), "-o", str(comp / "tu.o")],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_a_guard_like_block_not_enclosing_the_definition_is_still_unsafe(tmp_path):
    """F-A4-review (independent PRE_PR, CONFIRMED MANDATORY): the share-safety check
    must prove the inline body is *inside* an include guard, not merely that the
    header contains some ``#ifndef X``/``#define X`` pair. A **mid-file feature
    toggle** (``#ifndef FEATURE_X … #endif``) or a definition written **before** the
    real guard both leave the ``{…}`` repeated on a second inclusion, so such a
    header must be treated as unshareable: previously ``_header_has_guard`` returned
    True, the interface header ``#include``d it, and the generated test's TU failed
    with ``conflicting types for 'S'`` while ``limitations == []`` — uncompilable C,
    the shape §C forbids. Both shapes must now be refused with a named limitation."""
    for name, header in (
        ("feature-toggle", "#ifndef FEATURE_X\n#define FEATURE_X 0\n#endif\n"
                           "#include <stdint.h>\ntypedef struct { int v; } S;\n"
                           "void dep_send(S *s);\nint use(int a);\n"),
        ("def-before-guard", "#include <stdint.h>\ntypedef struct { int v; } S;\n"
                             "#ifndef FOO_H\n#define FOO_H\n"
                             "void dep_send(S *s);\nint use(int a);\n#endif\n"),
    ):
        bench, out = _mock_bench(
            tmp_path, name,
            "int use(int a) { S s = {0}; dep_send(&s); return a; }\n", header,
            {"id": "S1", "title": "t", "function": "use",
             "requirement_ids": ["FOO-001"], "mocks": ["dep_send"],
             "cases": [{"args": [1], "expected": 1,
                        "mocks": {"dep_send": {"s": {"expr": "(S){7}"}}}}],
             "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
        assert out.test_functions == [], (name, out.test_functions)
        assert any("cannot be included twice" in lim or "cannot be shared" in lim
                   for lim in out.limitations), (name, out.limitations)
        assert not (bench / ".mtm" / "gen" / "interface"
                    / "mtm_dep_dep_send.h").exists(), name


def test_a_pragma_once_header_with_an_inline_body_stays_shareable(tmp_path):
    """The complementary safe case the guard check must NOT false-refuse:
    ``#pragma once`` is the idiomatic, Ceedling-common way to make a header
    double-includable. Its inline-body alias can be shared through the interface
    header without a repeat defect, so its mock scenario must still be generated
    (previously ``_header_has_guard`` saw no ``#ifndef`` and refused it, dropping
    working coverage). Verified by a real compile of the generated include order."""
    import shutil
    import subprocess

    bench, out = _mock_bench(
        tmp_path, "pragma-once-body",
        "int use(int a) { S s = {0}; dep_send(&s); return a; }\n",
        "#pragma once\n#include <stdint.h>\ntypedef struct { int v; } S;\n"
        "void dep_send(S *s);\nint use(int a);\n",
        {"id": "S1", "title": "t", "function": "use", "requirement_ids": ["FOO-001"],
         "mocks": ["dep_send"],
         "cases": [{"args": [1], "expected": 1,
                    "mocks": {"dep_send": {"s": {"expr": "(S){7}"}}}}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    assert out.test_functions, out.limitations
    if shutil.which("gcc"):
        comp = tmp_path / "compile-pragma"
        comp.mkdir()
        tu = comp / "tu.c"
        tu.write_text('#include "mtm_dep_dep_send.h"\n#include "foo.h"\n'
                      "static S g;\nint main(void) { (void)g; return 0; }\n")
        proc = subprocess.run(
            ["gcc", "-std=c99", "-Wall", "-Werror",
             "-I", str(bench / ".mtm" / "gen" / "src"),
             "-I", str(bench / ".mtm" / "gen" / "interface"),
             "-c", str(tu), "-o", str(comp / "tu.o")],
            capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


def test_a_conditional_branch_of_the_guard_is_not_protection(tmp_path):
    """F-A5-review (independent PRE_PR, CONFIRMED MANDATORY): the guard check must
    reason over preprocessor *branches*, not over text spans. Two shapes make a
    **text-span** predicate read "guarded" while a second inclusion still repeats the
    ``{…}`` body:

    * the guard's ``#else``/``#elif`` branch repeats the inline body — taken on the
      second inclusion, when the guard macro is already defined;
    * the ``#define X`` is itself dead (nested under ``#if 0``), so the guard never
      collapses and the body is redefined on re-inclusion.

    Both previously emitted a test function with ``limitations == []`` and a
    ``conflicting types`` build failure — the shape §C forbids. They must now be
    refused with a named limitation."""
    for name, header in (
        ("else-branch", "#ifndef FOO_H\n#define FOO_H\n#include <stdint.h>\n"
                        "typedef struct { int v; } S;\n#else\n"
                        "typedef struct { int v; } S;\n#endif\n"
                        "void dep_send(S *s);\nint use(int a);\n"),
        ("dead-define", "#ifndef FOO_H\n#if 0\n#define FOO_H\n#endif\n"
                        "#include <stdint.h>\ntypedef struct { int v; } S;\n#endif\n"
                        "void dep_send(S *s);\nint use(int a);\n"),
    ):
        bench, out = _mock_bench(
            tmp_path, name,
            "int use(int a) { S s = {0}; dep_send(&s); return a; }\n", header,
            {"id": "S1", "title": "t", "function": "use",
             "requirement_ids": ["FOO-001"], "mocks": ["dep_send"],
             "cases": [{"args": [1], "expected": 1,
                        "mocks": {"dep_send": {"s": {"expr": "(S){7}"}}}}],
             "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
        assert out.test_functions == [], (name, out.test_functions)
        assert any("cannot be included twice" in lim or "cannot be shared" in lim
                   for lim in out.limitations), (name, out.limitations)
        assert not (bench / ".mtm" / "gen" / "interface"
                    / "mtm_dep_dep_send.h").exists(), name


def test_a_guarded_nested_conditional_body_still_shares(tmp_path):
    """The safe counterpart the conservative guard check must keep admitting: a
    definition inside an inner ``#if`` that is itself inside an unconditional guard is
    still protected (the outer guard collapses the re-inclusion), so its mock
    scenario must be generated and its include order compile."""
    import shutil
    import subprocess

    bench, out = _mock_bench(
        tmp_path, "nested-cond-body",
        "int use(int a) { S s = {0}; dep_send(&s); return a; }\n",
        "#ifndef FOO_H\n#define FOO_H\n#include <stdint.h>\n#if 1\n"
        "typedef struct { int v; } S;\n#endif\n"
        "void dep_send(S *s);\nint use(int a);\n#endif\n",
        {"id": "S1", "title": "t", "function": "use", "requirement_ids": ["FOO-001"],
         "mocks": ["dep_send"],
         "cases": [{"args": [1], "expected": 1,
                    "mocks": {"dep_send": {"s": {"expr": "(S){7}"}}}}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    assert out.test_functions, out.limitations
    if shutil.which("gcc"):
        comp = tmp_path / "compile-nested"
        comp.mkdir()
        tu = comp / "tu.c"
        tu.write_text('#include "mtm_dep_dep_send.h"\n#include "foo.h"\n'
                      "static S g;\nint main(void) { (void)g; return 0; }\n")
        proc = subprocess.run(
            ["gcc", "-std=c99", "-Wall", "-Werror",
             "-I", str(bench / ".mtm" / "gen" / "src"),
             "-I", str(bench / ".mtm" / "gen" / "interface"),
             "-c", str(tu), "-o", str(comp / "tu.o")],
            capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


def test_a_guard_macro_undefined_in_the_header_is_not_protection(tmp_path):
    """F-A6-review (independent PRE_PR, CONFIRMED MANDATORY): the guard only collapses
    a re-inclusion while its macro stays **defined**. An ``#undef X`` anywhere in the
    header — inside the guarded region before ``#endif``, or after it, even nested
    under an ``#if`` — leaves ``X`` undefined when the header is included again, so the
    second inclusion re-enters ``#ifndef X`` and repeats the ``{…}`` body. Such a
    header must be refused with a named limitation, not shared: previously it read as
    safe and the generated TU failed ``redefinition of 'struct…'`` with
    ``limitations == []``."""
    for name, header in (
        ("undef-in-guard", "#ifndef FOO_H\n#define FOO_H\n#include <stdint.h>\n"
                           "typedef struct { int v; } S;\n#undef FOO_H\n#endif\n"
                           "void dep_send(S *s);\nint use(int a);\n"),
        ("undef-after-endif", "#ifndef FOO_H\n#define FOO_H\n#include <stdint.h>\n"
                              "typedef struct { int v; } S;\n#endif\n#undef FOO_H\n"
                              "void dep_send(S *s);\nint use(int a);\n"),
    ):
        bench, out = _mock_bench(
            tmp_path, name,
            "int use(int a) { S s = {0}; dep_send(&s); return a; }\n", header,
            {"id": "S1", "title": "t", "function": "use",
             "requirement_ids": ["FOO-001"], "mocks": ["dep_send"],
             "cases": [{"args": [1], "expected": 1,
                        "mocks": {"dep_send": {"s": {"expr": "(S){7}"}}}}],
             "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
        assert out.test_functions == [], (name, out.test_functions)
        assert any("cannot be included twice" in lim or "cannot be shared" in lim
                   for lim in out.limitations), (name, out.limitations)
        assert not (bench / ".mtm" / "gen" / "interface"
                    / "mtm_dep_dep_send.h").exists(), name


def test_an_unrelated_undef_does_not_refuse_a_guarded_header(tmp_path):
    """Control for F-A6: an ``#undef`` of a macro *other than* the guard macro does not
    defeat the guard, so the header stays shareable and its scenario is generated."""
    import shutil
    import subprocess

    bench, out = _mock_bench(
        tmp_path, "unrelated-undef",
        "int use(int a) { S s = {0}; dep_send(&s); return a; }\n",
        "#ifndef FOO_H\n#define FOO_H\n#include <stdint.h>\n#undef OTHER\n"
        "typedef struct { int v; } S;\nvoid dep_send(S *s);\nint use(int a);\n#endif\n",
        {"id": "S1", "title": "t", "function": "use", "requirement_ids": ["FOO-001"],
         "mocks": ["dep_send"],
         "cases": [{"args": [1], "expected": 1,
                    "mocks": {"dep_send": {"s": {"expr": "(S){7}"}}}}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    assert out.test_functions, out.limitations
    if shutil.which("gcc"):
        comp = tmp_path / "compile-undef-other"
        comp.mkdir()
        tu = comp / "tu.c"
        tu.write_text('#include "mtm_dep_dep_send.h"\n#include "foo.h"\n'
                      "static S g;\nint main(void) { (void)g; return 0; }\n")
        proc = subprocess.run(
            ["gcc", "-std=c99", "-Wall", "-Werror",
             "-I", str(bench / ".mtm" / "gen" / "src"),
             "-I", str(bench / ".mtm" / "gen" / "interface"),
             "-c", str(tu), "-o", str(comp / "tu.o")],
            capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


def test_an_alias_defined_only_in_a_c_is_carried_verbatim(tmp_path):
    """An alias with no *header* that defines it has no includable definition, so
    the interface header must spell it verbatim. This exercises the fallback path
    (and guards a self-introduced ``KeyError``: the guard check must not index the
    alias→header map for an alias that has no defining header)."""
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan
    import yaml

    bench = tmp_path / "c-defined-alias"
    bench.mkdir()
    # `Q` is defined only in the .c; `foo.h` is valid C that does not reference it.
    (bench / "foo.c").write_text(
        "typedef unsigned short Q;\n"
        "int use(int a) { return a ? 1 : 2; }\n")
    (bench / "foo.h").write_text(
        "#ifndef FOO_H\n#define FOO_H\nint use(int a);\n#endif\n")
    (bench / "sdd.md").write_text(
        "# SDD\n- **FOO-001**: `use` returns 1 for a nonzero argument.\n")
    run_mtm_json("init", str(bench))
    for n, kind in (("foo.c", "source"), ("foo.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", n, "--kind", kind, cwd=bench)
    inventory = build_inventory(
        bench, yaml.safe_load((bench / "project.yaml").read_text()).get("scopes", []))
    scenario = {
        "id": "S1", "title": "t", "function": "use", "requirement_ids": ["FOO-001"],
        "mocks": ["dep_write"],
        "mock_declarations": {"dep_write": "int dep_write(Q q);"},
        "cases": [{"args": [5], "expected": 1, "mocks": {"dep_write": {}}}],
        "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}
    plan = parse_plan(json.dumps({"scenarios": [scenario]}), inventory,
                      LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory, resolve=_fake_resolve)  # must not raise
    assert out.test_functions, out.limitations
    body = (bench / ".mtm" / "gen" / "interface" / "mtm_dep_dep_write.h").read_text()
    assert "typedef unsigned short Q;" in body, body


def _llm_fallback_bench(tmp_path, name, field, *, header_extra=""):
    """A bench whose header declares only the SUT, plus an LLM mock field.

    Returns ``(bench, out)`` from ``generate_suite``. ``dep_write`` is declared in
    **no** selected file, so the plan's ``mock_declarations`` field is the only
    authority for it — the LLM-fallback path.
    """
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan
    import yaml

    bench = tmp_path / name
    bench.mkdir()
    (bench / "foo.c").write_text("int use(int a) { return a ? 1 : 2; }\n")
    (bench / "foo.h").write_text(
        f"#ifndef FOO_H\n#define FOO_H\n{header_extra}int use(int a);\n#endif\n")
    (bench / "sdd.md").write_text("# SDD\n- **FOO-001**: `use` returns 1.\n")
    run_mtm_json("init", str(bench))
    for n, kind in (("foo.c", "source"), ("foo.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", n, "--kind", kind, cwd=bench)
    inventory = build_inventory(
        bench, yaml.safe_load((bench / "project.yaml").read_text()).get("scopes", []))
    scenario = {
        "id": "S1", "title": "t", "function": "use", "requirement_ids": ["FOO-001"],
        "mocks": ["dep_write"],
        "mock_declarations": {"dep_write": field},
        "cases": [{"args": [5], "expected": 1, "mocks": {"dep_write": {}}}],
        "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}
    plan = parse_plan(json.dumps({"scenarios": [scenario]}), inventory,
                      LLMIdentity(provider="test", model="t"))
    return bench, generate_suite(bench, plan, inventory, resolve=_fake_resolve)


def test_a_typedef_carried_in_an_llm_mock_field_travels_with_its_prototype(tmp_path):
    """TC-2 (MANDATORY): the LLM-fallback prototype must not name an undefined alias.

    A ``mock_declarations`` field may pair a ``typedef`` with the prototype that
    uses it (``typedef unsigned short Q;\\nint dep_write(Q q);``) while **no**
    selected file defines ``Q``. The pre-lot-D generator passed the whole field
    through and carried the alias; the lot-D LLM path kept only the prototype,
    emitted ``int dep_write(Q q);`` with ``Q`` undefined, ran the scenario and
    reported ``limitations: []`` — uncompilable C behind a **false success**
    (I1/AP-06). The alias must be carried with the prototype or a named limitation
    recorded, never a body-less alias with empty limitations.
    """
    import shutil
    import subprocess

    bench, out = _llm_fallback_bench(
        tmp_path, "tc2-typedef-in-field",
        "typedef unsigned short Q;\nint dep_write(Q q);")
    # Honest either way: the alias is defined in the interface header, OR the mock
    # was refused with a named limitation. Never a declaration naming an undefined
    # alias behind empty limitations.
    if out.test_functions:
        body = (bench / ".mtm" / "gen" / "interface"
                / "mtm_dep_dep_write.h").read_text()
        assert "typedef unsigned short Q;" in body, body
    else:
        assert any("has no usable declaration" in lim for lim in out.limitations), \
            out.limitations
    # The decisive proof: a TU that includes the interface header then the unit
    # header (the order the generated test uses) must compile — it did not when `Q`
    # was left undefined.
    if out.test_functions and shutil.which("gcc"):
        comp = tmp_path / "compile-tc2"
        comp.mkdir()
        tu = comp / "tu.c"
        tu.write_text('#include "mtm_dep_dep_write.h"\n#include "foo.h"\n'
                      "int main(void) { return 0; }\n")
        proc = subprocess.run(
            ["gcc", "-std=c99", "-Wall", "-Werror",
             "-I", str(bench / ".mtm" / "gen" / "src"),
             "-I", str(bench / ".mtm" / "gen" / "interface"),
             "-c", str(tu), "-o", str(comp / "tu.o")],
            capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


def test_a_typedef_carried_in_an_llm_field_uses_the_alias_machinery_not_a_duplicate(tmp_path):
    """Contrast for TC-2: when the selected build context *does* define the alias,
    the interface header `#include`s its real defining header instead of
    re-declaring it — the fix reuses the existing alias machinery, it does not
    force a verbatim duplicate that would fight the compiler over the alias."""
    bench, out = _llm_fallback_bench(
        tmp_path, "tc2-alias-also-real",
        "typedef unsigned short Q;\nint dep_write(Q q);",
        header_extra="typedef unsigned short Q;\n")
    assert out.test_functions, out.limitations
    body = (bench / ".mtm" / "gen" / "interface" / "mtm_dep_dep_write.h").read_text()
    assert '#include "foo.h"' in body, body
    assert "typedef unsigned short Q;" not in body, body


def test_a_chained_typedef_in_an_llm_field_is_carried_whole(tmp_path):
    """Neighbouring shape of TC-2: a field may define a chain
    (``typedef int *P; typedef P *Q; int dep_write(Q q);``). Both statements must
    be carried, or ``Q`` names a type whose ``P`` base is undefined."""
    import shutil
    import subprocess

    bench, out = _llm_fallback_bench(
        tmp_path, "tc2-chained",
        "typedef int *P;\ntypedef P *Q;\nint dep_write(Q q);")
    assert out.test_functions, out.limitations
    body = (bench / ".mtm" / "gen" / "interface" / "mtm_dep_dep_write.h").read_text()
    assert "typedef int *P;" in body and "typedef P *Q;" in body, body
    if shutil.which("gcc"):
        comp = tmp_path / "compile-tc2-chain"
        comp.mkdir()
        tu = comp / "tu.c"
        tu.write_text('#include "mtm_dep_dep_write.h"\n#include "foo.h"\n'
                      "int main(void) { return 0; }\n")
        proc = subprocess.run(
            ["gcc", "-std=c99", "-Wall", "-Werror",
             "-I", str(bench / ".mtm" / "gen" / "src"),
             "-I", str(bench / ".mtm" / "gen" / "interface"),
             "-c", str(tu), "-o", str(comp / "tu.o")],
            capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


def test_an_llm_prototype_naming_an_undefined_type_is_a_named_limitation(tmp_path):
    """TC-2 neighbor (MANDATORY): no companion `typedef`, the alias defined nowhere.

    The same I1/AP-06 defect as TC-2 through the other route: a field that names an
    alias but never defines it (``int dep_write(Q q);``) with **no** selected
    definition and no companion ``typedef``. Pre-existing since at least base
    ``8a640be``, it emitted ``int dep_write(Q q);`` with ``Q`` undefined and ran the
    scenario behind ``limitations: []`` — uncompilable C as a false success. The
    declaration must be refused with a precise per-symbol limitation naming the
    symbol (and the compiler diagnostic naming the undefined type); no test function
    and no body-less interface header. The gate is the **compiler** (AP-08), not a
    lexical guard.
    """
    bench, out = _llm_fallback_bench(
        tmp_path, "tc2-bare-undefined",
        "int dep_write(Q q);")
    assert out.test_functions == [], out.test_functions
    assert any("dep_write" in lim and "interface header does not compile" in lim
               and "Q" in lim for lim in out.limitations), out.limitations
    # Nothing dangling: no interface header was emitted for the refused mock.
    assert not (bench / ".mtm" / "gen" / "interface" / "mtm_dep_dep_write.h").exists()


def test_llm_prototypes_using_only_standard_and_real_types_still_emit(tmp_path):
    """Contrast for the undefined-type refusal: the guard must reject **only** a
    genuinely undefined named type, never a primitive, a pointer, a `<stdint.h>`
    scalar (the interface header always includes `<stdint.h>`) or a parameter's own
    name. Over-refusing here would shrink the verified domain."""
    import shutil
    import subprocess

    bench, out = _llm_fallback_bench(
        tmp_path, "standard-types-ok",
        "int dep_write(uint8_t channel, const uint16_t *samples, void *ctx);")
    assert out.test_functions, out.limitations
    body = (bench / ".mtm" / "gen" / "interface" / "mtm_dep_dep_write.h").read_text()
    assert "int dep_write(uint8_t channel, const uint16_t *samples" in body, body
    if shutil.which("gcc"):
        comp = tmp_path / "compile-std-types"
        comp.mkdir()
        tu = comp / "tu.c"
        tu.write_text('#include "mtm_dep_dep_write.h"\nint main(void) { return 0; }\n')
        proc = subprocess.run(
            ["gcc", "-std=c99", "-Wall", "-Werror",
             "-I", str(bench / ".mtm" / "gen" / "src"),
             "-I", str(bench / ".mtm" / "gen" / "interface"),
             "-c", str(tu), "-o", str(comp / "tu.o")],
            capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


def test_a_struct_or_enum_tag_in_an_llm_prototype_is_not_an_undefined_type(tmp_path):
    """Neighboring acceptance: a ``struct``/``union``/``enum`` *tag* names an
    aggregate by identity, not an alias that must be defined; the guard must not
    read the tag as an undefined named type. (The tag's own completeness is the
    SUT's concern — the same declaration the selected header would supply.)"""
    bench, out = _llm_fallback_bench(
        tmp_path, "tag-ok",
        "int dep_write(struct Foo *p, enum Bar e);")
    assert out.test_functions, out.limitations


def test_an_undefined_alias_in_an_llm_return_type_is_also_refused(tmp_path):
    """Neighboring shape: the undefined type may be the **return** type, not a
    parameter. The compiler gate covers both spellings, or the same uncompilable-C
    false success returns through the return position."""
    bench, out = _llm_fallback_bench(
        tmp_path, "tc2-undefined-return",
        "Q dep_write(int a);")
    assert out.test_functions == [], out.test_functions
    assert any("interface header does not compile" in lim and "Q" in lim
               for lim in out.limitations), out.limitations


def test_a_field_typedef_resting_on_an_undefined_base_is_refused(tmp_path):
    """Same class one level down: the field's own companion ``typedef`` may rest on
    a type nothing defines (``typedef P *Q;`` with ``P`` undefined), so carrying it
    verbatim would emit ``typedef P *Q;`` — uncompilable C — behind empty
    limitations. The compiler gate sees the whole proposed header (field statements
    included), not just the prototype's own spellings."""
    bench, out = _llm_fallback_bench(
        tmp_path, "tc2-field-base-undefined",
        "typedef P *Q;\nint dep_write(Q q);")
    assert out.test_functions == [], out.test_functions
    assert any("interface header does not compile" in lim and "P" in lim
               for lim in out.limitations), out.limitations
    assert not (bench / ".mtm" / "gen" / "interface" / "mtm_dep_dep_write.h").exists()


def test_a_field_typedef_with_a_defined_chain_is_still_carried(tmp_path):
    """Contrast: a self-contained chain in the field (``typedef int *P; typedef P
    *Q;``) has no undefined base and must still emit and compile — the base check
    must not over-refuse a legitimate chained alias."""
    import shutil
    import subprocess

    bench, out = _llm_fallback_bench(
        tmp_path, "tc2-chain-defined",
        "typedef int *P;\ntypedef P *Q;\nint dep_write(Q q);")
    assert out.test_functions, out.limitations
    if shutil.which("gcc"):
        comp = tmp_path / "compile-chain-defined"
        comp.mkdir()
        tu = comp / "tu.c"
        tu.write_text('#include "mtm_dep_dep_write.h"\nint main(void) { return 0; }\n')
        proc = subprocess.run(
            ["gcc", "-std=c99", "-Wall", "-Werror",
             "-I", str(bench / ".mtm" / "gen" / "src"),
             "-I", str(bench / ".mtm" / "gen" / "interface"),
             "-c", str(tu), "-o", str(comp / "tu.o")],
            capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


# --- AP-08: the compiler gates every emitted interface header (lot F) ---------
#
# The retired lexical guard `_undefined_type_in_decl` leaked four times (TC-2, V2,
# a nested-array typedef, V3) because it re-derived C semantics from text. The
# gate is now the compiler: every proposed header body rides ONE batched
# `probe_headers` request on the same resolution call, and is written only when
# clang (offline: gcc, via the test double) says it compiles in the selected
# context. These tests fix the V3 reproduction, its same-class neighbours, and the
# discriminating positive control against over-refusal.


def test_v3_an_alias_resting_on_a_selected_but_incomplete_typedef_is_refused(tmp_path):
    """V3 (MANDATORY, the leak that motivated lot F): the selected header defines
    ``typedef unsigned short Q;`` but the plan's field adds ``typedef Q Q2;`` and
    uses ``Q2``. The retired lexical guard saw the field-carried statement's base
    ``Q`` as known (it *is* a selected alias) and admitted the header; the emitted
    ``typedef Q Q2;`` then failed gcc with ``unknown type name 'Q'`` behind
    ``limitations: []`` — uncompilable C as a false success. The compiler gate must
    refuse it with a precise per-symbol limitation and emit no body-less header."""
    bench, out = _llm_fallback_bench(
        tmp_path, "v3-incomplete-typedef",
        "typedef Q Q2;\nint dep_write(Q2 q);",
        header_extra="typedef unsigned short Q;\n")
    assert out.test_functions == [], out.test_functions
    assert any("dep_write" in lim and "interface header does not compile" in lim
               for lim in out.limitations), out.limitations
    assert not (bench / ".mtm" / "gen" / "interface" / "mtm_dep_dep_write.h").exists()


def _generated_header_compiles(bench: Path, tmp_path: Path, name: str) -> bool:
    """Compile the interface header (then the unit header, the test's order)."""
    import shutil
    import subprocess
    if not shutil.which("gcc"):
        return True
    comp = tmp_path / f"compile-{name}"
    comp.mkdir()
    tu = comp / "tu.c"
    tu.write_text(f'#include "mtm_dep_{name}.h"\n#include "foo.h"\n'
                  "int main(void) { return 0; }\n")
    proc = subprocess.run(
        ["gcc", "-std=c99", "-Wall", "-Werror",
         "-I", str(bench / ".mtm" / "gen" / "src"),
         "-I", str(bench / ".mtm" / "gen" / "interface"),
         "-c", str(tu), "-o", str(comp / "tu.o")],
        capture_output=True, text=True)
    return proc.returncode == 0


@pytest.mark.parametrize("field", [
    "typedef struct { Undef u; } S;\nint dep_write(S s);",   # nested-record typedef
    "typedef Undef Arr[4];\nint dep_write(Arr a);",          # array typedef
    "int dep_write(Undef u);",                               # bare alias (V2 shape)
])
def test_lot_f_neighbours_of_the_v3_class_are_refused_or_compile(tmp_path, field):
    """Same-class neighbours (MANDATORY): a nested ``typedef struct { Undef u; }``,
    an array typedef ``typedef Undef Arr[4];`` and a bare undefined alias (the V2
    shape) must each yield a **compiling header** or a **named limitation** —
    never a body-less header beside ``limitations: []``. The compiler gate covers
    all four shapes the lexical guard leaked on."""
    bench, out = _llm_fallback_bench(tmp_path, f"neighbour-{abs(hash(field))}", field)
    hdr = bench / ".mtm" / "gen" / "interface" / "mtm_dep_dep_write.h"
    if out.test_functions:
        assert hdr.exists() and _generated_header_compiles(bench, tmp_path, "dep_write"), \
            (out.limitations, hdr.read_text() if hdr.exists() else "")
    else:
        assert any("interface header does not compile" in lim and "dep_write" in lim
                   for lim in out.limitations), out.limitations
        assert not hdr.exists()


def test_lot_f_positive_control_a_selected_alias_is_admitted_and_compiles(tmp_path):
    """Discriminating POSITIVE control (MANDATORY, guard against over-refusal): a
    legitimate declaration whose alias IS defined by a selected header must still be
    admitted and its header compile. ``typedef unsigned short Q;`` in ``foo.h`` plus
    an LLM prototype ``int dep_write(Q q);`` is resolvable — refusing it would shrink
    the verified domain. The probe must pass a good header, not just fail bad ones."""
    bench, out = _llm_fallback_bench(
        tmp_path, "lotf-positive",
        "int dep_write(Q q);",
        header_extra="typedef unsigned short Q;\n")
    assert out.test_functions, out.limitations
    hdr = bench / ".mtm" / "gen" / "interface" / "mtm_dep_dep_write.h"
    assert hdr.exists()
    assert '#include "foo.h"' in hdr.read_text()
    assert _generated_header_compiles(bench, tmp_path, "dep_write")


def test_lot_f_probes_all_proposed_headers_in_one_batched_request(tmp_path):
    """AP-08 / efficiency: the compiler verdict rides the **same** resolution call —
    ONE ``probe_headers`` request carrying every proposed body, not one docker
    invocation per symbol."""
    import yaml
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    seen: list = []

    def recording(paths, **kwargs):
        seen.append(dict(kwargs.get("probe_headers") or {}))
        return _fake_resolve(paths, **kwargs)

    bench = tmp_path / "lotf-batched"
    bench.mkdir()
    (bench / "foo.c").write_text(
        "int use(int a) { dep_write(0); dep_other(0); return a ? 1 : 2; }\n")
    (bench / "foo.h").write_text("#ifndef FOO_H\n#define FOO_H\nint use(int a);\n#endif\n")
    (bench / "sdd.md").write_text("# SDD\n- **FOO-001**: `use` returns 1.\n")
    run_mtm_json("init", str(bench))
    for n, kind in (("foo.c", "source"), ("foo.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", n, "--kind", kind, cwd=bench)
    inventory = build_inventory(
        bench, yaml.safe_load((bench / "project.yaml").read_text()).get("scopes", []))
    scenario = {
        "id": "S1", "title": "t", "function": "use", "requirement_ids": ["FOO-001"],
        "mocks": ["dep_write", "dep_other"],
        "mock_declarations": {"dep_write": "int dep_write(int a);",
                              "dep_other": "int dep_other(int b);"},
        "cases": [{"args": [5], "expected": 1,
                   "mocks": {"dep_write": {"ret": 0}, "dep_other": {"ret": 0}}}],
        "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}
    plan = parse_plan(json.dumps({"scenarios": [scenario]}), inventory,
                      LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory, resolve=recording)
    assert out.test_functions, out.limitations
    # Exactly one call carried a batched probe, and it proposed BOTH headers.
    probe_calls = [p for p in seen if p]
    assert len(probe_calls) == 1, seen
    assert len(probe_calls[0]) == 2, probe_calls[0]
    assert {Path(name).name for name in probe_calls[0]} == {
        "mtm_dep_dep_write.h", "mtm_dep_dep_other.h"}, probe_calls[0]


def test_lot_f_a_tool_fault_while_compiling_a_proposed_header_is_a_run_level_fault(
        fixture_bench):
    """I4 preserved: if the adapter that compiles the proposed headers is itself
    broken, that is the existing run-level ``TOOL_FAILURE`` (exit 30) — the probe
    adds no second fault path, and no header is emitted on an unverified guess."""
    from mtm import ctypes_clang
    from mtm.generate import run_generate
    from mtm.llm import MockProvider

    def broken(paths, **kwargs):
        # The resolve call itself is fine, but the probe request fails as a tool
        # fault — the adapter cannot be used to compile the headers.
        if kwargs.get("probe_headers"):
            return ctypes_clang.CTypeResolution(
                status=ctypes_clang.TOOL_FAILED,
                message="libclang could not be used: ModuleNotFoundError: clang")
        return ctypes_clang.CTypeResolution(status=ctypes_clang.RESOLVED,
                                            clang_version="test-double")

    bench = fixture_bench("mocked-sensor")

    def runner(*a, **k):
        raise AssertionError("the suite must not be executed on a resolver fault")

    result = run_generate(bench, _scopes(bench), provider=MockProvider(),
                          image="img:test", runner=runner, resolve=broken)
    assert result.status == "TOOL_FAILURE" and result.exit_code == 30
    assert result.smoke["ran"] is False
    assert result.findings == []  # never a SUT defect


# --- AP-08 / IMP-18: a real declaration is authoritative over an LLM proposal --


#: The exact IMP-18 divergent field: it carries BOTH a `typedef` and a prototype,
#: and the prototype's return type contradicts the real selected `sensor.h`
#: (`int platform_read_sensor(uint16_t *reading);`).
_IMP18_DIVERGENT = (
    "typedef enum { SENSOR_OK=0, SENSOR_ERROR=1, SENSOR_OUT_OF_RANGE=2 } sensor_status_t;\n"
    "sensor_status_t platform_read_sensor(uint16_t *reading);"
)


def _imp18_scenarios() -> list[dict]:
    divergent = _IMP18_DIVERGENT
    def scen(sid, req, expected, mocks):
        return {"id": sid, "title": "t", "function": "sample_sensor",
                "requirement_ids": [req], "mocks": ["platform_read_sensor"],
                "mock_declarations": {"platform_read_sensor": divergent},
                "cases": [{"args": [0], "expected": expected,
                           "mocks": {"platform_read_sensor": mocks},
                           "expect_out": {}, "expect_calls": {"platform_read_sensor": 1}}],
                "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}
    return [scen("S1", "SEN-002", "SENSOR_ERROR", {"ret": 1, "reading": 0}),
            scen("S2", "SEN-003", "SENSOR_OK", {"ret": 0, "reading": 500}),
            scen("S3", "SEN-004", "SENSOR_OUT_OF_RANGE", {"ret": 0, "reading": 5000})]


def test_a_real_selected_prototype_beats_a_divergent_llm_declaration(tmp_path):
    """AP-08 / IMP-18: an LLM `mock_declarations` entry never overrides a real
    declaration resolved from the selected build context.

    IMP-18's reproduction: the header declares ``int platform_read_sensor(uint16_t
    *reading);`` while the plan's field proposed ``sensor_status_t …`` (and carried a
    ``typedef`` too). The default (LLM-first) order produced ``INCOMPLETE`` with five
    "no usable declaration" limitations and ``generated=0``. The real ``int``
    prototype must win, the three scenarios must materialise, and no limitation may
    remain.
    """
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = tmp_path / "imp18"
    bench.mkdir()
    source = (EXAMPLES / "mocked-sensor" / "sensor.c").read_text()
    header = (EXAMPLES / "mocked-sensor" / "sensor.h").read_text()
    (bench / "sensor.c").write_text(source)
    (bench / "sensor.h").write_text(header)
    (bench / "sdd.md").write_text((EXAMPLES / "mocked-sensor" / "sdd.md").read_text())
    run_mtm_json("init", str(bench))
    for n, kind in (("sensor.c", "source"), ("sensor.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", n, "--kind", kind, cwd=bench)
    inventory = build_inventory(bench, _scopes(bench))
    plan = parse_plan(json.dumps({"scenarios": _imp18_scenarios()}), inventory,
                      LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory, resolve=_fake_resolve)

    # The three scenarios materialise; the LLM's contradiction leaves no limitation.
    assert len(out.test_functions) == 3, out.limitations
    assert out.limitations == [], out.limitations
    body = (bench / ".mtm" / "gen" / "interface"
            / "mtm_dep_platform_read_sensor.h").read_text()
    assert "int platform_read_sensor(uint16_t *reading);" in body, body
    assert "sensor_status_t platform_read_sensor" not in body, body
    # Provenance: the declaration came from the real selected context, not the LLM.
    assert out.types["declaration_sources"]["platform_read_sensor"] in (
        "clang", "selected-header"), out.types


def test_an_llm_declaration_is_used_only_when_no_real_declaration_exists(tmp_path):
    """Contrast (the mandated fallback): with **no** declaration in the selected
    context, an unambiguous LLM prototype is used — never invented. Here the header
    does not mention ``dep_write`` at all, so clang resolves nothing for it and the
    plan's single-prototype field stands."""
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan
    import yaml

    bench = tmp_path / "llm-fallback"
    bench.mkdir()
    (bench / "foo.c").write_text("int use(int a) { return dep_write(a); }\n")
    # `foo.h` declares only the SUT — `dep_write` is nowhere real.
    (bench / "foo.h").write_text("#ifndef FOO_H\n#define FOO_H\nint use(int a);\n#endif\n")
    (bench / "sdd.md").write_text("# SDD\n- **FOO-001**: `use` SHALL return the read.\n")
    run_mtm_json("init", str(bench))
    for n, kind in (("foo.c", "source"), ("foo.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", n, "--kind", kind, cwd=bench)
    inventory = build_inventory(
        bench, yaml.safe_load((bench / "project.yaml").read_text()).get("scopes", []))
    scenario = {
        "id": "S1", "title": "t", "function": "use", "requirement_ids": ["FOO-001"],
        "mocks": ["dep_write"],
        "mock_declarations": {"dep_write": "int dep_write(int a);"},
        "cases": [{"args": [1], "expected": 1, "mocks": {"dep_write": {"ret": 1}}}],
        "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}
    plan = parse_plan(json.dumps({"scenarios": [scenario]}), inventory,
                      LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory, resolve=_fake_resolve)
    assert out.test_functions, out.limitations
    assert out.types["declaration_sources"]["dep_write"] == "llm", out.types
    body = (bench / ".mtm" / "gen" / "interface" / "mtm_dep_dep_write.h").read_text()
    assert "int dep_write(int a);" in body, body


def test_an_ambiguous_llm_declaration_field_is_a_named_limitation(tmp_path):
    """Contrast (the refusal): with no real declaration and a ``mock_declarations``
    field that is **not one unambiguous prototype** (IMP-18 recorded such a field),
    the generator must record a precise named limitation and invent no signature."""
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan
    import yaml

    bench = tmp_path / "ambiguous-field"
    bench.mkdir()
    (bench / "foo.c").write_text("int use(int a) { return dep_write(a); }\n")
    (bench / "foo.h").write_text("#ifndef FOO_H\n#define FOO_H\nint use(int a);\n#endif\n")
    (bench / "sdd.md").write_text("# SDD\n- **FOO-001**: `use` SHALL return the read.\n")
    run_mtm_json("init", str(bench))
    for n, kind in (("foo.c", "source"), ("foo.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", n, "--kind", kind, cwd=bench)
    inventory = build_inventory(
        bench, yaml.safe_load((bench / "project.yaml").read_text()).get("scopes", []))
    # Two different prototypes for the same symbol: ambiguous, so unusable.
    ambiguous = ("int dep_write(int a);\n"
                 "long dep_write(long a);")
    scenario = {
        "id": "S1", "title": "t", "function": "use", "requirement_ids": ["FOO-001"],
        "mocks": ["dep_write"],
        "mock_declarations": {"dep_write": ambiguous},
        "cases": [{"args": [1], "expected": 1, "mocks": {"dep_write": {"ret": 1}}}],
        "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}
    plan = parse_plan(json.dumps({"scenarios": [scenario]}), inventory,
                      LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory, resolve=_fake_resolve)
    assert out.test_functions == [], out.test_functions
    assert any("no usable declaration" in lim and "not one unambiguous prototype" in lim
               for lim in out.limitations), out.limitations
    # No interface header for the refused mock: nothing dangling, no invented C.
    assert not (bench / ".mtm" / "gen" / "interface" / "mtm_dep_dep_write.h").exists()


def test_a_typedef_after_a_declaration_on_the_same_line_is_still_resolved(tmp_path):
    """A ``typedef`` that does not start its line (``int g(void); typedef
    unsigned short vendor_t;``) must still be attributed to a defining *header*, so
    a mocked declaration through it is typed and its interface header can
    ``#include`` the defining header. If it were lost, the interface header would
    carry ``void dep_write(vendor_t p);`` with ``vendor_t`` unknown — uncompilable
    C with ``limitations == []``, the shape §C forbids. Under the clang contract
    the defining location comes from libclang (not the line-anchored regex); the
    lexical :func:`mtm.cscan.typedef_statement_map` remains only as the verbatim
    fallback text for an alias with no includable header. This asserts the
    generated header compiles with the real gcc."""
    import shutil
    import subprocess

    from mtm.cscan import typedef_statement_map

    text = "int g(void); typedef unsigned short vendor_t;\n"
    assert "vendor_t" in typedef_statement_map(text), typedef_statement_map(text)
    # A second alias after the first on the same line must not be swallowed by the
    # first match (the ``;`` separating them is not consumed).
    pair = "typedef int a; typedef int b;\n"
    assert set(typedef_statement_map(pair)) == {"a", "b"}, typedef_statement_map(pair)

    bench, out = _mock_bench(
        tmp_path, "combined-line-typedef",
        "int use(int a) { dep_write(0); return a; }\n",
        "#ifndef FOO_H\n#define FOO_H\n#include <stdint.h>\n"
        "int g(void); typedef unsigned short vendor_t;\n"
        "void dep_write(vendor_t p);\nint use(int a);\n#endif\n",
        {"id": "S1", "title": "t", "function": "use", "requirement_ids": ["FOO-001"],
         "mocks": ["dep_write"],
         "cases": [{"args": [1], "expected": 1, "mocks": {"dep_write": {"ret": 0}}}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    header_path = bench / ".mtm" / "gen" / "interface" / "mtm_dep_dep_write.h"
    body = header_path.read_text()
    # The alias is visible through the defining copied header, as for F3.
    assert '#include "foo.h"' in body, body
    if shutil.which("gcc"):
        comp = tmp_path / "compile-combined"
        comp.mkdir()
        copy = comp / "hdr.c"
        copy.write_text(body)
        proc = subprocess.run(
            ["gcc", "-std=c99", "-Wall", "-Werror",
             "-I", str(bench / ".mtm" / "gen" / "src"),
             "-c", str(copy), "-o", str(comp / "hdr.o")],
            capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


def test_real_image_executes_a_mock_through_a_combined_line_typedef(
        tmp_path, product_image):
    """AP-02 for the combined-line alias: the whole mocked bench (private interface
    header + CMock mock + Unity test) must compile, link and **execute** in the
    product image, not merely generate plausible text. The SUT calls the mocked
    dependency with a value and returns it, so the run is 1 test / 0 failed."""
    from mtm.ceedling import run_ceedling

    bench, out = _mock_bench(
        tmp_path, "combined-line-image",
        "unsigned short use(int a) { return dep_read(a); }\n",
        "#ifndef FOO_H\n#define FOO_H\n#include <stdint.h>\n"
        "int g(void); typedef unsigned short vendor_t;\n"
        "vendor_t dep_read(int a);\nunsigned short use(int a);\n#endif\n",
        {"id": "S1", "title": "t", "function": "use", "requirement_ids": ["FOO-001"],
         "mocks": ["dep_read"],
         "cases": [{"args": [3], "expected": 3, "mocks": {"dep_read": {"ret": 3}}}],
         "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}})
    assert out.test_functions, out.limitations
    run = run_ceedling(bench, image_ref=product_image)
    assert run.ran, run.diagnostics[-2000:]
    assert run.total == 1 and run.failed == 0, (run.total, run.failed, run.stdout[-1500:])



@pytest.mark.docker
def test_real_image_runs_a_mock_through_a_pointer_typedef_and_a_guarded_aggregate(
        tmp_path, product_image):
    """AP-02 / I2 / I5 for the clang contract: the whole mocked bench (private
    interface header + CMock mock + Unity test) must compile, link and **execute**
    in the real product image for (a) a mock through a **pointer typedef** declared
    at the clang depth and (b) a **guarded inline-aggregate alias**, and the same
    suite must discriminate a correct from a faulty SUT.

    The image resolver (libclang 18) drives the type map; the includes are shared
    by the compiler, not a guard regex. This is the real-compiler evidence the
    plan requires (a text assertion is not proof of C semantics).
    """
    from mtm.ceedling import run_ceedling
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = tmp_path / "clang-pointer-and-aggregate"
    bench.mkdir()
    # A guarded inline-aggregate alias (record identity via clang) and a pointer
    # typedef at depth 1 (`P`), used as a scalar parameter (depth 1, renderable).
    (bench / "foo.c").write_text(
        '#include "foo.h"\n'
        "unsigned short use(int a) { P p = &a; return dep_read(p); }\n")
    (bench / "foo.h").write_text(
        "#ifndef FOO_H\n#define FOO_H\n#include <stdint.h>\n"
        "typedef int *P;\n"
        "typedef struct { unsigned short v; } sample_t;\n"
        "typedef enum { E_A, E_B } e_t;\n"
        "sample_t dep_make(e_t e);\n"
        "unsigned short dep_read(P p);\n"
        "unsigned short use(int a);\n#endif\n")
    (bench / "sdd.md").write_text("# SDD\n- **FOO-001**: `use` SHALL return the read value.\n")
    run_mtm_json("init", str(bench))
    for n, kind in (("foo.c", "source"), ("foo.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", n, "--kind", kind, cwd=bench)
    inventory = build_inventory(
        bench, _scopes(bench))
    scenario = {
        "id": "S1", "title": "t", "function": "use", "requirement_ids": ["FOO-001"],
        "mocks": ["dep_read"],
        "cases": [{"args": [3], "expected": 3, "mocks": {"dep_read": {"ret": 3}}}],
        "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}
    plan = parse_plan(json.dumps({"scenarios": [scenario]}), inventory,
                      LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory, image=product_image)
    assert out.types["status"], out.types
    assert out.types["status"] == "RESOLVED", out.types
    assert out.test_functions, out.limitations
    # The pointer typedef's depth came from clang (F3): the mock's parameter is a
    # depth-1 `P` and the interface header shares the defining header.
    body = (bench / ".mtm" / "gen" / "interface" / "mtm_dep_dep_read.h").read_text()
    assert '#include "foo.h"' in body, body

    # Correct SUT: the mapped return discriminates.
    run = run_ceedling(bench, image_ref=product_image)
    assert run.ran, run.diagnostics[-2000:]
    assert run.total == 1 and run.failed == 0, (run.total, run.failed, run.stdout[-1500:])

    # Faulty SUT (contrast): the implementation contradicts the SDD-return, so the
    # same generated assertion must FAIL — a finding, never a silent pass.
    (bench / "foo.c").write_text(
        '#include "foo.h"\n'
        "unsigned short use(int a) { P p = &a; "
        "return (unsigned short)(dep_read(p) + 1); }\n")
    out2 = generate_suite(bench, plan, inventory, image=product_image)
    assert out2.test_functions, out2.limitations
    faulty = run_ceedling(bench, image_ref=product_image)
    assert faulty.ran, faulty.diagnostics[-2000:]
    assert faulty.total == 1 and faulty.failed == 1, \
        (faulty.total, faulty.failed, faulty.stdout[-1500:])


@pytest.mark.docker
def test_real_image_runs_an_enum_typed_argument(tmp_path, product_image):
    """AP-02 / I1 / I5 for the enum regression (TC-1): a mock and SUT path with an
    **enum-typed argument** must compile, link and **execute** in the real product
    image (libclang 18 drives the type map). At the regressed candidate the enum
    parameter was refused as an aggregate (empty suite); the fix must generate a
    real, runnable bench — correct SUT PASS, faulty SUT FAIL.

    The mocked ``dep_state`` takes an enum *by value* and another returns one; the
    SUT passes and returns enum-typed values, so the whole chain exercises the
    scalar-enum boundary the defect broke."""
    from mtm.ceedling import run_ceedling
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = tmp_path / "clang-enum-argument"
    bench.mkdir()
    (bench / "foo.c").write_text(
        '#include "foo.h"\n'
        "state_t use(state_t s) { return dep_next(s); }\n")
    (bench / "foo.h").write_text(
        "#ifndef FOO_H\n#define FOO_H\n#include <stdint.h>\n"
        "typedef enum { ST_STOPPED, ST_RUNNING } state_t;\n"
        "state_t dep_next(state_t s);\n"
        "state_t use(state_t s);\n#endif\n")
    (bench / "sdd.md").write_text(
        "# SDD\n- **FOO-001**: `use` SHALL return the next state.\n")
    run_mtm_json("init", str(bench))
    for n, kind in (("foo.c", "source"), ("foo.h", "source"), ("sdd.md", "spec")):
        run_mtm_json("scope", "add", n, "--kind", kind, cwd=bench)
    inventory = build_inventory(bench, _scopes(bench))
    scenario = {
        "id": "S1", "title": "t", "function": "use", "requirement_ids": ["FOO-001"],
        "mocks": ["dep_next"],
        "cases": [{"args": ["ST_STOPPED"], "expected": "ST_RUNNING",
                   "mocks": {"dep_next": {"ret": "ST_RUNNING"}}}],
        "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}
    plan = parse_plan(json.dumps({"scenarios": [scenario]}), inventory,
                      LLMIdentity(provider="test", model="t"))
    out = generate_suite(bench, plan, inventory, image=product_image)
    assert out.types["status"] == "RESOLVED", out.types
    # The enum parameter was NOT refused as an aggregate (the regression).
    assert out.limitations == [], out.limitations
    assert out.test_functions, out.limitations
    body = (bench / ".mtm" / "gen" / "test" / "test_foo.c").read_text()
    assert "(state_t)ST_STOPPED" in body, body

    # Correct SUT: the mock's enum return is observed and the case passes.
    run = run_ceedling(bench, image_ref=product_image)
    assert run.ran, run.diagnostics[-2000:]
    assert run.total == 1 and run.failed == 0, (run.total, run.failed, run.stdout[-1500:])

    # Faulty SUT (contrast): returns the *wrong* enum, so the same generated
    # assertion must FAIL — a finding, never a silent pass.
    (bench / "foo.c").write_text(
        '#include "foo.h"\n'
        "state_t use(state_t s) { (void)s; return ST_STOPPED; }\n")
    out2 = generate_suite(bench, plan, inventory, image=product_image)
    assert out2.test_functions, out2.limitations
    faulty = run_ceedling(bench, image_ref=product_image)
    assert faulty.ran, faulty.diagnostics[-2000:]
    assert faulty.total == 1 and faulty.failed == 1, \
        (faulty.total, faulty.failed, faulty.stdout[-1500:])


# --- I4: clang resolution failure is typed, never a guessed type -------------


def _mock_scenario():
    return {"id": "S1", "title": "t", "function": "use", "requirement_ids": ["FOO-001"],
            "mocks": ["dep_write"],
            "cases": [{"args": [1], "expected": 1, "mocks": {"dep_write": {"ret": 0}}}],
            "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}


def test_a_blocked_resolution_refuses_each_mock_with_its_diagnostic(tmp_path):
    """I4: an adapter ``BLOCKED`` (incomplete build context — a missing header or
    define) means no declaration can be typed faithfully. Each affected mock gets a
    *named per-symbol limitation* carrying the diagnostic and its scenarios are
    skipped — never a guessed type, never broken C with ``limitations == []``."""
    from mtm import ctypes_clang

    def blocked(paths, **kwargs):
        return ctypes_clang.CTypeResolution(
            status=ctypes_clang.BLOCKED, clang_version="18.1.8",
            message="'vendor.h' file not found",
            diagnostics=["/bench/foo.c:2:10: Fatal: 'vendor.h' file not found"],
            issues=[{"kind": "MISSING_CONTEXT", "message": "'vendor.h' file not found"}])

    bench, out = _mock_bench(
        tmp_path, "blocked-resolution",
        "int use(int a) { return dep_write(a); }\n",
        "#ifndef FOO_H\n#define FOO_H\nint dep_write(int a);\nint use(int a);\n#endif\n",
        _mock_scenario(), resolve=blocked)
    assert out.types["status"] == "BLOCKED"
    assert out.test_functions == [], out.test_functions
    assert any("dep_write" in lim and "could not be resolved" in lim
               for lim in out.limitations), out.limitations


def test_a_tool_failed_resolution_is_a_run_level_tool_fault_not_a_sut_defect(
        fixture_bench):
    """I4: a broken resolver (docker/binding) is a **run-level tool fault** —
    ``TOOL_FAILURE``/exit 30 — never reported as a SUT defect or a functional
    result, and never a guessed pass."""
    from mtm import ctypes_clang
    from mtm.generate import run_generate
    from mtm.llm import MockProvider

    def broken(paths, **kwargs):
        return ctypes_clang.CTypeResolution(
            status=ctypes_clang.TOOL_FAILED,
            message="libclang could not be used: ModuleNotFoundError: clang")

    bench = fixture_bench("mocked-sensor")

    def runner(*a, **k):
        raise AssertionError("the suite must not be executed on a resolver fault")

    result = run_generate(bench, _scopes(bench), provider=MockProvider(),
                          image="img:test", runner=runner, resolve=broken)
    assert result.status == "TOOL_FAILURE" and result.exit_code == 30
    assert result.smoke["ran"] is False
    assert result.findings == []  # never a SUT defect
