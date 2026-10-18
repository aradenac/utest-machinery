# Decision record 0012 — Ceedling + CMock adopted (supersedes 0010)

- **Status:** implemented (PR #14 review pass, 2026-10-18)
- **Date:** 2026-10-18
- **Supersedes:** [0010](0010-generated-suite-framework.md) (vendored Unity only, no Ceedling/CMock)

## Outcome (implemented 2026-10-18)

Implemented as described below. `mtm/gen.py` emits a standard Ceedling project;
`mtm/ceedling.py` runs `ceedling test:all` in the product image (Ruby + Ceedling + gcc);
`mtm/vendor/unity/` and the hand-written mock/runner emitters are removed. Verified
end-to-end: `pytest -m docker` → 9 passed; `pytest -m "not docker"` → 121 passed, 2 skipped;
the five fixtures generate and execute through Ceedling in the image (saturation detects the
seeded SAT-002 defect as `Expected 10 Was 9`). Exit contract aligned to
`workflow-failures.md` (SUCCEEDED 0 / FINDINGS 10 / PARTIAL 20 / TOOL_FAILURE 30).

## Context

[0010](0010-generated-suite-framework.md) recorded an *agent* decision to vendor Unity core
only and emit tests/mocks/runner deterministically, deviating from the milestone-2 scope,
which names **Ceedling/Unity/CMock** (`docs/plan/implementation.md`,
`docs/architecture/verification-strategy.md`, R08). PR #14 review finding **B1** established
that a significant toolchain departure is a product decision, not a routine implementation
detail (R20), and required either implementing the approved contract or explicit owner
arbitration.

## Owner arbitration (2026-10-18)

The owner did **not** validate dropping Ceedling/CMock; decision 0010 (agent-authored) does
not replace the approved scope. The owner chose to **implement Ceedling + CMock**, reusing the
existing inventory, plan, oracle and grounding work, without needlessly rewriting the other
components. If a real technical incompatibility had made Ceedling/CMock disproportionate, the
concrete evidence and alternatives were to be presented before any further architecture
change.

## Feasibility evidence (measured before committing)

Run in a throwaway `debian:12-slim` + `gem install ceedling` container (not the product image):

- Ceedling `1.1.10` installs and runs; it vendors Unity **2.7.2** and CMock **2.7.2**.
- A real project (`src/**`, `test/**`, `interface/**`) built and ran the mocked-sensor unit:
  **3/3 PASS**; a deliberately wrong assertion produced
  `test.c:15:test_x:FAIL: Expected 2 Was 0`, exit 1, and Unity's `N Tests M Failures` summary.
- CMock generates mocks from a **header**. Producing an isolated per-dependency header and a
  small `interface/` directory gives exactly the control surface the generator needs:
  `_ExpectAnyArgsAndReturn`, `_ExpectAndReturn` + `_IgnoreArg_<p>`, `_IgnoreAndReturn`,
  `_ReturnThruPtr_<p>`, and CMock's built-in zero-call strictness (no expectation ⇒ an
  unexpected call fails the test).
- Required plugins: `:ignore`, `:expect_any_args`, `:return_thru_ptr`, `:ignore_arg`,
  `:callback`. `_ExpectAnyArgsAndReturn` is only emitted with `:expect_any_args`.
- The whole run works as the **invoking user** (`docker run --user`) with `HOME=/tmp`.

So there is **no technical incompatibility**: the approved contract is implemented as a
generation *backend* behind the existing plan, and the useful PR-2 work is preserved.

## Decision

- **Ceedling + CMock drive the functional suite.** The deterministic generator no longer emits
  mock or runner C: it writes a Ceedling project — `project.yml`, the selected scope copied
  under a private revision directory, one header per mocked dependency under `interface/`, the
  private shims under `shim/`, and one Unity test file per unit under `test/`. CMock generates
  the mocks from the dependency headers; Ceedling generates the runner, compiles, links and
  executes.
- **Ceedling runs inside the product image**, as the invoking user, with the bench mounted
  read-write (only `<bench>/.mtm/gen` is written; the selected inputs are never touched). Ruby
  + the pinned gem are image components; the host needs no Ruby.
- **The plan stays the oracle.** The generator still refuses any case it cannot materialise
  faithfully (see [0011](0011-llm-harness-pure-completion.md) and `gen._case_problem`) and
  records a limitation instead of a defaulted assertion.
- **The revision directory is rebuilt each run**, so Ceedling only ever sees the current
  plan's scope (no stale artefacts), and BLOCKED units are excluded while remaining visible in
  the feasibility report and denominator (R03).

## Alternatives considered

- **Keep 0010's vendored-Unity generator** — rejected by owner arbitration: it is a scope
  change, not an implementation detail.
- **CMock only, no Ceedling** (drive CMock and gcc ourselves) — still a Ruby dependency for
  the mock generation while re-implementing the runner/link/orchestration Ceedling already
  provides; and it would keep the "custom engine" the review flagged.
- **Host Ruby/Ceedling** — would make every user install a Ruby toolchain on the host and
  would break the "execution happens in the container" boundary established in PR 1.

## Consequences

- `SBOM.md` / `toolchain.lock` gain Ruby and the Ceedling gem (MIT); Unity/CMock now arrive
  **through** Ceedling (2.7.2) rather than being vendored.
- `mtm/vendor/unity/` and the hand-written mock/runner emitters are removed.
- Generation's smoke step now requires Docker (the image); `generate` reports a clear
  `TOOL_FAILED` when the image is absent. Tests that exercise execution are Docker-gated, and
  the emission stage (project/test C) stays offline-testable.
- The generated project is a normal Ceedling project, so a developer can inspect and extend it
  with the standard tool.

## References

- `docs/plan/implementation.md` (milestone 2), `docs/architecture/verification-strategy.md`,
  R08; PR #14 review B1
- [0010](0010-generated-suite-framework.md) (superseded), [0011](0011-llm-harness-pure-completion.md), [0004](0004-in-image-llm-route.md)
