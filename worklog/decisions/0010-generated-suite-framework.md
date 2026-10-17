# Decision record 0010 — generated suite framework: vendored Unity, no Ceedling/Ruby

- **Status:** accepted (agent decision under R20, 2026-10-18)
- **Date:** 2026-10-18
- **Supersedes:** none

## Context

PR 2 must turn a grounded plan into an **executable** host-native C unit suite with
controllable mocks. The normative docs name **Ceedling/Unity/CMock**
(`docs/architecture/verification-strategy.md`) as the expected assembly, and the risk register
carries R08 (Ceedling/LLVM/Mull version incompatibilities: "do not reimplement these engines").

## Decision

Vendor **Unity 2.6.0 core** (`unity.c`, `unity.h`, `unity_internals.h`, MIT) under
`mtm/vendor/unity/` and generate the test file, the runner and the mocks **from the reviewed
plan** in deterministic Python. Ceedling and CMock are **not** used in PR 2.

## Justification (R20)

- **The generator cannot be delegated.** The acceptance criterion is "no silent oracle
  weakening": the executed assertion must be the value the *reviewed plan* carries, not a
  value a second toolchain re-derives from the code. Emitting the suite from the frozen plan
  is what makes that verifiable, and it is required regardless of framework.
- **Ceedling adds a Ruby toolchain for no coverage here.** Ceedling is a Ruby project
  manager; CMock is Ruby code generation. Both would be new image dependencies (Ruby gems, a
  network fetch, version pinning) whose only contribution is `TEST_ASSERT_*` macros and mock
  scaffolding that this repository already emits deterministically. Unity itself is a two-file
  C library, so vendoring it is cheap and auditable.
- **R08 is honoured, not contradicted.** The register warns against *reimplementing* these
  engines in the sense of clone-building a C test framework and mocking engine from scratch.
  The generated code is not a framework: the assertion macros, runner and registration come
  from upstream Unity verbatim; only the *test bodies and mocks for the selected unit* are
  rendered, which is exactly the artefact Ceedling/CMock would also produce.
- **Smaller maintenance surface.** No Ruby, no gem lockfile, no Ceedling version matrix to
  keep compatible with LLVM later (PR 3/4). Unity compiles with the same `cc` the rest of the
  pipeline uses.

## Alternatives considered

- **Ceedling + Unity + CMock as specified, Ruby in the image.** Faithful to the doc wording,
  but adds a Ruby toolchain and its version constraints for functionality already provided
  deterministically; risks the very R08 incompatibility the register warns about.
- **Unity + CMock without Ceedling** (Ruby only for mock generation). Still a Ruby dependency;
  the mock shapes actually needed (scalar/enum return, scalar/pointer parameters) are a few
  dozen lines of emitted C, and the generator refuses shapes it cannot render faithfully.
- **Vendor Unity and emit everything** (chosen). Smallest dependency set, maximal control over
  oracle fidelity.

## Consequences

- `SBOM.md` / `toolchain.lock` record Unity 2.6.0 (MIT) compiled **only** into generated test
  binaries, never into product code, and no Ceedling/CMock/Ruby component.
- The framework choice is recorded against `verification-strategy.md`'s Ceedling wording. If a
  later milestone needs a CMock-specific capability (e.g. full expectation ordering), revisit
  this record rather than silently diverging.
- The generator must **refuse** a plan shape it cannot materialise instead of emitting wrong
  C; that rule is [decision 0011](0011-llm-harness-pure-completion.md)'s counterpart.

## References

- `docs/architecture/verification-strategy.md` (Ceedling/Unity/CMock), `docs/project/risks.md` R08
- [0003](0003-third-party-licenses-and-dagu.md) (SBOM/licence policy), [0011](0011-llm-harness-pure-completion.md)
