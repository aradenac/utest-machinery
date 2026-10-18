# --- uTest Machinery generic image ------------------------------------------
#
# One image serves both milestones delivered so far:
#   * PR 1 — headless orchestration (Dagu) and control;
#   * PR 2 — LLM-assisted generation, running Claude Code through CCR to
#     DeepSeek V4.1 Flash *inside* the container;
#   * PR 3 — the deterministic offline verification profiles (`mtm verify`):
#     Clang/LLVM source-based coverage (incl. MC/DC), ASan/UBSan and Valgrind
#     Memcheck, alongside the GCC build Ceedling already uses.
#
# The advanced dynamic toolchain (Mull, libFuzzer) arrives in PR 4. No
# project-specific state and no credentials ever live in this image: the token is
# injected at runtime only.
#
# Build context (assembled by the CLI from the installed package):
#   image/  (this file, entrypoint.sh, mtm-llm.sh, ccr-config.template.json,
#            toolchain.lock, SBOM.md)
#   mtm/    (the mtm Python package)
FROM node:24.21.0-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20

LABEL org.opencontainers.image.title="uTest Machinery generic image" \
      org.opencontainers.image.description="Headless orchestration, LLM generation harness and (later) deterministic verification runtime" \
      org.opencontainers.image.licenses="see /usr/share/utest-machinery/SBOM.md"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Pinned tool versions. A checksum mismatch or a version drift fails the build.
ARG DAGU_VERSION=2.20.1
ARG DAGU_SHA256=0d5c1612cd842931fe6d4e598fc2e43fb912a94122e571c62a8f73691ea49837
ARG CLAUDE_CODE_VERSION=2.1.302
ARG CCR_VERSION=3.1.3
ARG CEEDLING_VERSION=1.1.10
# LLVM/Clang release the coverage and sanitizer profiles pin. The `-18` package
# suffix keeps these coexistable with an unversioned distro toolchain and is what
# `mtm/profiles.py` names as each profile's required tool.
ARG LLVM_VERSION=18

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg gpgv python3 python3-yaml python3-jsonschema git \
      ruby ruby-dev gcc libc6-dev make \
 && rm -rf /var/lib/apt/lists/*

# Clang + LLVM (coverage/MC-DC and ASan/UBSan profiles), the libclang C API and
# the matching Python bindings — the semantic authority for C declarations/types
# (owner decision 0017) — and Valgrind (Memcheck profile). apt.llvm.org is the
# upstream project's own repository; the key is fetched and dearmored at build
# time so no key material is committed here. `libclang-18-dev` ships the `clang-c`
# headers; `python3-clang-18` ships `/usr/lib/python3/dist-packages/clang/`.
RUN set -eux; \
    curl -fsSL https://apt.llvm.org/llvm-snapshot.gpg.key | gpg --dearmor -o /usr/share/keyrings/llvm.gpg; \
    echo "deb [signed-by=/usr/share/keyrings/llvm.gpg] http://apt.llvm.org/bookworm/ llvm-toolchain-bookworm-${LLVM_VERSION} main" \
      > /etc/apt/sources.list.d/llvm.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      "clang-${LLVM_VERSION}" "llvm-${LLVM_VERSION}" "llvm-${LLVM_VERSION}-tools" \
      "libclang-rt-${LLVM_VERSION}-dev" "libclang-common-${LLVM_VERSION}-dev" \
      "libclang-${LLVM_VERSION}-dev" "python3-clang-${LLVM_VERSION}" valgrind; \
    rm -rf /var/lib/apt/lists/*; \
    "clang-${LLVM_VERSION}" --version | head -1; \
    "llvm-profdata-${LLVM_VERSION}" --version | head -1; \
    valgrind --version; \
    python3 -c "import ctypes, clang.cindex as ci; \
lib = ctypes.CDLL(ci.conf.lib._name); \
lib.clang_getClangVersion.restype = ctypes.c_void_p; \
lib.clang_getCString.restype = ctypes.c_char_p; \
print('python3-clang binding:', ci.__file__); \
print('libclang:', lib.clang_getCString(lib.clang_getClangVersion()).decode())"

# Ceedling + CMock drive the generated functional unit suite. Ruby and the C
# toolchain live in the image so a user needs neither on the host. Ceedling
# vendors its pinned Unity/CMock; the gem is the only extra dependency.
RUN set -eux; \
    gem install --no-document --version "${CEEDLING_VERSION}" ceedling; \
    mkdir -p /usr/share/doc/ceedling; \
    printf 'Ceedling is MIT-licensed. See https://github.com/ThrowTheSwitch/Ceedling\n' \
      > /usr/share/doc/ceedling/LICENSE; \
    ceedling version | head -3

# Fetch and verify Dagu, keep the licence, drop the tarball.
RUN set -eux; \
    curl -fsSL -o /tmp/dagu.tgz \
      "https://github.com/dagu-org/dagu/releases/download/v${DAGU_VERSION}/dagu_${DAGU_VERSION}_linux_amd64.tar.gz"; \
    echo "${DAGU_SHA256}  /tmp/dagu.tgz" | sha256sum -c -; \
    mkdir -p /opt/dagu; \
    tar -xzf /tmp/dagu.tgz -C /opt/dagu; \
    install -m 0755 /opt/dagu/dagu /usr/local/bin/dagu; \
    mkdir -p /usr/share/doc/dagu; \
    cp /opt/dagu/LICENSE /usr/share/doc/dagu/LICENSE; \
    rm -rf /tmp/dagu.tgz /opt/dagu

# The in-image LLM harness: Claude Code driven by Claude Code Router (CCR).
# Both are installed from the npm registry at pinned versions. CCR is a separate
# process (like Dagu) and never linked into the product code.
RUN set -eux; \
    npm install -g --no-fund --no-audit \
      "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
      "@musistudio/claude-code-router@${CCR_VERSION}"; \
    npm cache clean --force; \
    mkdir -p /usr/share/doc/claude-code /usr/share/doc/claude-code-router; \
    cp "$(npm root -g)/@anthropic-ai/claude-code/LICENSE" \
       /usr/share/doc/claude-code/LICENSE 2>/dev/null || true; \
    cp "$(npm root -g)/@musistudio/claude-code-router/LICENSE" \
       /usr/share/doc/claude-code-router/LICENSE 2>/dev/null || true

# Bench and runtime layout inside the container. All real state is host-mounted.
RUN mkdir -p /bench /usr/share/utest-machinery /opt/mtm
COPY image/SBOM.md /usr/share/utest-machinery/SBOM.md
COPY image/ccr-config.template.json /usr/share/utest-machinery/ccr-config.template.json
COPY mtm /opt/mtm/mtm
COPY image/entrypoint.sh /usr/local/bin/mtm-entrypoint
COPY image/mtm-llm.sh /usr/local/bin/mtm-llm

# A usable mtm launcher for in-container use (source layout, no pip step).
RUN printf '#!/bin/sh\nexport PYTHONPATH=/opt/mtm\nexec python3 -m mtm.cli "$@"\n' \
      > /usr/local/bin/mtm \
 && chmod 0755 /usr/local/bin/mtm /usr/local/bin/mtm-llm /usr/local/bin/mtm-entrypoint

# The in-container loopback router key, exposed through Claude Code's own
# apiKeyHelper mechanism (the mechanism the validated host route uses). It is a
# fixed local value that authenticates the in-container client to the
# in-container router: NOT a secret, and it never equals an upstream credential.
# Baked into the image so the harness needs no write access to /usr/local/bin,
# which lets 'mtm generate' run the container as the invoking (non-root) user.
RUN printf '#!/bin/sh\nprintf "%%s\\n" "%s"\n' mtm-local > /usr/local/bin/mtm-api-key \
 && chmod 0755 /usr/local/bin/mtm-api-key

# Headless only: no server, no UI, no graphical dependency.
ENV DAGU_HOME=/bench/.mtm/dagu \
    MTM_BENCH=/bench \
    PYTHONPATH=/opt/mtm \
    MTM_LLM_CONFIG_TEMPLATE=/usr/share/utest-machinery/ccr-config.template.json

# Run inside the bench by default so the CLI discovers the host-mounted bench.
WORKDIR /bench

ENTRYPOINT ["/usr/local/bin/mtm-entrypoint"]
CMD ["dagu", "--help"]
