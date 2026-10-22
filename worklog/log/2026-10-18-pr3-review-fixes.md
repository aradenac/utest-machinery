# 2026-10-18 PR 3 — Addressing review corrections (P1-2/3/4, P2-5)

## Scope
Address the 4 P1 and 1 P2 review comments on PR #35 (`feat/offline-verify-and-c-fidelity`),
resolving issues in `_valgrind_metrics`, `_run_functional`, `check_frozen`, and the PR body.
All fixes target `mtm/verify.py`, `mtm/plan.py`, and `mtm/generate.py`.

## Changes

### P1-2 / Valgrind false PASS (mtm/verify.py)

**Issue:** Two defects in the Valgrind profile:
1. `_valgrind_metrics` used `re.search` which only captured the FIRST `ERROR SUMMARY` line
   across multiple binaries — if several binaries each had 0 errors except one with 3, the
   result would incorrectly report 0 errors.
2. `_run_valgrind` ignored the Unity `FAILED` status from the Ceedling build when creating
   a new result after running Valgrind.

**Fix:**
- `_valgrind_metrics`: replaced `re.search` with `re.findall` to aggregate ALL `ERROR SUMMARY`
  lines across all Valgrind runs. Returns `sum(int(n) for n in summary_counts)` instead of
  just the first match.
- `_run_valgrind`: restructured to create `ProfileResult` with `status=TOOL_FAILED` as default,
  then set status/reason after all checks (abnormal exits, error counts). Preserved the `built`
  failure check so a failed build returns early with `built`'s result.

### P1-3 / Functional abnormal exit (mtm/verify.py)

**Issue:** `_run_functional` checked for a Unity summary and reported PASSED/FAILED based on
it, but did not check whether the Docker process exited abnormally (signal-death, OOM killer
SIGKILL=137, Docker daemon errors=125-127). An abnormal exit means the Unity summary is
unreliable, even if it was emitted.

**Fix:** Added exit-code prioritization in `_run_functional`:
- `rc >= 128`: signal death (e.g., OOM, SIGKILL) → `FAILED` with reason about unreliable summary
- `125 <= rc < 128`: Docker CLI/daemon errors → `FAILED` with reason about abnormal exit
- Normal exit (0-124): proceed with Unity summary parsing

### P1-4 / Replay integrity — suite authentication (mtm/verify.py, mtm/plan.py, mtm/generate.py)

**Issue:** `check_frozen` only validated the input snapshot hash. If someone modified a
generated test file, mock, interface header, or `project.yml` after generation but before
`verify`, the replay would still run against the tampered suite, invalidating Invariant 1
(offline replay truthfulness).

**Fix:** Three-module change:
1. **`mtm/plan.py`**: Added `suite_hash: dict[str, Any] = field(default_factory=dict)` to the
   `Plan` dataclass, with docstring explaining it's a SHA-256 fingerprint of the generated
   suite. Updated `to_dict()` to include `"suite_hash": self.suite_hash`.
2. **`mtm/generate.py`**: After generating the suite, call `frozen_suite_hash(bench_root)` to
   compute its fingerprint and store it in `plan.suite_hash`, then rewrite the plan via
   `write_json_atomic`.
3. **`mtm/qualify.py`**: Rewrote `frozen_suite_hash` to hash test files, interface headers,
   mocks, and `project.yml` (but NOT `plan.json` — to avoid circular dependency, since the
   plan.json now contains the `suite_hash` field which is a fingerprint of this dict).
4. **`mtm/verify.py`**: In `check_frozen`, after the snapshot check, call `frozen_suite_hash`
   to compute the current suite fingerprint and compare each file's hash against the stored
   value. On mismatch, raise `SNAPSHOT_MISMATCH` with action to regenerate.

### P2-5 / PR body cleanup

**Issue:** PR #35 body still documented the superseded `_header_shares_safely` design blocker
in the title, top notice, dedicated blocker section, and closing note.

**Fix:** Updated PR #35 title and body via `gh pr edit`:
- Title: removed "(draft: §C share-safety blocked)"
- Replaced the "⚠️ Open blocker for review: §C interface-header share-safety (escalated)" section
  with a "Resolved: §C interface-header share-safety (escalated → resolved)" section that
  documents the compiler-led probe resolution (Decision 0016).
- Updated "Remaining limitations" to reflect the resolved status.
- Updated closing note to remove the blocker dependency.

## Verification
- Full test suite: `python3 -m pytest -q` → 259 passed, 2 skips, exit 0
- PR 3 specific: `python3 -m pytest tests/test_pr3_verify.py tests/test_pr3_clang.py tests/test_pr2_runner.py -q` → all pass
- Docker-gated profile tests also pass (fixed circular dependency in suite hash)

## Files changed
- `mtm/verify.py`: `_valgrind_metrics`, `_run_functional`, `_run_valgrind`, `check_frozen`
- `mtm/plan.py`: `Plan.suite_hash` field + `to_dict()`
- `mtm/generate.py`: suite hash computation + plan rewrite
- `mtm/qualify.py`: `frozen_suite_hash` rewrite (exclude plan.json, add interfaces/mocks)
- PR #35: title and body via `gh pr edit`

## Status
Fixes committed to branch `feat/offline-verify-and-c-fidelity`. PR #35 updated. Waiting
for owner review and manual merge.
