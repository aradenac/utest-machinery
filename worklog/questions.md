# Open questions and arbitrations

Questions waiting on a user decision. Answered questions are kept here with their
resolution and date, and mirrored into the relevant dated log entry.

## D1 — secret rotation — RESOLVED (user decision 2026-10-17)

A reconnaissance command printed the full `SCW_BEARER_TOKEN` into the session transcript.
Targeted check found no persistence in `~/.claude/history.jsonl` or the CCR app-data stores.
**User will rotate the token.** Blows nothing in PR 1. Process fix adopted: presence checks
use `[ -n "$VAR" ]` only, never the value.

## D2 — Dagu GPL-3.0 in the shipped image — RESOLVED (user decision 2026-10-17)

**Decision:** keep Dagu **non-contaminating** as a separate executable, and record all
third-party components (versions + licenses) in a **versioned repository documentation file
acting as an SBOM**. See [`decisions/0003-third-party-licenses-and-dagu.md`](decisions/0003-third-party-licenses-and-dagu.md).

## D3 — authorization to run external binaries — RESOLVED (user decision 2026-10-17)

**Decision:** the user relaunched with broader permissions. **Done:** downloaded
`dagu-org/dagu` `v2.20.1` (sha256 `0d5c1612…49837`, verified against upstream) and ran the
gate proof. All seven criteria pass; one limitation found and delegated to the wrapper. See
[`decisions/0005-dagu-gate-proof.md`](decisions/0005-dagu-gate-proof.md). The Dagu route is
now **measured**, not docs-only.

## D4 — Python version range — RESOLVED (decision 2026-10-17)

**Decision:** floor **Python 3.11**, deps limited to PyYAML + jsonschema, CI matrix 3.11 +
newest; the in-image Python is pinned independently. See
[`decisions/0002-python-version-policy.md`](decisions/0002-python-version-policy.md).

## D5 — credentials file and in-image LLM harness — RESOLVED (user decision 2026-10-17)

**User decisions:**
1. `doctor` **shall require** the credentials.
2. Use the user's Scaleway (SCW) credentials, sourced from the shell environment
   (`~/.bashrc`), not invented.
3. Install **Claude Code + CCR inside Docker**, matching the working host configuration.

**Actions taken 2026-10-17:** created `~/.config/utest-machinery/credentials.env`
(mode `600`) from the environment variables; values were never printed. Keys: `SCW_BASE_URL`,
`SCW_BEARER_TOKEN`. In-image Claude Code + CCR and a live route check are PR 2 work (needs D6).

## D6 — in-image CCR configuration — RESOLVED (option B, 2026-10-17)

The route was derived read-only from `~/.claude-code-router/config.sqlite` (values masked):
provider `scaleway` / `openai_chat_completions`, base `https://llm.tools.lacroix-dev.com`,
model `deepseek-v4.1-flash`, gateway `127.0.0.1:3456`, profile `default-claude-code`. The
provider api_key is stored **inline and equals `SCW_BEARER_TOKEN`** — hence the host config is
never copied; only the nonsecret route shape is used, with the token injected at runtime. See
[`decisions/0004-in-image-llm-route.md`](decisions/0004-in-image-llm-route.md).

**Open sub-item:** no decision yet on whether the project will **publish** images (registry /
releases). Does not block PR 1; recorded so it is settled before any publication.
