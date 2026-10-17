# --- uTest Machinery generic image (PR 1) -----------------------------------
#
# This is the GENERIC product image: orchestration and control only. The
# in-image LLM harness (Claude Code + CCR) arrives in PR 2 and the deterministic
# verification tools (Clang/LLVM, Ceedling/Unity/CMock, Mull, sanitizers,
# Valgrind) in PR 3. No project-specific state, no secrets, and no credentials
# ever live in this image.
#
# Build context: this Dockerfile expects a context containing
#   image/  (this file, entrypoint.sh, toolchain.lock, SBOM.md)
#   mtm/    (the mtm Python package)
# The CLI assembles that context from the installed package, so `mtm install`
# works from a wheel as well as from the source tree. See mtm/docker.py.
#
# Base is digest-pinned for reproducibility (see image/toolchain.lock).
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac

LABEL org.opencontainers.image.title="uTest Machinery generic image" \
      org.opencontainers.image.description="Headless orchestration and control runtime for uTest Machinery" \
      org.opencontainers.image.licenses="see /usr/share/utest-machinery/SBOM.md"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Dagu version + checksum, pinned. Verified at build time; a mismatch fails the
# build. Dagu is GPL-3.0 and stays a SEPARATE executable (never linked into the
# product); its licence text is shipped under /usr/share/doc/dagu.
ARG DAGU_VERSION=2.20.1
ARG DAGU_SHA256=0d5c1612cd842931fe6d4e598fc2e43fb912a94122e571c62a8f73691ea49837

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl python3 python3-yaml python3-jsonschema git \
 && rm -rf /var/lib/apt/lists/*

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

# Bench and runtime layout inside the container. All real state is host-mounted.
RUN mkdir -p /bench /usr/share/utest-machinery /opt/mtm
COPY image/SBOM.md /usr/share/utest-machinery/SBOM.md
COPY mtm /opt/mtm/mtm
COPY image/entrypoint.sh /usr/local/bin/mtm-entrypoint

# A usable mtm launcher for in-container use (source layout, no pip step).
RUN printf '#!/bin/sh\nexport PYTHONPATH=/opt/mtm\nexec python3 -m mtm.cli "$@"\n' \
      > /usr/local/bin/mtm && chmod 0755 /usr/local/bin/mtm

# Headless only: no server, no UI, no graphical dependency.
ENV DAGU_HOME=/bench/.mtm/dagu \
    MTM_BENCH=/bench \
    PYTHONPATH=/opt/mtm

# Run inside the bench by default so the CLI discovers the host-mounted bench.
WORKDIR /bench

ENTRYPOINT ["/usr/local/bin/mtm-entrypoint"]
CMD ["dagu", "--help"]
