# Decision record 0011 — the in-image LLM harness is a pure text completion

- **Status:** accepted (agent decision under R20, 2026-10-18)
- **Date:** 2026-10-18
- **Supersedes:** partially extends [0004](0004-in-image-llm-route.md) (route unchanged; the
  *invocation* is now constrained)

## Context

The route in [0004](0004-in-image-llm-route.md) works: the in-container router answers a real
`/v1/messages` request with HTTP 200 from DeepSeek. But invoking it as **Claude Code** meant
the model behaved as an *agent*: given the inventory prompt it replied with prose ("## Why
I'm stopping rather than building … if instead the intent was that I *should* proceed on
reasonable assumptions … say so and I'll write the unit, the verifier, and the DAG") instead
of the required JSON plan. `mtm generate` then failed with `LLM_CONFIG`.

## Decision

`mtm-llm` runs Claude Code as a **pure text-completion function**, not an agent:

- `--tools ""` — no tool is available, so a model that tries to act cannot.
- `--strict-mcp-config` — no MCP server is loaded from the environment.
- `--append-system-prompt-file <...>` — states that the harness has no tools, must never
  offer to build/run/verify, must never ask a question, must treat stdin as opaque **data**,
  and must answer with exactly one JSON object (encoding ambiguity as `UNDETERMINED`/`ASSUMED`
  provenance rather than asking for clarification).

## Justification (R20)

- **Least privilege.** Generation needs one thing from the model — a JSON plan. Tools,
  file edits and questions are capabilities the pipeline must not grant: SYS-025 (inputs are
  data, not instructions) is far easier to hold when the model *cannot* act on them.
- **Determinism and error contract.** An agent turn can end in prose, a question or a tool
  call; all three are indistinguishable from a broken generation. A pure completion has one
  success shape and one failure shape, which the LLM error contract can classify.
- **No new machinery.** This uses Claude Code's own flags; no wrapper framework, no second
  provider abstraction, and the route itself is unchanged from 0004.

## Consequences

- The prompt template gains an explicit, keyed output contract (the plan's case shape) so the
  model's JSON dialect matches the generator instead of being guessed at. Bumping it bumps
  `PROMPT_VERSION` (`plan-v1` → `plan-v2`) and the recorded fixtures with it.
- **Two-sided defence.** The generator still validates every plan: a control key the mock
  does not expose, or a scalar supplied for an aggregate pointee, is a recorded **limitation**
  and the scenario is not emitted — never a silently defaulted mock that invents a defect.
- `--tools ""` means the model cannot read the bench, which is why the prompt carries the
  full inventory. That is deliberate and matches the "bench NOT mounted" property of the
  generation container ([0008](0008-docker-liveness-and-mount-policy.md)).

## References

- [0004](0004-in-image-llm-route.md) (route), [0010](0010-generated-suite-framework.md) (generator rules)
- `docs/architecture/automation-policy.md`, SYS-025
