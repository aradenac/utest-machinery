#!/bin/sh
# uTest Machinery container entrypoint (PR 1).
#
# Responsibilities kept minimal and explicit:
#   * ensure the host-mounted bench directory exists;
#   * point Dagu at host-mounted state so nothing irreplaceable lives in the
#     container;
#   * hand off to the requested command (usually ``dagu ...``).
#
# It deliberately does NOT read, echo or require secrets. Credentials for the
# LLM phase are injected only for the LLM operation (PR 2), via a narrowly
# scoped --env-file or a runtime secret mount, never baked into the image.
set -eu

BENCH="${MTM_BENCH:-/bench}"
mkdir -p "${BENCH}"

export DAGU_HOME="${DAGU_HOME:-${BENCH}/.mtm/dagu}"
mkdir -p "${DAGU_HOME}/dags" "${DAGU_HOME}/logs" "${DAGU_HOME}/data"

# Headless: no auth setup wizard, no server. Dagu otherwise warns on every call.
if [ ! -f "${DAGU_HOME}/config.yaml" ]; then
  printf 'auth:\n  mode: none\n' > "${DAGU_HOME}/config.yaml"
fi

# Ownership: the caller is responsible for identity, not this script. The CLI
# runs the container as the invoking user (docker run --user UID:GID) so files
# created in the host-mounted bench stay user-owned. This entrypoint never runs
# 'chown -R' on the bench: the bench may contain the selected source tree, and
# rewriting its ownership would modify unrelated host files.

if [ "$#" -eq 0 ]; then
  set -- dagu --help
fi
exec "$@"
