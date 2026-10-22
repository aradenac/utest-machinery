#!/bin/sh
# Ephemeral in-image direct Scaleway completion. No agent or router process.
set -eu
exec python3 -m mtm.direct_llm
