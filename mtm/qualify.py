"""Independent qualification of oracles (R04) and suite sensitivity (R22).

Both qualifications are **read-only conclusions over an unchanged frozen plan and
suite**: neither edits a scenario, an approved oracle, a generated test or a
native run record, and neither runs inside the offline ``mtm verify`` command
(``docs/architecture/data-contracts.md``, R04/R22 in
``docs/qualification/qualification.md``).

* **R04 — oracle justification.** The existence of a valid requirement id is
  *reference integrity only*: it does not prove that a concrete ``expected`` value
  is entailed by the cited SDD text. :func:`assess_oracles` compares each planned
  observable against an **independently maintained** manifest
  (``examples/qualification-manifest.json``) that is withheld from the generating
  model, and classifies it ``SUPPORTED_BY_SDD`` / ``CONTRADICTED_BY_SDD`` /
  ``INDETERMINATE`` / ``NOT_CHECKED``. A disagreement is an *oracle-quality
  discrepancy*: it never becomes a code-defect claim.
* **R22 — suite sensitivity.** A suite that merely compiles and passes proves
  little. :func:`compare_sensitivity` runs the **same frozen tests/oracles**
  against separately snapshotted correct and faulty inputs through the offline
  ``verify`` path and classifies each seeded defect ``DETECTED`` / ``MISSED`` /
  ``FALSE_POSITIVE`` / ``NOT_EVALUABLE`` against the withheld ground truth. A
  compile error or tool fault is **not** a detection.

The manifest is keyed by ``<fixture>/<function>`` and lists, per requirement, the
independently expected outcome and (for R22) the seeded defects with their
location and the defect kind, plus the withheld reference ("ground truth") used
only for comparison — never fed to the generator.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import Code, MtmError
from .paths import read_json

# --- R04 outcomes -----------------------------------------------------------

SUPPORTED = "SUPPORTED_BY_SDD"
CONTRADICTED = "CONTRADICTED_BY_SDD"
INDETERMINATE = "INDETERMINATE"
NOT_CHECKED = "NOT_CHECKED"

# --- R22 classifications ----------------------------------------------------

DETECTED = "DETECTED"
MISSED = "MISSED"
FALSE_POSITIVE = "FALSE_POSITIVE"
NOT_EVALUABLE = "NOT_EVALUABLE"

#: The independent manifest name inside a benchmark's example directory.
MANIFEST_NAME = "qualification-manifest.json"


def load_manifest(path: Path) -> dict[str, Any]:
    """Load the independent qualification manifest, or fail clearly."""
    data = read_json(path)
    if not isinstance(data, dict):
        raise MtmError(
            code=Code.INPUT_MISSING,
            message=f"No readable qualification manifest at {path}.",
            action="Provide the independent manifest; it must stay out of the "
                   "generation scope (QD-012).",
            stage="qualify",
        )
    return data


# --- R04 --------------------------------------------------------------------


@dataclass
class OracleAssessment:
    """A read-only quality conclusion about one planned observable (R04).

    Mirrors the ``OracleAssessment`` data contract: it links a frozen
    plan/snapshot to a scenario/case/observable, the SDD anchor, the observed
    expected value and an independent reference, with a result and explanation.
    It **does not** touch the ``Scenario`` or the generated test.
    """

    scenario: str
    function: str
    requirement_ids: list[str]
    observable: str          # "return" | "out[<idx>]" | "mock_calls[<symbol>]"
    expected: Any            # what the plan asserts
    reference: Any           # the independently authored value (withheld)
    result: str
    explanation: str
    anchor: str = ""         # the verbatim cited SDD clause
    case_index: int = 0
    provenance: str = "independent-manifest"

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "function": self.function,
            "requirement_ids": self.requirement_ids,
            "observable": self.observable,
            "expected": self.expected,
            "reference": self.reference,
            "result": self.result,
            "explanation": self.explanation,
            "anchor": self.anchor,
            "case_index": self.case_index,
            "provenance": self.provenance,
        }


def _norm(value: Any) -> Any:
    """Normalise a value for comparison without inventing equality.

    Only JSON scalars are compared; a non-scalar (dict/list) is returned as-is so
    an unrenderable expectation is reported ``NOT_CHECKED`` rather than guessed.
    Booleans stay distinct from ints (``True != 1``) because C ``bool`` and
    ``uint16_t`` are different observation domains.
    """
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("num", float(value))
    if isinstance(value, str):
        return ("str", value)
    return ("opaque", repr(value))


def _manifest_case(entry: dict[str, Any], args: Any) -> dict[str, Any] | None:
    """The manifest case whose ``args`` match a planned case, or ``None``.

    Matching is by the **input arguments**, because the expected outcome depends on
    the inputs (a boundary input and an interior input of the same function have
    different correct results). An arg-list that the manifest does not author is
    reported ``NOT_CHECKED`` — never silently compared against a different case's
    reference.
    """
    target = [_norm(a) for a in (args or [])]
    for case in entry.get("cases", []) or []:
        if [_norm(a) for a in (case.get("args") or [])] == target:
            return case
    return None


def assess_oracles(plan: dict[str, Any],
                   manifest: dict[str, Any]) -> list[OracleAssessment]:
    """Assess each planned observable against the independent manifest (R04).

    For every scenario the plan declares, this compares its **concrete expected
    values** (the return value and each ``expect_out`` observable) with the
    manifest's independent expectation for the *same function and inputs*. The
    plan is read, never written; the generated tests are never inspected and never
    changed.
    """
    functions = manifest.get("functions", {}) or {}
    assessments: list[OracleAssessment] = []
    for scenario in plan.get("scenarios", []) or []:
        fn = str(scenario.get("function", ""))
        reqs = [str(r) for r in scenario.get("requirement_ids", []) or []]
        oracle = scenario.get("oracle") or {}
        kind = str(oracle.get("kind", "UNDETERMINED")).upper()
        entry = functions.get(fn) or {}
        anchor = ""
        for a in scenario.get("anchors", []) or []:
            anchor = str(a.get("statement", "")) or anchor
        cases = scenario.get("cases", []) or []
        # An UNDETERMINED oracle carries no normative value (plan.py strips it):
        # there is nothing to be justified, and claiming SUPPORTED would invent
        # one. Report it INDETERMINATE, never SUPPORTED.
        if kind == "UNDETERMINED" or not reqs:
            assessments.append(OracleAssessment(
                scenario=str(scenario.get("id", "?")), function=fn,
                requirement_ids=reqs, observable="return",
                expected=None, reference=None, result=INDETERMINATE,
                explanation=("the SDD states no normative value for this "
                             "scenario, so no expectation can be justified"),
                anchor=anchor))
            continue
        for index, case in enumerate(cases):
            for observable, expected, reference, ref_present in _observables(case, entry):
                if expected is None:
                    result, why = NOT_CHECKED, (
                        "the case declares no value for this observable to justify")
                elif not ref_present:
                    result, why = NOT_CHECKED, (
                        "the independent manifest states no reference value for "
                        "this function and input set, so entailment cannot be checked")
                elif _norm(expected) == _norm(reference):
                    result, why = SUPPORTED, (
                        "the planned value matches the independently derived SDD "
                        "outcome")
                else:
                    result, why = CONTRADICTED, (
                        "the planned value disagrees with the SDD's independent "
                        "outcome despite a valid requirement id")
                assessments.append(OracleAssessment(
                    scenario=str(scenario.get("id", "?")), function=fn,
                    requirement_ids=reqs, observable=observable, expected=expected,
                    reference=reference, result=result, explanation=why,
                    anchor=anchor, case_index=index))
    return assessments


def _observables(case: dict[str, Any],
                 entry: dict[str, Any]) -> list[tuple[str, Any, Any, bool]]:
    """``(observable, expected, reference, reference_present)`` for one case.

    The return value and every declared ``expect_out`` observable are assessed
    **individually**, against the manifest case for the same inputs.
    """
    ref_case = _manifest_case(entry, case.get("args"))
    rows: list[tuple[str, Any, Any, bool]] = []
    ref_return = ref_case.get("return") if ref_case else None
    rows.append(("return", case.get("expected"), ref_return, ref_case is not None
                 and "return" in ref_case))
    ref_outs = (ref_case or {}).get("expect_out") or {}
    for key, value in (case.get("expect_out") or {}).items():
        present = str(key) in ref_outs
        rows.append((f"out[{key}]", value, ref_outs.get(str(key)), present))
    return rows


def oracle_summary(assessments: list[OracleAssessment]) -> dict[str, Any]:
    """Aggregate R04 results; ``CONTRADICTED`` is an oracle-quality discrepancy."""
    counts = {SUPPORTED: 0, CONTRADICTED: 0, INDETERMINATE: 0, NOT_CHECKED: 0}
    for assessment in assessments:
        counts[assessment.result] = counts.get(assessment.result, 0) + 1
    return {
        "counts": counts,
        "assessed": len(assessments),
        "discrepancies": [a.to_dict() for a in assessments
                          if a.result == CONTRADICTED],
        # An unsupported/contradicted oracle never substantiates a conformity or
        # a code-defect verdict; the summary says so explicitly.
        "note": ("oracle-quality conclusions only; CONTRADICTED/INDETERMINATE "
                 "never proves a C defect and SUPPORTED never proves conformity"),
    }


# --- R22 --------------------------------------------------------------------


@dataclass
class DefectOutcome:
    """The R22 classification of one independently seeded functional defect."""

    defect_id: str
    fixture: str
    location: str
    kind: str                # the seeded defect class (e.g. "off_by_one")
    reference: str           # the withheld reference outcome
    correct_status: str = ""
    faulty_status: str = ""
    result: str = NOT_EVALUABLE
    reason: str = ""
    commands: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "defect_id": self.defect_id,
            "fixture": self.fixture,
            "location": self.location,
            "kind": self.kind,
            "reference": self.reference,
            "correct_status": self.correct_status,
            "faulty_status": self.faulty_status,
            "result": self.result,
            "reason": self.reason,
            "commands": self.commands,
        }


def classify_defect(*, correct_status: str, faulty_status: str,
                    evaluable: bool = True) -> tuple[str, str]:
    """Classify one defect from the two campaigns' statuses (R22 step 3).

    * the reference-correct variant must PASS;
    * the faulty variant must NOT pass (a relevant assertion fails);
    * ``DETECTED`` requires **both**; the faulty variant passing is ``MISSED``;
      the correct variant failing is ``FALSE_POSITIVE`` (an unjustified failure).

    ``NOT_EVALUABLE`` when either campaign could not reach a clean functional
    verdict — a build failure or tool fault is never a detection.
    """
    if not evaluable or correct_status == "TOOL_FAILED" or faulty_status == "TOOL_FAILED":
        return NOT_EVALUABLE, ("a campaign could not be executed to a functional "
                               "verdict (build/tool fault is not a detection)")
    if correct_status != "PASSED":
        return FALSE_POSITIVE, ("the reference-correct variant did not pass, so the "
                                "suite fails an assertion it should not")
    if faulty_status == "PASSED":
        return MISSED, ("the faulty variant passed the unchanged suite; no assertion "
                        "distinguished it from the correct reference")
    return DETECTED, ("the reference-correct variant passed and the faulty variant "
                      "failed under the same frozen tests/oracles")


def sensitivity_summary(outcomes: list[DefectOutcome]) -> dict[str, Any]:
    """Aggregate R22 outcomes over the **evaluable** defects only."""
    counts = {DETECTED: 0, MISSED: 0, FALSE_POSITIVE: 0, NOT_EVALUABLE: 0}
    for outcome in outcomes:
        counts[outcome.result] = counts.get(outcome.result, 0) + 1
    evaluable = counts[DETECTED] + counts[MISSED] + counts[FALSE_POSITIVE]
    return {
        "counts": counts,
        "attempted": len(outcomes),
        "evaluable": evaluable,
        "detected": counts[DETECTED],
        "outcomes": [o.to_dict() for o in outcomes],
        # A finite sample is not a universal quality measure (R22 step 4).
        "note": ("finite-sample qualification; a detected ratio is not a guarantee "
                 "and a NOT_EVALUABLE case is excluded from the denominator"),
    }


def parse_manifest_defects(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """The withheld seeded-defect ground truth from the manifest (R22)."""
    defects: list[dict[str, Any]] = []
    for fixture, entry in (manifest.get("fixtures") or {}).items():
        for defect in (entry.get("defects") or []):
            defects.append({"fixture": fixture, **defect})
    return defects


def frozen_suite_hash(bench_root: Path) -> dict[str, Any]:
    """Hash the frozen test files and the project.yml, to pin what a campaign ran.

    Empty when the bench has no frozen suite yet, so a caller can require a
    non-empty hash before trusting a comparison.

    Note: we deliberately do **not** hash ``plan.json`` here. The plan carries
    the ``suite_hash`` field (a fingerprint of this dict), so hashing plan.json
    would create a circular dependency. The plan's own integrity is covered by
    the input snapshot_hash; the suite_hash authenticates the generated C files.
    """
    from .ceedling import REVISION_DIRNAME
    from .paths import sha256_file

    revision = bench_root / REVISION_DIRNAME
    tests = sorted((revision / "test").glob("test_*.c")) if (revision / "test").is_dir() else []
    interfaces = sorted((revision / "interface").glob("*_interface.h")) \
        if (revision / "interface").is_dir() else []
    mocks = sorted((revision / "mock").glob("mock_*.h")) \
        if (revision / "mock").is_dir() else []
    plan = revision / "project.yml"
    return {
        "tests": {p.name: sha256_file(p) for p in tests},
        "interfaces": {p.name: sha256_file(p) for p in interfaces},
        "mocks": {p.name: sha256_file(p) for p in mocks},
        "project_yml": sha256_file(plan) if plan.is_file() else "",
    }


def write_qualification_report(evidence_dir: Path, *,
                               oracle: dict[str, Any] | None = None,
                               sensitivity: dict[str, Any] | None = None,
                               fingerprints: dict[str, Any] | None = None) -> Path:
    """Persist an immutable qualification report under *evidence_dir*."""
    payload = {
        "schema_version": 1,
        "kind": "qualification",
        "fingerprints": fingerprints or {},
        "oracle_assessment": oracle,
        "suite_sensitivity": sensitivity,
        "note": ("R04/R22 quality conclusions over an unchanged frozen plan/suite; "
                 "no plan, oracle, test or native run record is modified"),
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    target = evidence_dir / "qualification.json"
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target
