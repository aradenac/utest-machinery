"""PR 2 acceptance and invariant tests (the merged, user-facing behaviour).

Covers the milestone acceptance criteria from ``docs/plan/implementation.md``
(PR 2 column) and the AP-03 critical invariants, all offline and token-free via
the recorded mock provider:

* a fresh selected C/SDD fixture becomes an **executable** unit suite;
* an intentional defect is detected against the **SDD** oracle (not the code);
* an unknown/conflicting oracle stays **UNDETERMINED** yet is still executed;
* the original inputs are byte-identical before/after;
* no oracle weakening: a SPECIFIED oracle is never silently relaxed to pass.

Invariants (AP-03), each with a representative transition:

* write -> read: the versioned plan is written, then ``mtm plan`` reads it and
  its report agrees with the stored plan;
* determinism/repeatability: two generations produce identical suite bytes;
* nonterminal vs terminal: an UNDETERMINED scenario that *executes and passes*
  is still reported UNDETERMINED, never SUPPORTED;
* mixed outcome: one passing and one contradicting scenario in the same unit
  yields a finding while the passing scenario is retained.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from conftest import REPO, run_mtm_json

EXAMPLES = REPO / "examples"


def _hashes(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


@pytest.fixture
def fixture_bench(tmp_path: Path):
    """Create a bench holding a copy of one fixture and select its files."""

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


# --- acceptance: executable suite and detected defect ----------------------


def test_fresh_fixture_becomes_an_executable_suite(fixture_bench, tmp_path: Path):
    bench = fixture_bench("mocked-sensor")
    record, code = run_mtm_json("generate", "--provider", "mock", cwd=bench)
    assert code == 0, record
    assert record["status"] == "SUCCEEDED"
    assert record["smoke"]["built"] is True and record["smoke"]["ran"] is True
    assert record["smoke"]["unity"]["total"] >= 1
    # The generated artefacts are real files under the bench.
    assert (bench / "tests" / "mtm_runner.c").is_file()
    assert list((bench / "mocks").glob("mock_*.c"))


def test_intentional_defect_is_detected_against_the_sdd(fixture_bench):
    bench = fixture_bench("saturation")
    record, code = run_mtm_json("generate", "--provider", "mock", cwd=bench)
    assert code == 10, record  # a finding, preserved as evidence
    assert record["status"] == "FINDINGS"
    findings = record["findings"]
    assert findings and findings[0]["category"] == "IMPLEMENTATION_DEFECT"
    # The oracle comes from SAT-002, not from the implementation's behaviour.
    assert "ceiling" in findings[0]["expected_behavior"].lower()


def test_adapted_unit_with_absent_header_is_built_from_a_private_shim(fixture_bench):
    bench = fixture_bench("vendor-adapter")
    record, code = run_mtm_json("generate", "--provider", "mock", cwd=bench)
    assert code == 0, record
    assert record["feasibility_counts"]["ADAPTED"] == 1
    # The shim lives under the bench; the source directory gained no vendor header.
    assert list((bench / "shims").glob("*.h"))
    assert not (EXAMPLES / "vendor-adapter" / "vendor_adc.h").exists()


def test_unknown_oracle_stays_undetermined(fixture_bench):
    bench = fixture_bench("underspecified")
    record, code = run_mtm_json("generate", "--provider", "mock", cwd=bench)
    assert code == 0, record  # the undefined cases do not fail the SDD
    plan, code = run_mtm_json("plan", cwd=bench)
    assert code == 0
    assert plan["undetermined"] >= 1
    assert any(p.get("proposal") for p in plan["proposals"])


def test_conflicting_sdd_boundary_is_undetermined_not_arbitrary_pass(fixture_bench):
    bench = fixture_bench("conflicting-sdd")
    record, code = run_mtm_json("generate", "--provider", "mock", cwd=bench)
    assert code == 0, record
    plan, _ = run_mtm_json("plan", cwd=bench)
    # Exactly one scenario (the equality boundary) is undetermined.
    undetermined = [s for s in plan["scenarios"] if s["oracle_kind"] == "UNDETERMINED"]
    assert len(undetermined) == 1


# --- acceptance: immutability and no oracle weakening ----------------------


def test_selected_inputs_are_unchanged_by_generation(fixture_bench):
    bench = fixture_bench("saturation")
    sources = {name: hashlib.sha256((bench / name).read_bytes()).hexdigest()
               for name in ("saturation.c", "saturation.h", "sdd.md")}
    run_mtm_json("generate", "--provider", "mock", cwd=bench)
    run_mtm_json("plan", cwd=bench)
    for name, digest in sources.items():
        assert hashlib.sha256((bench / name).read_bytes()).hexdigest() == digest, name


def test_generated_test_asserts_the_sdd_value_not_the_implementation(fixture_bench):
    """R04/AP-01: the failing case must encode the SDD oracle (10), not the code (9)."""
    bench = fixture_bench("saturation")
    run_mtm_json("generate", "--provider", "mock", cwd=bench)
    source = (bench / "tests" / "saturation_mtm_test.c").read_text()
    assert "TEST_ASSERT_EQUAL(10, saturate_u16((uint16_t)11, (uint16_t)10))" in source
    assert "TEST_ASSERT_EQUAL(9," not in source  # the implementation's wrong value is never the oracle


# --- AP-03 invariants -------------------------------------------------------


def test_invariant_plan_written_then_read_consistently(fixture_bench):
    bench = fixture_bench("saturation")
    gen, _ = run_mtm_json("generate", "--provider", "mock", cwd=bench)
    stored = json.loads((bench / ".mtm" / "state" / "generate" / "plan.json").read_text())
    plan, code = run_mtm_json("plan", cwd=bench)
    assert code == 0
    assert plan["snapshot_hash"] == stored["snapshot_hash"] == gen["snapshot_hash"]
    assert plan["planned"] == len(stored["scenarios"])


def test_invariant_generation_is_reproducible(fixture_bench):
    bench = fixture_bench("saturation")
    run_mtm_json("generate", "--provider", "mock", cwd=bench)
    first = (bench / "tests" / "saturation_mtm_test.c").read_text()
    run_mtm_json("generate", "--provider", "mock", cwd=bench)
    second = (bench / "tests" / "saturation_mtm_test.c").read_text()
    assert first == second


def test_invariant_undetermined_pass_is_not_supported(fixture_bench):
    """An UNDETERMINED scenario runs and may pass, but is never a conformity claim."""
    bench = fixture_bench("underspecified")
    record, code = run_mtm_json("generate", "--provider", "mock", cwd=bench)
    assert code == 0, record
    # Its cases execute (the suite runs) but the plan still reports them UNDETERMINED.
    source = (bench / "tests" / "mode_mtm_test.c").read_text()
    assert "UNDETERMINED" in source  # comment marks the coverage-only cases
    plan, _ = run_mtm_json("plan", cwd=bench)
    assert all(s["oracle_kind"] != "SUPPORTED" for s in plan["scenarios"])


def test_invariant_mixed_outcome_reports_finding_and_keeps_passing_case(fixture_bench):
    """Saturation has one passing scenario (S1) and one contradiction (S2)."""
    bench = fixture_bench("saturation")
    record, code = run_mtm_json("generate", "--provider", "mock", cwd=bench)
    assert code == 10
    assert record["smoke"]["unity"]["passed"] >= 1
    assert record["smoke"]["unity"]["failed"] >= 1
    plan, _ = run_mtm_json("plan", cwd=bench)
    assert plan["generated"] == plan["planned"]  # every planned scenario materialised


def test_generate_without_scope_is_a_clean_error(bench: Path):
    record, code = run_mtm_json("generate", "--provider", "mock", cwd=bench)
    assert code == 40
    assert record["code"] == "INPUT_MISSING"


def test_dry_run_does_not_call_the_model_or_write_tests(fixture_bench):
    bench = fixture_bench("saturation")
    record, code = run_mtm_json("generate", "--dry-run", "--provider", "mock", cwd=bench)
    assert code == 0
    assert record["status"] == "PLANNED"
    assert record["feasibility_counts"]["READY"] == 1
    assert not (bench / "tests" / "saturation_mtm_test.c").exists()


# --- generator invariants exposed by real (non-recorded) plans ---------------


def _scopes(bench: Path) -> list[dict]:
    import yaml
    data = yaml.safe_load((bench / "project.yaml").read_text())
    return data.get("scopes", [])


def _plan_from(bench: Path, payload: dict):
    """Ground a hand-written plan payload exactly as a model response would be."""
    import json as _json
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    inventory = build_inventory(bench, _scopes(bench))
    return parse_plan(_json.dumps(payload), inventory,
                      LLMIdentity(provider="test", model="hand-written"))


def test_pointer_argument_carries_its_planned_initial_value(fixture_bench):
    """A case's pointer arg is the pointed-to value BEFORE the call.

    This is what lets a scenario assert that an error path preserves the
    previously accepted reading; initialising it to 0 instead would fail the
    assertion and report a defect the SUT does not have.
    """
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory

    bench = fixture_bench("mocked-sensor")
    inventory = build_inventory(bench, _scopes(bench))
    plan = _plan_from(bench, {
        "scenarios": [{
            "id": "S1", "title": "t", "function": "sample_sensor",
            "requirement_ids": ["SEN-002"], "mocks": ["platform_read_sensor"],
            "cases": [{"args": [642], "expected": "SENSOR_ERROR",
                       "mocks": {"platform_read_sensor": {"ret": 1, "reading": 55}},
                       "expect_out": {"0": 642},
                       "expect_calls": {"platform_read_sensor": 1}}],
            "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"},
        }],
    })
    generate_suite(bench, plan, inventory)
    source = (bench / "tests" / "sensor_mtm_test.c").read_text()
    assert "uint16_t out0_0 = (uint16_t)642;" in source


def test_every_case_resets_the_mock_call_count(fixture_bench):
    """`expect_calls` is per case: a case must not inherit the previous count."""
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory

    bench = fixture_bench("mocked-sensor")
    inventory = build_inventory(bench, _scopes(bench))
    plan = _plan_from(bench, {
        "scenarios": [{
            "id": "S1", "title": "t", "function": "sample_sensor",
            "requirement_ids": ["SEN-001", "SEN-003"], "mocks": ["platform_read_sensor"],
            "cases": [
                {"args": [1], "expected": "SENSOR_OK",
                 "mocks": {"platform_read_sensor": {"ret": 0, "reading": 1}},
                 "expect_calls": {"platform_read_sensor": 1}},
                {"args": [2], "expected": "SENSOR_OK",
                 "mocks": {"platform_read_sensor": {"ret": 0, "reading": 2}},
                 "expect_calls": {"platform_read_sensor": 1}},
            ],
            "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"},
        }],
    })
    generate_suite(bench, plan, inventory)
    source = (bench / "tests" / "sensor_mtm_test.c").read_text()
    # One reset per case, not one per test function.
    assert source.count("mock_platform_read_sensor_reset();") == 2


def test_unfaithful_scenario_is_a_limitation_not_a_fabricated_finding(fixture_bench):
    """A control the generator cannot honour must not become a failing assertion.

    Dropping the unsupported control would leave the mock at its default and
    report an implementation defect that does not exist. The scenario is refused
    and surfaced as a coverage limitation instead.
    """
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory

    bench = fixture_bench("mocked-sensor")
    inventory = build_inventory(bench, _scopes(bench))
    plan = _plan_from(bench, {
        "scenarios": [{
            "id": "S1", "title": "t", "function": "sample_sensor",
            "requirement_ids": ["SEN-003"], "mocks": ["platform_read_sensor"],
            "cases": [{"args": [1], "expected": "SENSOR_OK",
                       "mocks": {"platform_read_sensor": {"sequence": [0, 0]}},
                       "expect_calls": {"platform_read_sensor": 1}}],
            "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"},
        }],
    })
    generated = generate_suite(bench, plan, inventory)
    assert any("sequence" in lim for lim in generated.limitations)
    assert generated.test_functions == []
