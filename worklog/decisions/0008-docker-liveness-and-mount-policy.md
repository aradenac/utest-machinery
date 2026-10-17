# Decision record 0008 — docker run liveness and read-only bench mount

- **Status:** accepted (2026-10-17, PR #5 review round 3)
- **Date:** 2026-10-17
- **Supersedes:** none. Corrects the round-2 liveness and mount behaviour.

## Context

Two round-2 defects, both found by review:

1. **False INTERRUPTED.** The docker path passed `pid=0`; `_finalize` treated "no host PID"
   as "dead owner" and reconciled a *still-running* container to INTERRUPTED. A running run
   was indistinguishable from a killed one.
2. **Broad read-write mount.** The whole bench was mounted RW (`-v bench:/bench`). The bench
   may *be* the repository holding the selected C/SDD inputs, so a run could overwrite an
   immutable input — forbidden by the read-only source contract.

## Decisions

**Liveness (unknown ≠ dead).** `_pid_alive` now returns `bool | None`; `None` means "not
determinable" and is **not** reconciled. Reconciliation to INTERRUPTED happens only on a
positive `False`:

- **docker backend:** the owner container is labelled `mtm.run-id=<run_id>` at `docker run`;
  a later status query runs `docker ps --filter label=mtm.run-id=<id>`. Found ⇒ alive (RUNNING);
  absent ⇒ dead ⇒ INTERRUPTED; `docker ps` fails ⇒ unknown ⇒ RUNNING.
- **host backend:** the OS PID from the run state, as before.

Every `RunStatus` now carries `owner_alive` and `liveness_evidence` (a human-readable
justification, e.g. the container id or "host PID dead"), surfaced in `workflow status`.

**Mount policy (read-only inputs, writable state).**

```
-v <bench>:/bench:ro          # selected inputs: never writable
-v <bench>/.mtm:/bench/.mtm   # engine state: the only RW path
```

The `.mtm` directory is created before the mount so Docker does not create it as root. Dagu's
`DAGU_HOME` lives under `/bench/.mtm/dagu`, so it stays writable and bench-local.

## Alternatives considered

- **Keep `pid=0` → INTERRUPTED:** rejected — false interruption of live runs.
- **Parse the container PID from `docker inspect`:** rejected — container PIDs are namespaced;
  a label lookup is the portable, directly-checkable signal.
- **Mount the bench RW but only seed inputs RO:** rejected — cannot be expressed with a single
  bind mount, and the bench root *is* the input tree.
- **No `.mtm` mount, rely on the RO bench:** rejected — Dagu needs writable state.

## Consequences

- The `mtm.run-id` label is a stable, documented liveness handle.
- The container can no longer write outside `.mtm`; a workflow writing an input fails inside
  the container (covered by an integration test and the CI smoke).
- Residual: `docker ps` liveness assumes the same Docker context as the run; if the context
  changes the query returns "not found" → INTERRUPTED, which is the conservative reading.

## References

- `mtm/engine.py` (`DockerRunner.owner_alive`, `OWNER_LABEL`, `_pid_alive`, `_finalize`),
  `worklog/decisions/0006-workflow-backend.md`, `docs/architecture/architecture.md` (SYS-004).
