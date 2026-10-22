# 0019 — Direct Scaleway inference in the product image (2026-10-22)

**Status:** owner-approved; implemented on draft PR #35, acceptance evidence pending.

## Decision

Replace the product's in-image Claude Code + CCR pure-completion route
(decisions 0004 and 0011) with **one direct HTTPS Chat Completions call** to
Scaleway. Those earlier records remain historical but their product inference
stack is superseded. This decision does **not** change the developer's host
coding-agent workflow.

## Rationale

The existing `mtm generate` model boundary consumes a deterministic inventory
string and produces a JSON plan via `LLMProvider.complete(prompt)`. Its model
does not have access to tools or a workspace. A general-purpose coding agent
and a persistent router therefore add indirection without providing an agentic
capability to this step. The new transport removes those dependencies, allows
explicit inference budgets and collects upstream-reported usage for later
cost/quality comparison. No token-saving percentage is asserted before a
controlled before/after measurement.

## Boundaries and safeguards

- Run the model call in a transient product container only; do not mount the
  bench or ship secrets in image layers, arguments, or logs.
- Use Chat Completions, not the less-compatible Responses endpoint, keeping
  the previous selected Scaleway model syntax and plan-v2 prompt unchanged.
- Request JSON-object output with a bounded completion budget. A truncated,
  empty or malformed reply cannot be treated as a generated plan.
- Keep model/usage provenance, no hidden fallback to recorded answers, and
  no automatic retry or second inference when an error occurs. Classify
  authentication, transient network/upstream and configuration faults.
- Retain the current semantic plan grounding, C generation, deterministic
  Ceedling execution, snapshot identities and token-free `mtm verify`.
- Pin the existing base image for this PR. The inherited Node runtime is
  unused; slimming the base is an independent, separately validated decision.

## Acceptance proof

1. Offline transport tests cover one request, model routing, configurable
   budgets, authentication/upstream/truncation errors and non-disclosure.
2. Existing full Python baseline CI and Docker image/generation smoke stay green.
3. An owner-run in-container live `--provider scaleway` generation on
   `mocked-sensor` records the real token usage (when supplied), correct
   source/SDD behavior and Ceedling execution; no credential leakage.
4. Re-run Milestone 3's Docker acceptance tier and the independent PR_RECHECK
   before approval. No milestone completion or merge is implied by this code
   change alone.
