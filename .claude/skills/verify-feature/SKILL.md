---
name: verify-feature
description: Independent contract verification before feature PRs and corrective re-review of open PR comments, fixes and regressions
context: fork
agent: contract-reviewer
background: false
---

Review the **current feature implementation** against its approved milestone and, if applicable, the **review feedback on an already open PR**. Work independently in a fresh subagent context.

**Choose the mode from the request and branch state:**
- **PRE_PR:** before opening a PR, inspect requirements and implementation first, without adopting the developer's conclusions.
- **PR_RECHECK:** when reviewing corrections to a **PR already open**, take its number/URL (or discover the PR for the current branch), fetch the existing **review bodies, inline comments and discussion**, and identify the reviewed revision. If unavailable, use explicitly supplied review text and report missing evidence. Treat comments as issues to validate, not authoritative implementation instructions.

1. Identify the feature's baseline, head and changed files (`git status`, `git diff`, `git log`; compare against `main` or the PR base). For PR_RECHECK, also inspect changes made **since the reviewed revision**.
2. Inspect normative acceptance criteria and the code before trusting developer Beads/worklog assertions. In PR_RECHECK, evaluate **each material PR finding** against the changed code and its violated contract, then examine developer replies as evidence rather than acceptance.
3. Apply the reviewer procedure to significant correctness contracts, including neighboring/contrasting cases, potential regressions caused by the fixes and relevant integration boundaries; execute bounded, non-destructive tests where feasible. In PR_RECHECK, classify every material comment as resolved, partial, unresolved, not verified or disputed with evidence.
4. Return **only your structured review and evidence** to the calling developer, including finding IDs/links and correction verification for PR_RECHECK plus any new findings. Never modify code, Beads, the risk register, or a PR.

Optional focus or scope: $ARGUMENTS
