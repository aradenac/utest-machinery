# Decision record 0003 — third-party licenses (SBOM) and non-contaminating Dagu

- **Status:** accepted (user decision, 2026-10-17)
- **Date:** 2026-10-17
- **Supersedes:** none

## Context

Dagu is GPL-3.0. The spec requires Dagu to remain a **separate executable**
(`docs/architecture/workflow-engines.md`) and to record the license of every bundled tool
(SYS-037, `docs/architecture/verification-strategy.md`). The user asked to keep Dagu
non-contaminating and to record all components (version + license) in a versioned file, like
an SBOM.

## Decision

- **Dagu stays a separate executable**, invoked as a distinct process. It is never linked
  into, imported by, or vendored into the product CLI/engine. This is mere aggregation, not
  a derived work, so the GPL does not extend to the product code.
- **The product CLI never depends on Dagu.** It lives behind an engine adapter with a small
  documented interface, so a fallback engine (per `workflow-engines.md`) can replace it
  without touching the CLI. This is the operational meaning of "non-contaminating".
- **Third-party inventory (SBOM):** maintain a versioned file in the repository recording,
  per component: name, exact version/tag or image digest, license (SPDX id), where it runs
  (host / image), and a source pointer or checksum. It is updated in the same PR that
  introduces or changes a dependency, and reviewed like code.
- **License notices:** every bundled third-party license text is stored under a versioned
  `LICENSES/` directory, and the SBOM references it. The SBOM file lands **inside the
  `worklog/` tree and its sibling `LICENSES/`**, keeping the original `docs/` structure
  untouched.
- **Distribution rule recorded:** GPL obligations (license + source availability) trigger on
  **distribution** of a built image. A purely local build triggers none. If images are ever
  published, the Dagu notice + source pointer in the SBOM/`LICENSES/` satisfies the
  aggregation requirements. Whether the project publishes images is still to confirm with the
  user; the SBOM is written to be correct either way.

## Alternatives considered

- **Embed the pinned Dagu binary in the repo** with a license notice: reproducible and
  offline, but stores a large third-party binary in Git history.
- **Fetch at image-build time** with a verified checksum: lightweight repo, but the build
  needs network and provenance depends on upstream. Either way the built image still
  *contains* Dagu; the aggregation/distribution analysis is identical.
- **Remove Dagu from the image**, install into the bench at `init`: fully free image, but
  departs from the documented topology and needs an explicit documented deviation.

**Adopted:** fetch-at-build with a pinned checksum recorded in the SBOM (lightweight repo,
pinned provenance), Dagu as a separate process, notice + source pointer versioned. Exact
placement of the SBOM file and the `LICENSES/` contents is fixed when the image is built
(PR 1 `install`), not before.

## Consequences

- A new mandatory PR step: any dependency change updates the SBOM + `LICENSES/`.
- The engine adapter boundary becomes real code in PR 1, not a placeholder.

## References

- `docs/architecture/workflow-engines.md`, `docs/architecture/architecture.md` (SYS-037),
  `docs/architecture/verification-strategy.md` (toolchain gate),
  [`0001-stack-and-pins.md`](0001-stack-and-pins.md).
