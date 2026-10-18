# 0014 — R04/R22 qualification: separate, read-only conclusions over a frozen plan

**Status:** accepted (PR 3, milestone 3)

## Context

PR 3 adds two independent qualifications on top of `mtm verify`:

* **R04** — assess whether a generated oracle's *meaning* is entailed by the SDD,
  not merely whether its cited requirement id exists.
* **R22** — assess whether a generated suite distinguishes a correct C
  implementation from independently seeded defects.

Both must not modify the frozen plan, the approved oracles, the generated tests or
the native run records, and neither may run inside the offline `verify` command
(no LLM, no automatic oracle repair). The plan contract says to "expose the
assessment through the existing plan/qualification evidence, without a new public
command".

## Options considered

1. **A new `mtm qualify` command.** Clean CLI surface, but the plan explicitly
   forbids a new public command for this and wants the evidence surfaced through
   the existing surface. Rejected.
2. **Run the assessment automatically in `verify`.** Would make `verify` depend on
   an external manifest and blur the "raw execution ≠ qualification" boundary the
   contract draws. Rejected.
3. **A read-only module (`mtm/qualify.py`) exercised through the existing `plan`
   command (guarded by an explicit `MTM_QUALIFICATION_MANIFEST` path) and the test
   suite.** Accepted.

## Decision

`mtm/qualify.py` owns the R04/R22 conclusions as pure functions over already-frozen
data:

* **R04.** `assess_oracles(plan, manifest)` compares each planned observable
  (return, each `expect_out`) with an **independently authored** manifest, matched
  by function **and input arguments** (a boundary case and an interior case of the
  same function have different correct results). Results are
  `SUPPORTED_BY_SDD / CONTRADICTED_BY_SDD / INDETERMINATE / NOT_CHECKED`; a
  disagreement is an *oracle-quality discrepancy* that never becomes a code-defect
  claim. `bool` and `int` are kept distinct observation domains (C `bool` ≠
  `uint16_t`) even though Python compares `True == 1`.
* **R22.** `classify_defect(correct_status, faulty_status)` returns
  `DETECTED / MISSED / FALSE_POSITIVE / NOT_EVALUABLE`; a build/tool fault is never
  a detection, and `NOT_EVALUABLE` cases are excluded from the denominator.
  `frozen_suite_hash` pins the test bytes so a comparison can assert the *same*
  unchanged tests ran on both snapshots.
* The independent manifest lives at `examples/qualification-manifest.json`,
  deliberately **outside** the shipped package and never referenced by the plan
  prompt or any recorded response (QD-012: withheld from the generating LLM). A
  test asserts this separation.
* `plan` runs the R04 assessment only when `MTM_QUALIFICATION_MANIFEST` names a
  manifest explicitly; without it, no assessment appears (the manifest is never a
  silent input). The assessment is persisted to
  `.mtm/state/generate/oracle_assessment.json`; the frozen plan is byte-identical
  afterwards.

## Consequences

* **Positive:** the qualifications are reproducible, offline and read-only, and
  reuse the existing `plan`/`verify` surfaces. The R22 test fixtures
  (`examples/qualification/saturation-correct` vs the seeded-faulty
  `examples/saturation`) share one frozen suite, so a DETECTED result pins a real
  assertion difference, not a re-generate.
* **Cost / limit:** the manifest covers a finite fixture sample (R04 step 4); a
  finite detection ratio is explicitly *not* a universal quality measure, and the
  reports say so. Natural-language entailment that no independent ground truth
  exists for is `NOT_CHECKED`, not `SUPPORTED`.

## References

* `mtm/qualify.py`; `mtm/commands.py` (`_assess_oracles`).
* `docs/qualification/qualification.md` (R04, R22);
  `docs/architecture/data-contracts.md` (`OracleAssessment`).
* PR 3 DoD 4 in `docs/plan/implementation.md`.
