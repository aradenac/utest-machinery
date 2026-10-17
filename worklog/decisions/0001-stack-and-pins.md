# Decision record 0001 — PR 1 stack, pins and bench layout

- **Status:** accepted 2026-10-17; Dagu-license, SBOM and Python points refined by
  [`0002-python-version-policy.md`](0002-python-version-policy.md) and
  [`0003-third-party-licenses-and-dagu.md`](0003-third-party-licenses-and-dagu.md)
- **Date:** 2026-10-17
- **Supersedes:** none

## Context

The repository is a specification baseline with no product code. The implementation plan
requires six feature milestones; the plan explicitly does **not** impose an implementation
language. PR 1 must deliver a working headless CLI, a Docker image check, a persistent bench,
a Dagu-orchestrated minimal workflow with stop/error diagnostics, and an initialized Beads
backlog.

## Decision

**Components and pins**

| Component | Pin | License | Where |
| --- | --- | --- | --- |
| Dagu | `v2.20.1` (released 2026-10-14) | GPL-3.0 | separate executable; linux_amd64 tarball + `checksums.txt` |
| Docker base | `ubuntu:24.04`, digest `sha256:68ee2200…aaff33ac` | — | pinned digest |
| Python (CLI) | floor **3.11** (local dev 3.14.4) | PSF | CI on 3.11; keep CLI 3.11-compatible |
| Claude Code | `@anthropic-ai/claude-code@2.1.302` | proprietary (host route) | in-image, PR 2 |
| CCR | `@musistudio/claude-code-router@3.1.3` | MIT | in-image, PR 2 |
| Model route | `deepseek-v4.1-flash` | — | in-image; container runs its own CCR |
| Beads | `bd` 1.3.1, embedded Dolt | Apache-2.0 | dev-only, never in the user image |

**Interface and layout**

- CLI command name `mtm`; image tag `utest-machinery/mtm:<version>`.
- Bench root = **this repository** (the owning repo). `mtm init` writes a versioned
  `project.yaml` at the root plus a `state/` journal; defaults are overridable.
- PR 1 implements `install`, `doctor`, `init`, `scope list/add/remove`, `status`,
  `workflow view` and the error/exit contract. `plan/generate/verify/interpret/report`
  are declared but **NOT_TESTED** until their own milestones.
- Error contract follows `docs/architecture/workflow-failures.md`: exit codes
  `0/10/20/30/40/130`, `state/last-operation.json`, structured per-step results. The Dagu
  wrapper maps Dagu statuses to mtm's so `continue_on` can never surface as PASS.
- Add `.gitignore` for bench `build/` and Beads local DB/Dolt working state, while keeping
  `.beads/issues.jsonl` and Dolt refs versioned.
- `doctor` treats host clang/valgrind absence as a **warning**, not a failure (in-image
  tools arrive at PR 3).

**Beads**

- `bd init --skip-agents`; one parent work item per milestone (6) with inter-milestone
  dependencies; optional subtasks. Export `.beads/issues.jsonl` in the PR and document the
  pinned-version recovery procedure; use `bd dolt push/pull` against the repo git remote
  for cross-machine sync. No GitHub issues.

## Alternatives considered

- **CLI language:** Python 3 (chosen) — matches the existing `scripts/validate_baseline.py`,
  fast to iterate, stdlib-only viable for PR 1. A compiled CLI was not required by the spec.
- **Workflow engine:** Dagu (preferred candidate per the spec) versus Prefect / a small
  Python state machine. Dagu stays the choice pending the binary gate proof; fall back only
  with documented evidence.
- **Bench root:** this repository versus a separate bench repo. This repo is chosen because
  the plan defines one owning repository per workspace and this is it.

## Consequences

- PR 1 delivers a bootstrap capability plus its failure paths, not a test engine.
- The Dagu license decision (embed vs. fetch-at-build) is unresolved; see `questions.md` D2.
- The gate proof requires running the Dagu release binary, currently not authorized (D3).

## References

- `docs/plan/implementation.md` (feature gates), `docs/development/setup.md` (credentials,
  Beads), `docs/architecture/workflow-failures.md` (error contract),
  `docs/architecture/workflow-engines.md` (Dagu gate).
