---
name: verify-feature
description: Independent evidence-led contract verification before a feature PR or after substantial review corrections
context: fork
agent: contract-reviewer
background: false
---

Review the **current feature implementation** against the approved milestone on `main`. Work independently in a fresh subagent context; do not use the developer's conversational conclusions as your starting point.

1. Identify the feature's baseline and current changed files (`git status`, `git diff`, `git log`; compare against `main` or the actual PR base).
2. Inspect normative acceptance criteria and the code before reading the developer's Beads/worklog assertions.
3. Apply the reviewer procedure to significant correctness contracts, including neighboring/contrasting cases and real user-facing integration when relevant; execute bounded, non-destructive tests where feasible.
4. Return **only your structured review and evidence** to the calling developer. Never modify code, Beads, the risk register, or a PR.

Optional focus or scope: $ARGUMENTS
