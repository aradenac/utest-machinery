# Decision record 0009 — packaged image build context (standalone `mtm install`)

- **Status:** accepted (2026-10-17, PR #5 review round 4)
- **Date:** 2026-10-17
- **Supersedes:** none. Replaces the round-2/round-3 choice that the image build
  context is the **source tree** (recorded in the round-2 worklog entry and the
  round-3 packaging test). The prior contract is now **reversed**.

## Context

PR 1 delivers `mtm install`, which builds the generic Docker image. Round 2 decided
the build context was the **source tree**: the wheel shipped the control CLI and its
data (schemas, sample DAG) but *not* a Docker context, and `mtm install` from an
installed wheel failed with `CONFIG_INVALID`/exit 40 and an actionable message. That
was accepted in round 3 as an explicit contract and tested.

Round 4 review is right that this is the wrong call for a **user product**: the
published workflow promises `mtm install`, so a source checkout must not be a hidden
prerequisite. A test proving the failure (exit 40) documents the gap but does not
deliver a working installed-product flow. The suggested escape hatch
(`mtm install --context <src>`) was also *syntactically wrong* — `--context` was a
global option and had to precede `install`.

## Decision

**Ship the image build context inside the package** at `mtm/image/`
(`Dockerfile`, `entrypoint.sh`, `toolchain.lock`, `SBOM.md`), declared as package
data (`[tool.setuptools.package-data] mtm = [..., "image/*"]`). `mtm install`
becomes autonomous:

- `build_image(tag)` assembles a fresh temporary context from the **installed
  package**: `mtm/image` → `image/`, and the `mtm` package directory → `mtm/` (the
  Dockerfile `COPY`s both), then runs `docker build -f image/Dockerfile` in it.
- `--context`/`DEFAULT_CONTEXT` are **removed**; the context is no longer a user
  input, and the build no longer depends on the current working directory.
- `image_context_dir()` / `image_context_files()` expose the packaged context;
  `base_image_digest()` reads the packaged `toolchain.lock`.

## Alternatives considered

- **Keep the source-tree requirement (status quo):** rejected — fails the product
  gate; the user workflow promises `mtm install`.
- **Fetch the context over the network at install time:** rejected — needs a network
  beyond Docker/PyPI, adds a second artifact to version and verify, and re-introduces
  a supply-chain surface for no benefit.
- **Fix only the `--context` syntax (make it a subcommand option):** rejected — treats
  the symptom; a manual path is still required, contradicting "no manual installs".
- **Embed the Dockerfile as a Python string:** rejected — unreadable and loses the
  separate, licence-annotated Dockerfile a reviewer can inspect.

## Consequences

- `mtm install` works from a wheel with no source checkout and no `--context`
  (verified end-to-end from `/tmp`; image rebuilt after `docker rmi`).
- The wheel grows by the four small context files; the Dockerfile still `COPY`s `mtm/`
  so the package source participates in the build.
- **Residual:** the build still needs Docker and network access to the Ubuntu base and
  the pinned Dagu tarball; that is inherent to building an image, not a packaging gap.
  Reproducibility is preserved by the digest-pinned base and the Dagu checksum.
- The round-3 packaging test that asserted the exit-40 failure is **replaced** by a
  test asserting the packaged context ships and is resolvable by the installed CLI.

## References

- `mtm/docker.py` (`PACKAGE_IMAGE_DIR`, `image_context_dir`, `image_context_files`,
  `build_image`, `base_image_digest`), `mtm/commands.py` (`cmd_install`),
  `pyproject.toml` (package-data), `mtm/image/Dockerfile`,
  `tests/test_packaging.py`, [`0003-third-party-licenses-and-dagu.md`](0003-third-party-licenses-and-dagu.md).
