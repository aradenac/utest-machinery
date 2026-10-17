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

# Ownership: when the image runs as root against a host-mounted bench (the
# default on Linux/WSL), files it creates would be root-owned on the host. If a
# numeric owner was passed, take it so the bench stays writable by the user.
if [ "$(id -u)" = "0" ] && [ -n "${MTM_UID:-}" ]; then
  chown -R "${MTM_UID}:${MTM_GID:-${MTM_UID}}" "${BENCH}" 2>/dev/null || true
fi

if [ "$#" -eq 0 ]; then
  set -- dagu --help
fi
exec "$@"
