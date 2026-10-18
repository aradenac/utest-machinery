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

from conftest import REPO, run_mtm_json

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
                        runner=runner)


def _run(ok: bool, returncode: int, *, stdout="", stderr="", timed_out=False,
         ran=False, total=0, passed=0, failed=0, failures=None):
    from mtm.ceedling import CeedlingRun

    return CeedlingRun(
        ok=ok, returncode=returncode, stdout=stdout, stderr=stderr,
        argv=["docker", "run", "img:test", "ceedling", "test:all"],
        timed_out=timed_out, ran=ran, total=total, passed=passed, failed=failed,
        failures=failures or [],
    )


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
    assert _unity_summary(stdout) == (5, 4, 1)
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
    summary = ("test_saturation.c:12:test_u_abc_S1:FAIL: Expected 10 Was 9\n"
               "-----------------------\n1 Tests 1 Failures 0 Ignored\nFAIL\n")
    result = _generate(bench, lambda *a, **k: _run(
        False, 1, stdout=summary, ran=True, total=1, passed=0, failed=1,
        failures=[("test_u_abc_S1", "Expected 10 Was 9")]))
    assert result.status == "FINDINGS" and result.exit_code == 10
    assert result.findings and result.findings[0]["category"] == "IMPLEMENTATION_DEFECT"
    assert "Expected 10 Was 9" == result.findings[0]["observed"]


def test_clean_pass_is_succeeded(fixture_bench):
    bench = fixture_bench("mocked-sensor")
    summary = "-----------------------\n3 Tests 0 Failures 0 Ignored\nOK\n"
    result = _generate(bench, lambda *a, **k: _run(
        True, 0, stdout=summary, ran=True, total=3, passed=3, failed=0))
    assert result.status == "SUCCEEDED" and result.exit_code == 0


def test_counted_failure_with_unparseable_detail_is_still_a_finding(fixture_bench):
    """If the summary counts failures but no `:FAIL` line parses (an unexpected
    Unity format), the run must not fall through to SUCCEEDED — a counted failure
    is a defect, recorded even without a per-test detail line."""
    bench = fixture_bench("saturation")
    summary = "Something odd happened\n1 Tests 1 Failures 0 Ignored\nFAIL\n"
    result = _generate(bench, lambda *a, **k: _run(
        False, 1, stdout=summary, ran=True, total=1, passed=0, failed=1,
        failures=[]))
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

    result = run_generate(bench, scopes, provider=_Stub(), image="img:test", runner=runner)
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
        summary = "-----------------------\n1 Tests 0 Failures 0 Ignored\nOK\n"
        return _run(True, 0, stdout=summary, ran=True, total=1, passed=1, failed=0)

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
                         image="img:test", runner=lambda *a, **k: _run(
                             True, 0, stdout="1 Tests 0 Failures 0 Ignored\nOK\n",
                             ran=True, total=1, passed=1, failed=0))
    assert first.plan is not None
    state = bench / ".mtm" / "state" / "generate"
    assert (state / "plan.json").is_file()

    (bench / "sensor.c").write_text("int sample_sensor(void) { return ??? ; }\n")
    second = run_generate(bench, _scopes(bench), provider=MockProvider(),
                          image="img:test", runner=lambda *a, **k: _run(True, 0))
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
