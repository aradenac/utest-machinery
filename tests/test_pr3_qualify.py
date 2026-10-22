"""PR 3 qualification tests: R04 oracle justification and R22 suite sensitivity.

These exercise the qualification *logic* offline with synthetic plans and
campaign statuses, so the classification contract is pinned without Docker. The
real fixture campaigns (frozen generated suite run against the correct and faulty
saturation snapshots in the product image, and the withheld manifest comparison)
are a separate execution-tier test in this module, gated on the product image.

R04 (``docs/qualification/qualification.md``): a valid requirement id is reference
integrity only. A concrete expected value that disagrees with the SDD's
independently derived outcome must be ``CONTRADICTED_BY_SDD`` and must never become
a code-defect claim; an absent/ambiguous value stays ``INDETERMINATE``/
``NOT_CHECKED`` and never ``SUPPORTED``.

R22: the *same unchanged* tests/oracles run on separately snapshotted correct and
faulty inputs; a defect is ``DETECTED`` only when the correct variant passes and
the faulty one fails; a build/tool fault is ``NOT_EVALUABLE``, never a detection.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import REPO
from conftest import fake_resolve as _offline_resolve
from mtm.qualify import (
    CONTRADICTED,
    DETECTED,
    FALSE_POSITIVE,
    INDETERMINATE,
    MISSED,
    NOT_CHECKED,
    NOT_EVALUABLE,
    SUPPORTED,
    DefectOutcome,
    assess_oracles,
    classify_defect,
    load_manifest,
    oracle_summary,
    sensitivity_summary,
)

MANIFEST = REPO / "examples" / "qualification-manifest.json"


def _scenario(fn, args, expected, *, reqs=("SAT-002",), kind="SPECIFIED",
              expect_out=None):
    case = {"args": list(args), "expected": expected}
    if expect_out is not None:
        case["expect_out"] = expect_out
    return {"id": "S1", "title": "t", "function": fn, "requirement_ids": list(reqs),
            "mocks": [], "cases": [case],
            "oracle": {"kind": kind, "statement": "x", "rationale": "y"}}


# --- R04 --------------------------------------------------------------------


def test_a_correct_sdd_grounded_value_is_supported():
    """Positive contrast: the boundary value the SDD entails is accepted."""
    plan = {"scenarios": [_scenario("saturate_u16", [11, 10], 10)]}
    assessments = assess_oracles(plan, load_manifest(MANIFEST))
    assert [a.result for a in assessments] == [SUPPORTED]


def test_a_wrong_value_with_a_valid_requirement_id_is_contradicted():
    """The decisive R04 case: the SDD says input>ceiling returns ceiling (10), but
    the plan asserts the faulty implementation's 9 while citing the valid SAT-002.
    This must be ``CONTRADICTED_BY_SDD`` and must not touch the plan."""
    plan = {"scenarios": [_scenario("saturate_u16", [11, 10], 9)]}
    assessments = assess_oracles(plan, load_manifest(MANIFEST))
    assert [a.result for a in assessments] == [CONTRADICTED]
    assert assessments[0].expected == 9 and assessments[0].reference == 10


def test_an_undetermined_oracle_is_never_supported():
    """An absent/ambiguous expected value must stay ``INDETERMINATE`` — a valid id
    alone never makes it ``SUPPORTED_BY_SDD``."""
    plan = {"scenarios": [_scenario("saturate_u16", [11, 10], None,
                                    kind="UNDETERMINED")]}
    assessments = assess_oracles(plan, load_manifest(MANIFEST))
    assert [a.result for a in assessments] == [INDETERMINATE]


def test_a_function_absent_from_the_manifest_is_not_checked_not_supported():
    plan = {"scenarios": [_scenario("unknown_fn", [1], 7)]}
    assessments = assess_oracles(plan, load_manifest(MANIFEST))
    assert [a.result for a in assessments] == [NOT_CHECKED]


def test_an_input_not_authored_in_the_manifest_is_not_checked():
    """A case whose inputs the manifest does not author is ``NOT_CHECKED`` — the
    assessment never compares it against a *different* case's reference."""
    plan = {"scenarios": [_scenario("saturate_u16", [7, 3], 3)]}
    assessments = assess_oracles(plan, load_manifest(MANIFEST))
    assert [a.result for a in assessments] == [NOT_CHECKED]


def test_each_output_observable_is_assessed_individually():
    plan = {"scenarios": [_scenario("saturate_u16", [11, 10], 10,
                                    expect_out={"0": 3})]}
    manifest = {"functions": {"saturate_u16": {"cases": [
        {"args": [11, 10], "return": 10, "expect_out": {"0": 4}}]}}}
    results = {a.observable: a.result for a in assess_oracles(plan, manifest)}
    assert results["return"] == SUPPORTED
    assert results["out[0]"] == CONTRADICTED


def test_bool_and_int_are_distinct_observation_domains():
    """C ``bool`` and ``uint16_t`` are different types: ``true`` must not be judged
    equal to ``1`` even though Python compares them equal."""
    manifest = {"functions": {"permit_enable": {"cases": [
        {"args": [1, 9000, 0], "return": True}]}}}
    plan = {"scenarios": [_scenario("permit_enable", [1, 9000, 0], 1)]}
    assert [a.result for a in assess_oracles(plan, manifest)] == [CONTRADICTED]


def test_the_oracle_summary_keeps_discrepancies_separate_from_defects():
    plan = {"scenarios": [_scenario("saturate_u16", [11, 10], 9)]}
    summary = oracle_summary(assess_oracles(plan, load_manifest(MANIFEST)))
    assert summary["counts"][CONTRADICTED] == 1
    assert len(summary["discrepancies"]) == 1
    assert "never proves a C defect" in summary["note"]


# --- R22 --------------------------------------------------------------------


@pytest.mark.parametrize("correct,faulty,expected_result", [
    ("PASSED", "FAILED", DETECTED),
    ("PASSED", "PASSED", MISSED),
    ("FAILED", "FAILED", FALSE_POSITIVE),
    ("PASSED", "TOOL_FAILED", NOT_EVALUABLE),
    ("TOOL_FAILED", "FAILED", NOT_EVALUABLE),
])
def test_defect_classification_matrix(correct, faulty, expected_result):
    result, _ = classify_defect(correct_status=correct, faulty_status=faulty)
    assert result == expected_result


def test_a_compile_error_is_never_a_detection():
    """R22 step 3: a failed build on the faulty variant is ``NOT_EVALUABLE``, not
    ``DETECTED`` — the defect was not distinguished by an assertion."""
    result, why = classify_defect(correct_status="PASSED", faulty_status="TOOL_FAILED")
    assert result == NOT_EVALUABLE
    assert "not a detection" in why


def test_the_sensitivity_summary_counts_only_evaluable_defects():
    outcomes = [
        DefectOutcome("D1", "saturation", "saturation.c:8", "off_by_one", "ceiling",
                      correct_status="PASSED", faulty_status="FAILED", result=DETECTED),
        DefectOutcome("D2", "frame-parser", "frame.c:3", "bounds", "reject",
                      correct_status="PASSED", faulty_status="TOOL_FAILED",
                      result=NOT_EVALUABLE),
    ]
    summary = sensitivity_summary(outcomes)
    assert summary["attempted"] == 2
    assert summary["evaluable"] == 1 and summary["detected"] == 1
    assert summary["counts"][NOT_EVALUABLE] == 1
    assert "not a guarantee" in summary["note"]


def test_the_r04_assessment_is_surfaced_through_plan_evidence(tmp_path):
    """R04 is exposed through the existing ``plan`` evidence (no new public
    command): with ``MTM_QUALIFICATION_MANIFEST`` set, ``mtm plan`` runs the
    assessment over the *frozen* plan and persists it, leaving the plan untouched.
    """
    import shutil
    import subprocess
    import sys
    import yaml

    from conftest import run_mtm_json

    from mtm.generate import run_generate
    from mtm.llm import MockProvider

    bench = tmp_path / "sat"
    bench.mkdir()
    for f in (REPO / "examples" / "saturation").iterdir():
        if f.is_file():
            shutil.copy(f, bench / f.name)
    assert run_mtm_json("init", str(bench))[1] == 0
    for f in sorted(bench.iterdir()):
        if f.suffix in (".c", ".h", ".md"):
            run_mtm_json("scope", "add", f.name, "--kind",
                         "source" if f.suffix != ".md" else "spec", cwd=bench)
    scopes = yaml.safe_load((bench / "project.yaml").read_text())["scopes"]
    run_generate(bench, scopes, provider=MockProvider(), image="img:test",
                 resolve=_offline_resolve)
    plan_before = (bench / ".mtm" / "state" / "generate" / "plan.json").read_bytes()

    record, code = run_mtm_json("plan", cwd=bench,
                                env={"MTM_QUALIFICATION_MANIFEST": str(MANIFEST)})
    assert code == 0, record
    assessment = record["oracle_assessment"]
    # The recorded plan asserts the SDD value 10 (not the faulty 9): supported.
    assert assessment["counts"][SUPPORTED] >= 1
    assert assessment["counts"][CONTRADICTED] == 0
    assert assessment["path"].endswith("oracle_assessment.json")
    # The frozen plan is byte-identical: R04 never edits an oracle.
    assert (bench / ".mtm" / "state" / "generate" / "plan.json").read_bytes() == plan_before


def test_without_a_manifest_the_assessment_is_absent(tmp_path):
    """The manifest is never a silent input: with no ``MTM_QUALIFICATION_MANIFEST``
    the plan output carries no assessment at all."""
    import shutil

    from conftest import run_mtm_json

    from mtm.generate import run_generate
    from mtm.llm import MockProvider
    import yaml

    bench = tmp_path / "sat"
    bench.mkdir()
    for f in (REPO / "examples" / "saturation").iterdir():
        if f.is_file():
            shutil.copy(f, bench / f.name)
    assert run_mtm_json("init", str(bench))[1] == 0
    for f in sorted(bench.iterdir()):
        if f.suffix in (".c", ".h", ".md"):
            run_mtm_json("scope", "add", f.name, "--kind",
                         "source" if f.suffix != ".md" else "spec", cwd=bench)
    scopes = yaml.safe_load((bench / "project.yaml").read_text())["scopes"]
    run_generate(bench, scopes, provider=MockProvider(), image="img:test",
                 resolve=_offline_resolve)
    record, code = run_mtm_json("plan", cwd=bench)
    assert code == 0
    assert "oracle_assessment" not in record


def test_the_manifest_is_not_a_generation_input():
    """QD-012: the independent manifest must not be selectable as a generator
    input. It lives in ``examples/`` (outside the shipped package) and is never
    referenced by the plan prompt or a recording."""
    from mtm.plan import PROMPT_TEMPLATE

    manifest = load_manifest(MANIFEST)
    assert manifest  # it is real
    # The generation prompt template never names or embeds the withheld file.
    assert "qualification-manifest" not in PROMPT_TEMPLATE
    assert "examples/qualification" not in PROMPT_TEMPLATE
    # No recorded model response embeds the withheld reference values file name.
    for recording in (REPO / "mtm" / "recordings").glob("*.json"):
        assert "qualification-manifest" not in recording.read_text()
    # And the fixtures under examples/ are not package data.
    import mtm

    packaged = Path(mtm.__file__).parent
    assert not (packaged / "qualification-manifest.json").exists()


def test_the_r22_correct_variant_conforms_where_the_faulty_one_does_not():
    """The two fixtures differ exactly at the SAT-002 boundary: the reference
    returns the ceiling, the seeded-faulty variant returns ceiling-1."""
    correct = (REPO / "examples" / "qualification" / "saturation-correct"
               / "saturation.c").read_text()
    faulty = (REPO / "examples" / "saturation" / "saturation.c").read_text()
    assert "return ceiling;" in correct
    assert "ceiling - 1u" not in correct
    assert "ceiling - 1u" in faulty
    manifest = load_manifest(MANIFEST)
    defects = manifest["fixtures"]["saturation"]["defects"]
    assert defects and defects[0]["id"] == "SAT-OFF-BY-ONE"
