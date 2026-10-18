# Third-party components — Software Bill of Materials (SBOM)

Versioned inventory of every third-party component the product ships or invokes.
Reviewed like code: any PR that adds or changes a dependency updates this file.
Machine-readable companion: [`toolchain.lock`](toolchain.lock).

**Rule:** Dagu and every other third-party tool stays a **separate executable**,
never linked into the product code (see `worklog/decisions/0003-third-party-licenses-and-dagu.md`).
Full licence texts are shipped inside the image under `/usr/share/doc/<component>/`.

| Component | Version / pin | Licence (SPDX) | Runs in | Provenance |
| --- | --- | --- | --- | --- |
| Node.js base image | `node:24.21.0-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20` | various (MIT, BSD, GPL for Debian components) | image base | Docker Hub official |
| Dagu | `v2.20.1`, tar.gz sha256 `0d5c1612cd842931fe6d4e598fc2e43fb912a94122e571c62a8f73691ea49837` | **GPL-3.0-only** | image (separate binary) | github.com/dagu-org/dagu |
| Claude Code | `2.1.302` (npm `@anthropic-ai/claude-code`) | Anthropic commercial terms (see shipped LICENSE) | image (separate CLI) | registry.npmjs.org |
| Claude Code Router (CCR) | `3.1.3` (npm `@musistudio/claude-code-router`) | MIT | image (separate CLI) | registry.npmjs.org |
| Ruby | `3.1` (distro `ruby` + `ruby-dev`) | Ruby / BSD-2-Clause | image (runtime for Ceedling) | debian package |
| Ceedling | `1.1.10` (gem) | MIT | image (separate CLI; drives the generated functional suite) | rubygems.org |
| Unity | `2.7.2` (vendored **by Ceedling**, not by this repo) | MIT | image (compiled into the generated suite only) | github.com/ThrowTheSwitch/Unity |
| CMock | `2.7.2` (vendored **by Ceedling**, not by this repo) | MIT | image (generates the mocked dependencies for the generated suite) | github.com/ThrowTheSwitch/CMock |
| GCC / libc6-dev / make | distro packages | GPL-3.0-or-later (GCC runtime exception) | image (C toolchain used by Ceedling) | debian package |
| Python | `3.11+` (host floor), `3.12` in image | PSF-2.0 | host CLI + image | debian package |
| PyYAML | `>=6.0` | MIT | host CLI + image | PyPI |
| jsonschema | `>=4.0` | MIT | host CLI + image | PyPI |
| git | distro package | GPL-2.0-only | image | debian package |
| curl | distro package | curl (MIT-like) | image (build + harness readiness) | debian package |

## Distribution note (GPL obligations)

GPL-3.0 obligations for Dagu (licence notice + source availability) trigger on
**distribution** of a built image. A purely local build triggers none. Because
Dagu is invoked as a separate process and never linked into the product, this is
mere aggregation and does not extend the GPL to the product code. If images are
ever published, the bundled `/usr/share/doc/dagu/LICENSE` plus the source pointer
above satisfy the notice requirement. Unity and CMock are MIT and are compiled
only into the **generated** test binaries (never into the product code); they are
vendored inside the image by Ceedling, not stored in this repository.

## Not yet bundled (later milestones)

Clang/LLVM, sanitizers, Mull, libFuzzer, Valgrind (PR 3/4). Each addition
updates this SBOM in the same PR.
