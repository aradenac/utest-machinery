# Decision record 0004 — in-image LLM route (Claude Code + CCR → DeepSeek V4.1 Flash)

- **Status:** accepted (route derived read-only from the host config, 2026-10-17)
- **Date:** 2026-10-17
- **Supersedes:** none

## Context

The image must run its own Claude Code + CCR routed to DeepSeek V4.1 Flash, matching the
working **host** configuration (D5). The host CCR stores its config in `config.sqlite`
(SQLite), not the `config.json` the setup docs mention. Reading that file read-only and
deriving a sanitized template is cheaper and safer than hand-guessing, and it satisfies the
user's requirement (D6) to use the same configuration.

## Derived host route (no secret recorded)

Observed by reading `~/.claude-code-router/config.sqlite` read-only, values masked:

| Element | Value |
| --- | --- |
| Provider id / name | `scaleway` |
| Provider type | `openai_chat_completions` |
| Upstream base URL | taken from `SCW_BASE_URL` in the environment (value never recorded) |
| Upstream chat endpoint | `<SCW_BASE_URL>` + `/chat/completions` |
| Alternate detected endpoint | `<SCW_BASE_URL>` + `/responses` (`openai_responses`) |
| Upstream model id | `deepseek-v4.1-flash` |
| Provider credential | length 216, **equal to `SCW_BEARER_TOKEN` from the environment** (see below) |
| CCR gateway | enabled, bound to a loopback port inside the container |
| Claude profile id | `default-claude-code` (scope `ccr`, surface `cli`) |
| Profile model slots | `model`/`opusModel`/`sonnetModel`/`haikuModel`/`fableModel`/`smallFastModel` all = `scaleway/deepseek-v4.1-flash` |
| Profile env | `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`, `CLAUDE_CODE_MAX_OUTPUT_TOKENS=16384`, `CLAUDE_CODE_AUTO_COMPACT_WINDOW=240000`, `CLAUDE_CODE_MAX_CONTEXT_TOKENS=256000` |

**Security finding:** the provider `api_key` is stored **inline** in `config.sqlite` and is
byte-equal to the `SCW_BEARER_TOKEN` environment value. This is exactly why the host config
must **never** be copied into the image: it contains the live token. Only the route *shape*
above is transferred.

## Agent-profile layer (captured 2026-10-17, second pass)

The route is only half of the harness. The **agent profile** — a distinct layer CCR writes to
`~/.claude-code-router/profiles/<id>/claude/settings.json` — carries the context sizing and
model mapping, and the container must reproduce it too:

| Element | Value (nonsecret) |
| --- | --- |
| Profile dir | `~/.claude-code-router/profiles/default-claude-code/claude/settings.json` |
| API base (in-container router) | `ANTHROPIC_BASE_URL` = `ANTHROPIC_API_BASE_URL` = `CLAUDE_AGENT_API_BASE_URL` = the container's own loopback router (`http://<loopback>:<port>`)| 
| Model mapping | `ANTHROPIC_MODEL` + `ANTHROPIC_DEFAULT_{FABLE,OPUS,SONNET,HAIKU}_MODEL` all = `scaleway/deepseek-v4.1-flash`; also `CCR_CLAUDE_CODE_MODEL` / `CODEXL_CLAUDE_CODE_MODEL` |
| **Context sizing** | `CLAUDE_CODE_MAX_CONTEXT_TOKENS=256000`, `CLAUDE_CODE_AUTO_COMPACT_WINDOW=240000`, `CLAUDE_CODE_MAX_OUTPUT_TOKENS=16384` |
| Window enforcement | `CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1` |
| Model discovery | `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1` |
| Proxy bypass | `NO_PROXY`/`no_proxy` = `127.0.0.1,localhost,::1` |
| **Key mechanism** | `apiKeyHelper` = the CCR wrapper `bin/ccr-claude-code-api-key-<profile>` — i.e. the key is fetched by a helper, **not** embedded in Claude Code config. The container must use the same mechanism with the runtime secret. |
| Agent autonomy policy | `autoMode.allow` / `soft_deny` lists plus org-wide policy text — **this is dev-host agent policy, not product behavior; not copied into the image.** |

**Consequence:** the sanitized product template needs **both** layers — the CCR route
(provider + model) **and** the Claude Code profile env (base URL, model mapping, the three
context-size values, discovery flags) — wired to the runtime secret through an `apiKeyHelper`
equivalent. `doctor` should assert the sizing/model env is present in-container, and this is
what makes "same configuration as here" true rather than approximate.

## Decision

The image ships a **sanitized CCR route template** (nonsecret) with these properties, and the
token is injected only at runtime:

- Provider `scaleway`, type `openai_chat_completions`, base = `SCW_BASE_URL` (runtime).
- Model list: `deepseek-v4.1-flash` (the `glm-5.3` host entry is not part of the product route).
- All Claude profile model slots → `scaleway/deepseek-v4.1-flash`, with the four `env` vars
  above so in-container model discovery/limits match the host.
- **Both layers reproduced:** the CCR route config **and** the Claude Code profile `env`
  (base URLs, model mapping, `CLAUDE_CODE_MAX_CONTEXT_TOKENS=256000`,
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW=240000`, `CLAUDE_CODE_MAX_OUTPUT_TOKENS=16384`, discovery
  and window-enforcement flags), with the credential supplied through an `apiKeyHelper`
  equivalent reading the runtime secret.
- Gateway bound to a loopback port **inside the container**; the container runs its own CCR,
  so the host's router port is irrelevant (`docs/development/setup.md`).
- The **provider api_key is a runtime secret**, injected from
  `~/.config/utest-machinery/credentials.env` via a narrowly scoped `--env-file` or a runtime
  secret mount, expanded by the container entrypoint. Never in `ARG`/`ENV`/`COPY`/layers,
  never in Git, never in logs.
- Upstream reachability from inside Docker was **not** tested yet: the container still needs
  outbound HTTPS to the host named by `SCW_BASE_URL`, and this is a PR 1/PR 2 verification item
  (the live call is PR 2, per `docs/qualification/ci-and-manual-tests.md`).

## Alternatives considered

- **Copy the host `config.sqlite` into the image:** rejected — it embeds the live token and
  is a hostile persistence path; also couples the image to one user's machine.
- **Reconstruct the route by hand from the docs' `config.json` shape:** rejected — the host
  does not use that format; deriving from the real config is more faithful and auditable.

## Consequences

- The product needs a **secret-injection entrypoint** (PR 1 image / PR 2 live call) — becomes
  a concrete deliverable, not an assumption.
- `doctor` must verify: template present, credentials file present/`600`, and (PR 2) that the
  container can start CCR and reach the upstream and that the route resolves to
  `deepseek-v4.1-flash` — failing loudly as `LLM_AUTH` / `LLM_CONFIG` / `LLM_NETWORK`
  otherwise, never silently switching model.

## References

- `docs/development/setup.md` (in-image router, credential handling),
  `docs/architecture/architecture.md` (SYS-036), `docs/architecture/workflow-failures.md`
  (`LLM_AUTH`/`LLM_CONFIG`/`LLM_NETWORK`), [`0003-third-party-licenses-and-dagu.md`](0003-third-party-licenses-and-dagu.md).
