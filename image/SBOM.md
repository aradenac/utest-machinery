# Third-party components — Software Bill of Materials (SBOM)

Versioned inventory of every third-party component the product ships or invokes.
Reviewed like code: any PR that adds or changes a dependency updates this file.
Machine-readable companion: [`toolchain.lock`](toolchain.lock).

**Rule:** Dagu and every other third-party tool stays a **separate executable**,
never linked into the product code (see `worklog/decisions/0003-third-party-licenses-and-dagu.md`).
Full licence texts are shipped inside the image under `/usr/share/doc/<component>/`.

| Component | Version / pin | Licence (SPDX) | Runs in | Provenance |
| --- | --- | --- | --- | --- |
| Ubuntu base image | `ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac` | various (mostly GPL-2.0-or-later, MIT, BSD) | image base | Docker Hub official |
| Dagu | `v2.20.1`, tar.gz sha256 `0d5c1612cd842931fe6d4e598fc2e43fb912a94122e571c62a8f73691ea49837` | **GPL-3.0-only** | image (separate binary) | github.com/dagu-org/dagu |
| Python | `3.11+` (host floor), `3.12` in image | PSF-2.0 | host CLI + image | ubuntu package |
| PyYAML | `>=6.0` | MIT | host CLI + image | PyPI |
| jsonschema | `>=4.0` | MIT | host CLI + image | PyPI |
| git | distro package | GPL-2.0-only | image | ubuntu package |
| curl | distro package | curl (MIT-like) | image (build only) | ubuntu package |

## Distribution note (GPL obligations)

GPL-3.0 obligations for Dagu (licence notice + source availability) trigger on
**distribution** of a built image. A purely local build triggers none. Because
Dagu is invoked as a separate process and never linked into the product, this is
mere aggregation and does not extend the GPL to the product code. If images are
ever published, the bundled `/usr/share/doc/dagu/LICENSE` plus the source pointer
above satisfy the notice requirement.

## Not yet bundled (later milestones)

Claude Code + CCR (PR 2, see `worklog/decisions/0004-in-image-llm-route.md`);
Clang/LLVM, Ceedling/Unity/CMock, Mull, libFuzzer, GCC, Valgrind (PR 3/4).
Each addition updates this SBOM in the same PR.
