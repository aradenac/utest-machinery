# Decision record 0007 — workflow outcome taxonomy (typed step evidence)

- **Status:** accepted (2026-10-17, PR #5 review rounds 3–4)
- **Date:** 2026-10-17
- **Supersedes:** none. Refines [`0006`](0006-workflow-backend.md) (backend) and the error/exit
  contract in `docs/architecture/workflow-failures.md`.

## Context

Round 2 mapped **every** Dagu `FAILED` run to `TEST_FINDING`/`TEST_ASSERTION`/exit 10. Review
(round 3) is right that this is a wrong taxonomy: Dagu's `FAILED` says only "a step exited
non-zero". A crashed tool, a missing executable or a broken shell command is a
**workflow/tool failure** (exit 30), not a C test assertion. Only an actual test finding is
exit 10 (`workflow-failures.md`, exit table).

The wrapper must not guess: Dagu's node state does carry a per-step numeric status and an
`error` string, but nothing that classifies *why*. The classification has to come from the
step itself.

## Decision

A step **types its own outcome** by emitting an output variable named `MTM_CATEGORY`:

```yaml
- name: unit-check
  command: |
    echo MTM_CATEGORY=TEST_FINDING
    exit 1
  output: MTM_CATEGORY
```

The wrapper reads it from Dagu's own run state (`status.jsonl` → node `outputVariables`) and
maps only a small, explicit allow-list:

| Reported | Category | Code | Exit |
| --- | --- | --- | --- |
| `TEST_FINDING` | TEST_FINDING | `TEST_ASSERTION` | 10 |
| `INPUT_LIMITATION` | INPUT_LIMITATION | `HOST_UNSUPPORTED` | 20 |
| `TOOL` | TOOL | `TOOL_FAILED` | 30 |
| *(none / unknown)* | TOOL | `TOOL_FAILED` | 30 |

Untyped or unrecognised ⇒ **tool failure** (exit 30). The default is the *safe* one: never
invent a test finding from a generic shell exit status.

**Aggregation across steps (round-4 amendment).** A run has many steps, so the run-level
outcome must aggregate, not take the first typed failure. `RunStatus.dominant_outcome`
collects an outcome from **every** failed step — each step's recognised `MTM_CATEGORY`, or
the untyped TOOL outcome — and picks the one with the greatest `errors.SEVERITY`, matching the
contract's "**most severe wins, all findings retained**" rule. `category_hint` derives from the
dominant outcome. Consequence: a C assertion failure alongside a crashed tool reports the
**tool** failure (exit 30); the finding is not lost, it stays in `step_evidence`. Reporting only
the first typed step (the round-3 behaviour) let a co-occurring infrastructure failure hide
behind a test finding.

**Nonterminal observation (round-4 amendment).** `PENDING`/`RUNNING` are not failures at all:
a status consultation of a live run reports `OPERATION_RUNNING` and exit **0**, never
`OPERATION_FAILED`/exit 30. "Not finished" ≠ "failed".

## Alternatives considered

- **Custom step keys** (`mtm_kind:`): rejected — Dagu's validator rejects unknown keys
  ("`spec.step` has invalid keys"), so it cannot be carried in the DAG.
- **Map Dagu exit code / node status alone:** rejected — the evidence is untyped; that is the
  defect being fixed.
- **A file-based contract** (step writes a JSON next to its output): deferred — the output
  variable already rides Dagu's supported channel, survives into the run state, and needs no
  extra mount.

## Consequences

- Verification steps must emit `MTM_CATEGORY`; PR 2/3 tooling will do so. Until then a failing
  sample step is correctly a tool failure.
- `status.jsonl` under the bench is read host-side; a missing/corrupt file yields no evidence
  (untyped), which is safe.
- The run record exposes `step_evidence` so a reviewer can see the typed basis of the exit code;
  the run-level category is the **most severe** failed step's outcome.

## References

- `mtm/engine.py` (`CATEGORY_MARKER`, `REPORTED_CATEGORIES`, `_step_evidence_from_state`,
  `RunStatus.dominant_outcome`/`category_hint`),
  `mtm/commands.py` (`_apply_status_error`, `_exit_for_engine_status`, `Code.OPERATION_RUNNING`),
  `mtm/errors.py` (`SEVERITY`, `CONTRACT`),
  `docs/architecture/workflow-failures.md`, [`0006-workflow-backend.md`](0006-workflow-backend.md).
