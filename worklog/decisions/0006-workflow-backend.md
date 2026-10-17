# Decision record 0006 — workflow execution backend (docker default, host opt-in)

- **Status:** accepted (2026-10-17, PR #5 review round 2)
- **Date:** 2026-10-17
- **Supersedes:** none. Refines [`0003`](0003-third-party-licenses-and-dagu.md) (Dagu stays a
  separate executable) and [`0005`](0005-dagu-gate-proof.md) (gate proof).

## Context

PR #5 shipped `mtm workflow run` calling a **host** `dagu` binary found on `PATH`. Review
required the CLI to execute the workflow through the **built image** instead, so the default
user path needs no host Dagu install and matches the shipped toolchain. The adapter already
existed; the question was which backend is default and how the choice is expressed.

## Decision

`make_adapter(backend_name, ...)` selects the backend and the CLI exposes it:

- **`docker` is the default.** `DockerRunner` builds `docker run --user <uid>:<gid>
  -v <bench>:/bench ... <image> dagu ...`; Dagu state lives bench-local at
  `<bench>/.mtm/dagu` (never `~/.dagu`), so it survives container recreation and stays with
  the bench.
- **`host` is an explicit opt-in** via `--backend host` (with optional `--host-dagu <dir>`),
  kept for local debugging and for running without Docker.
- The image is required for the default path: a missing image is reported as `IMAGE_MISSING`
  (invalid-input category), never as a silent host fallback.

## Alternatives considered

- **Host Dagu as default:** rejected — the product ships an image precisely so the toolchain
  is pinned and reproducible; requiring a host binary inverts that and was the review finding.
- **No backend switch (docker only):** rejected — a host path is genuinely useful for
  bisecting an in-image problem and costs one flag/one class.
- **Auto-detect (docker if present, else host):** rejected — implicit fallback makes a run's
  environment non-obvious and can hide a missing image; the contract must fail loudly.

## Consequences

- The default path depends on Docker being available; `doctor` and the error contract make
  that an explicit, actionable failure.
- Bench ownership is handled by `--user` (see the R20 note on `utm-bu8`), not `chown`, so
  immutable selected sources are never re-owned.
- Tests: docker backend integration (skipped when the image is absent), host backend
  (skipped when no host dagu), and offline unit tests of the adapter always run.

## References

- `mtm/engine.py`, `mtm/commands.py` (`_adapter`, `make_adapter`),
  [`0003-third-party-licenses-and-dagu.md`](0003-third-party-licenses-and-dagu.md),
  [`0005-dagu-gate-proof.md`](0005-dagu-gate-proof.md), Beads `utm-bu8` comments.
