# 0018 — Resuming PR 3 on the approved Clang/libclang architecture

**Status:** accepted (coordinator, PR 3 resumption)
**Supersedes:** nothing. **Amends:** [0016](0016-share-safety-escalation.md) (resolves
its open owner arbitration); **realized by** owner decision
[0017](0017-clang-semantic-authority.md) (merged to `main`).

## Context

PR #35 was pushed as a **draft** with one open blocker: the §C interface-header
share-safety predicate (`_header_shares_safely`/`_simple_guard_span`, `mtm/gen.py`)
approximated the C preprocessor with regexes and took five mandatory holes on the same
class (F-A1…F-A6). Decision 0016 escalated the architecture question to the owner
rather than open a sixth pass.

The owner arbitrated (decision 0017, merged to `main` as `b4e57d5`): **Clang 18 /
libclang 18 is the sole semantic authority for C declarations and types**, with
**native Ceedling/CMock mock-header preprocessing**; the homegrown guard/branch/regex
classifiers must be retired; keep user-visible scope, preserve the already-verified
`verify`/R04/R22 work, and **do not** race comparative design spikes. Risk register
carries this as **R20/IMP-17/AP-08** and clarifies that the developer-convergence
failure is a **separate process axis** (R25/IMP-16/AP-07).

## Preflight facts (observed)

* Branch `feat/offline-verify-and-c-fidelity` rebased cleanly onto `origin/main`
  (`b4e57d5`); `CLAUDE.md`, `.claude/skills/`, `.claude/agents/` are **identical**
  to `main` after the rebase (no workflow sync required beyond the rebase).
* `mtm/gen.py` runs **on the host**; Ceedling runs **in the image**
  (`docker run --entrypoint ceedling … /bench:ro`). `mtm/image/toolchain.lock`
  already pins `clang-18 18.1.8` for PR 3 profiles but the image does **not** yet
  install the libclang python bindings.
* Host has `gcc` but **no clang/libclang** (`import clang.cindex` fails). Therefore
  the adapter must execute **in the pinned image**, like the Ceedling runner.
* In-container probe (`utest-machinery/mtm:1.0.0`): installing
  `python3-clang-18 libclang-18-dev` from the already-configured apt.llvm.org repo
  works; `clang.cindex` then resolves `typedef int *P` (depth 2 at `P *pp`),
  `typedef struct {…} S` / `typedef struct Tag S` (record identity), enums, and
  emits real diagnostics. `libclang-18.so` is already present in the base image.
* `scenario.mock_declarations` and `plan.shim_headers` come from the **LLM plan**,
  with `_prototype_for_symbol` (selected-header regex) only as a fallback — so a
  mocked interface's types are LLM-named but must be **Clang-validated**.

## Decision (coordinator)

Resume and update the **same** PR #35, implementing the approved architecture as one
directed plan, no comparative spikes:

1. **Adapter (lot A).** Add pinned `libclang-18-dev` + `python3-clang-18` to the
   image; a thin adapter returns a `CTypeResolution` (canonical parameter/return
   types, pointer depth, qualifiers, record/enum identity, alias→defining
   declaration + inline-body flag, diagnostics) for the selected build context.
   Explicit `BLOCKED` (missing context) vs `TOOL_FAILED` (broken clang).
2. **Generator (lot B).** Consume those facts on the type/argument chain and the
   interface-header path (include the original defining header for one shared
   definition per TU; fallback for `.c`-only aliases). **Delete**
   `_header_shares_safely`/`_simple_guard_span` and the guard-regex refusal; a
   sharing failure becomes a harness/context limitation. Retire `cscan` semantic
   type claims superseded by the adapter (keep lexical discovery only).
3. Keep `:use_test_preprocessor: :none` unless a concrete failing case requires
   `:mocks`; the approved plan frames it as "when needed", not a mandate.
4. Preserve every already-verified `verify`/R04/R22 capability untouched; rerun only
   the affected tests plus one integrated candidate qualification.

## Consequences

* The blocked F-A1…A6 class is closed by removing the mechanism (guard regex), not by
  a sixth patch; the interface-header decision becomes compiler-grounded.
* `mtm/gen.py` gains an image-bound dependency for type resolution; where the image
  or build context is absent/unusable, affected mocks are `BLOCKED` with an
  actionable diagnostic (never a silently-guessed type), matching AP-08.
* Decision 0016's option 3 (a sixth predicate pass) is now moot; option 1's "bound
  the claim" survives only as the honest `BLOCKED`/limitation path for genuinely
  unresolvable context.

## References

* Owner decision [0017](0017-clang-semantic-authority.md); escalation
  [0016](0016-share-safety-escalation.md); typedef path [0015](0015-typedef-fidelity-mock-path.md).
* `docs/plan/implementation.md` (revised PR 3 §B/§C + DoD 3), `docs/project/risks.md`
  (R20/IMP-17, AP-08), `docs/architecture/architecture.md` (C semantic authority),
  `verification-strategy.md`, `data-contracts.md` (`CTypeResolution`).
* Beads `utm-u8x`, lots `utm-u8x.1`, `utm-u8x.2`; worklog
  [`2026-10-18-pr3-resume.md`](../log/2026-10-18-pr3-resume.md).
