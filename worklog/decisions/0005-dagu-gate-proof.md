# Decision record 0005 — Dagu gate proof (measured, v2.20.1)

- **Status:** accepted 2026-10-17 — gate **passed with one documented limitation**
- **Date:** 2026-10-17
- **Supersedes:** the docs-level assessment in [`0001-stack-and-pins.md`](0001-stack-and-pins.md)

## Context

`docs/architecture/workflow-engines.md` requires the Dagu acceptance gate to be *proven*, not
assumed, before adoption: (1) no server/UI needed, (2) deterministic explicit failure
policies, (3) resumability after container interruption, (4) workflow/state export with no
hidden source of truth, (5) CLI progress + optional DAG display, (6) per-project state on a
host volume, (7) compatible image/license/version pinning.

## Artifact verified

| Item | Value |
| --- | --- |
| Source | `github.com/dagu-org/dagu`, release `v2.20.1` (2026-10-14) |
| Asset | `dagu_2.20.1_linux_amd64.tar.gz` |
| **sha256** | `0d5c1612cd842931fe6d4e598fc2e43fb912a94122e571c62a8f73691ea49837` (matches upstream `checksums.txt`) |
| Binary | `dagu version` → `2.20.1` |
| License | GPL-3.0 (bundled `LICENSE` is GPLv3; SBOM seed) |

Everything ran under an isolated `DAGU_HOME=/tmp/dagu-gate/home`; nothing touched the repo.

## Gate results

| # | Criterion | Result | Evidence |
| --- | --- | --- | --- |
| 1 | Headless, no server/UI | **PASS** | `validate`, `dry`, `start`, `status`, `history --format json`, `ps`, `retry` all operate against the local backend; no `server`/`start-all` invoked |
| 2 | Deterministic explicit failure | **PASS** | failing step → DAG `Failed`, CLI exit `1`, structured `error` field `"fail: exit status 3…"`, dependent step `aborted`; `dry` runs green |
| 3 | Resume after interruption | **PASS with limitation** | see below |
| 4 | State export, no hidden source of truth | **PASS** | all state under `DAGU_HOME/{dags,logs,data}`; per-run `status.jsonl` event log + `dag.json`; `history --format json` machine-readable |
| 5 | CLI progress + optional DAG display | **PASS** | `status` prints a tree of steps with durations/status; optional `server` UI, off by default |
| 6 | Per-project state on host volume | **PASS** | `DAGU_HOME` fully relocatable; benches can point it at the bench `state/` |
| 7 | License/version pinning | **PASS** | exact tag + sha256 + GPL-3.0 recorded; separate executable (non-contaminating, decision 0003) |

### Measured facts

- A DAG file passed **on the command line must not define `name:`** — `validate` rejects it
  (`entrypoint document must not define name`). The wrapper must use the file-path form.
- `--run-id <id>` gives deterministic run IDs; `retry --run-id <id>` re-runs under the **same**
  ID and inherits the attempt; it exits `1` when the retry fails again (no false success).
- Structured output: `history --format json` → `{name, dagRunId, status, startedAt,
  finishedAt, duration, workerId, error}`.
- On `auth.mode` unset, every invocation warns and defaults to `builtin`; headless use should
  configure `auth.mode: none` (or an explicit mode) to avoid the warning. Not a blocker.

### Limitation (gate item 3) — recorded, must be handled by the wrapper

After a **SIGKILL** of the Dagu process (simulating container termination), the run stays
recorded as status **`running`** permanently: `ps` keeps listing it as `FRESH`, `history`
keeps reporting `running`, and Dagu does **not** reconcile a dead owner's run on the local
backend. **A "running" run whose PID is dead is the interruption signal, and the mtm wrapper
must classify it `INTERRUPTED` — never `RUNNING`, never success** — satisfying
`docs/architecture/workflow-failures.md` (`WORKFLOW_INTERRUPTED`, "never a false success").
Dagu therefore **passes the gate but is not adopted blindly**: the wrapper owes an explicit
liveness/reconciliation step. This is a limitation of the engine, not of the gate.

## Decision

**Adopt Dagu `v2.20.1` as the PR 1 headless orchestrator**, behind the engine adapter
(decision 0003), with these mandatory wrapper behaviours: path-form DAG invocation,
deterministic `--run-id`, `DAGU_HOME` on the bench `state/`, `auth.mode: none` for headless,
and **PID-liveness reconciliation** mapping dead-owner `running` runs to `INTERRUPTED`.
No fallback engine is needed for PR 1.

## Alternatives considered

Fall back to Prefect or a small Python orchestrator. Not needed: every gate item passed with
the limitation handled in the wrapper, and the fallback would add a heavier service surface.

## References

- `docs/architecture/workflow-engines.md`, `docs/architecture/workflow-failures.md`,
  [`0001-stack-and-pins.md`](0001-stack-and-pins.md), [`0003-third-party-licenses-and-dagu.md`](0003-third-party-licenses-and-dagu.md).
