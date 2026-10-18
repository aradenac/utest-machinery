# 0013 — Offline `verify` profiles: per-profile subdirectory Ceedling projects

**Status:** accepted (PR 3, milestone 3)

## Context

`mtm verify` replays a **frozen** generated bench across several *profiles*
(functional, LLVM coverage/MC-DC, ASan/UBSan, clang `-O2`, Valgrind Memcheck). Each
profile re-runs the **same unchanged sources and tests** with a different compiler
or instrumentation. Ceedling needs a `project.yml` per profile (distinct
`:build_root:`, compiler `:tools:`, `:flags:`), but the base frozen
`project.yml` and the frozen tests must stay byte-identical — the whole point of a
frozen campaign is that the approved suite is not edited for a re-run.

## Options considered

1. **Rewrite the single frozen `project.yml` in place per profile.** Simplest, but
   it mutates the frozen revision between profiles and between runs: profile A's
   build config would be what profile B sees, the "frozen" project hash would
   change, and a validator could not tell a re-run from a re-generate. Rejected.
2. **A separate Ceedling project directory (copy the whole suite) per profile.**
   Guarantees isolation, but duplicates the frozen tests/mocks per profile, so the
   "same unchanged tests" claim would rest on a copy that could diverge. Rejected.
3. **A per-profile `project.yml` in a subdirectory of the frozen revision, with the
   profile's `:paths:` re-pointed up to the same frozen `src/`, `test/`, `shim/`,
   `interface/`.** The sources and tests the profile compiles are literally the
   frozen ones — one copy, one hash. Ceedling runs with CWD = the profile
   subdirectory. Accepted.

## Decision

Write `<revision>/profiles/<profile-id>/project.yml`, derived from the frozen
`project.yml`:

* `:build_root:` becomes the profile's own name (`build-coverage`, …), so two
  profiles never share objects and an instrumented build cannot reuse a plain one.
  It stays **local** to the profile directory, so `find .` finds that profile's
  built executables and its report artefacts are self-contained.
* Every relative `:paths:` list entry is prefixed with `../../` (the profile
  directory is two levels below the revision root), so the profile reaches the
  **same** frozen `src/**`, `test/**`, `shim/**` and `interface/**`. Ceedling
  symbols in the `:cmock:` plugin list (`:ignore`, `:return_thru_ptr`, …) are left
  untouched by matching only non-`:`-prefixed list items.
* `:tools:`/`:flags:` carry the profile's compiler and flags. A `:flags:` block is
  emitted only when a flag list is non-empty: Ceedling rejects an empty
  `:flags: :test: :link: :*` ("a nilclass"), which broke the flagless `-O2`
  profile.

The `profiles/` directory lives under the frozen revision (inside the bench, never
the inputs), is refreshed on every run so a profile config can never silently go
stale, and is excluded from the recorded snapshot (it holds no selected input).

## Consequences

* **Positive:** the frozen `project.yml` and the frozen tests are byte-identical
  before and after a verify run (proved by `tests/test_pr3_verify.py`), while each
  profile compiles the *same* files with its own toolchain. Raw native output goes
  into the run's own directory (`<run>/raw/<profile>.{out,err}`), so two campaigns
  never overwrite each other's evidence.
* **Cost:** the `../../` re-pointing is a stub of path arithmetic, not a full
  project config; a future non-list `:paths:` form would need re-derivation. Ceedling
  resolves `**` globs and relative paths from its CWD, verified in the image.
* **Coverage denominator:** the coverage profile splits vendored framework TUs
  (Unity/CMock and Ceedling's generated runners/mocks) out of the headline
  line/branch/MC-DC percentages into a separate `framework` bucket, so the reported
  figure is about the *unit under test*, not hundreds of framework lines (DoD 2:
  truthful denominators).

## References

* `mtm/verify.py` (`_profile_project_yml`, `_write_profile_projects`,
  `_coverage_metrics`), `mtm/profiles.py`.
* PR 3 contract §A/§C and DoD 2 in `docs/plan/implementation.md`.
* `docs/architecture/verification-strategy.md` ("instrumented builds are
  independent profiles"; "separate immutable runs").
