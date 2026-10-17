# Decision record 0002 — Python version policy

- **Status:** accepted (user delegated the choice, 2026-10-17)
- **Date:** 2026-10-17
- **Supersedes:** none

## Context

Constraints stated by the user:

1. reasonably old floor so most environments can run the CLI;
2. forward-compatible: a newer Python must not fail;
3. recent enough to use recent release versions of the Python-based tools (primordial).

Observed environment: host 3.14.4; CI (`baseline-checks.yml`) pinned to 3.11; the only
current Python is `scripts/validate_baseline.py`.

## Decision

- **Floor: Python 3.11.** Widely available (Ubuntu 23.10+/24.04, Debian 12+), supported to
  late 2027, and every current release of the planned deps installs on it.
- **CLI dependencies kept minimal:** `PyYAML` + `jsonschema` (both support >= 3.9). Prefer
  the standard library elsewhere. This removes the main forward-compatibility risk, since
  the CLI itself depends on nothing that could drop 3.11.
- **Forward compatibility is enforced by testing, not assumed:** CI matrix runs the CLI on
  **3.11 and the newest** available interpreter, so a newer Python cannot silently break.
- **The in-image Python is pinned independently** in the image build. It may be newer than
  the host floor; container tool versions are pinned by the image digest, decoupled from the
  host interpreter. This is how constraint (3) is met without dragging the host floor up.
- **No syntax above 3.11** in the CLI. Multiple concurrent Python versions are *not* needed:
  keeping the CLI dependency-light removes the reason to chase a newer floor.

## Alternatives considered

- **Floor 3.12** (Ubuntu 24.04 default): more "recent", but drops Ubuntu 22.04/WSL images
  still in common use and narrows constraint (1).
- **Floor 3.10**: broader, but loses the widely-deployed 3.11/3.12 default images and the
  forward window to 2027.
- **Multiple concurrent Python versions**: only justified if a required tool needs a version
  incompatible with the floor. Not the case once the CLIs stay dependency-light; not adopted.

## Consequences

- CI must run a **2-entry matrix** (3.11, newest) rather than a single version.
- Any future dependency that drops 3.11 forces a documented floor bump (a superseding
  decision record), not a silent change.

## References

- `docs/development/setup.md`, `.github/workflows/baseline-checks.yml`,
  [`0001-stack-and-pins.md`](0001-stack-and-pins.md).
