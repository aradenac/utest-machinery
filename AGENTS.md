# Instructions for the implementation agent

Read in order: [product goals](README.md) → [architecture and requirements](docs/architecture/architecture.md) → [contracts](docs/architecture/data-contracts.md) → [autonomy rules](docs/architecture/automation-policy.md) → [implementation plan](docs/plan/implementation.md) → [qualification](docs/qualification/ci-and-manual-tests.md).

## Working contract

- Implement the **entire plan sequentially**, with **one reviewable PR per numbered stage**, against `main`; no GitHub issues as a substitute. Use `feat/stage-01-...` etc. Wait for the previous stage's PR to be merged and its gate accepted before depending on it. **Do not self-merge** unless explicitly instructed.
- Each PR must implement the stage's complete exit criteria, its tests, evidence, CLI/docs/schema changes and any needed fixture updates. Include exact reproduction commands and observed results; never claim unexecuted checks passed.
- Treat every selected input C/SDD as read-only. Source-specific work belongs in the project's single test-bench directory; the product repository contains generic implementation and qualification fixtures.
- Preserve independent expected outcomes: never expose `examples/expected-outcomes.md` to the **generation** harness during blind qualification.
- Never repair a failing test by adjusting the SDD-derived oracle to match the code. Categorize ambiguous, untestable, unsupported and environment failures explicitly.
- Preserve and reuse existing Ceedling/LLVM/Mull/libFuzzer/Valgrind tools. Do not implement a custom coverage, mutation or fuzz metric.
- Workflows must be headless, textual, resilient and visually inspectable. Dagu is a candidate subject to a measured acceptance gate; retain alternatives.
- Credentials are per-user or protected CI secrets, outside repositories, image layers and logs. Limit container execution permissions and tool calls.
- Use offline deterministic checks in every ordinary PR; credentialed real-model qualification is separate.
- Keep docs compact. Update requirements/contracts, public CLI, workflow diagrams and verification checks in the same PR as code.

## Required PR handoff

The [implementation plan](docs/plan/implementation.md) contains the authoritative stage gates. For every PR report: scope and decisions; changed artifacts; requirement IDs covered; exact automated/manual commands with PASS/FAIL/SKIP; sample evidence locations; input-preservation proof; remaining risk; dependency and next-stage readiness. Block progression for missing or failing mandatory gates. The human/assistant can review the resulting PR and tests in a later turn; no background review is implied.

## Status

The repository is a **specification and fixture baseline**, not an implemented test platform. Commands and acceptance procedures below are contractual targets until introduced by the relevant stage.
