---
name: verify-feature
description: Independent contract verification before feature PRs and corrective re-review of open PR comments, fixes and regressions
context: fork
agent: contract-reviewer
background: false
---

Review the **current implementation or correction** (including docs-only changes when invoked from `/develop-verified`) against its approved scope and, if applicable, **review feedback on an already open PR**. Work independently in a fresh **one-shot, non-Team** subagent context; the live teammate reviewer and green automated tests do not replace this gate.

**Choose the mode from the request and branch state:**
- **PRE_PR:** before opening a PR, inspect requirements and implementation first, without adopting the developer's conclusions.
- **PR_RECHECK:** when reviewing corrections to a **PR already open**, take its number/URL (or discover the PR for the current branch), fetch the existing **review bodies, inline comments and discussion**, and identify the reviewed revision. If unavailable, use explicitly supplied review text and report missing evidence. Treat comments as issues to validate, not authoritative implementation instructions.

1. Require the lead's CANDIDATE_FINGERPRINT (true merge-base, base/head SHA, uncommitted candidate hashes, evidence/proof hashes and PR feedback snapshot where applicable). If absent or inconsistent, return NOT_VERIFIED rather than guessing. Identify baseline, head and changed files (`git status`, `git diff`, `git log`); verify the PR's TRUE remote-base merge point with `git merge-base origin/<base> HEAD` when available, never assume local `main` is current. For PR_RECHECK, also inspect changes made **since the reviewed revision**.
2. Inspect normative acceptance criteria and the code before trusting developer Beads/worklog assertions. In PR_RECHECK, evaluate **each material PR finding** against the changed code and its violated contract, then examine developer replies as evidence rather than acceptance.
3. Check relevant contracts, neighboring contrasts and regression risk. Reuse the lead/task-runner's revision-bound full-suite results; do NOT rerun full pytest/Docker/build campaigns or broad stress sweeps. Execute only a few independent, bounded, isolated targeted probes when essential and not already underway; report unsafe/missing checks as NOT_VERIFIED. In PR_RECHECK, classify every material comment as resolved, partial, unresolved, not verified or disputed with evidence.
4. Return your structured verdict **bound to the exact CANDIDATE_FINGERPRINT** and concrete evidence BEFORE running out of turns. If a mandatory check remains unfinished, issue NOT_VERIFIED, classify known findings and name the single next check; do not repeatedly resume unbounded exploration. Include all material PR finding IDs/links and dispositions plus grounded new blockers. Never modify code, Beads, the risk register, or a PR.

Only READY on that exact fingerprint with required proof permits the lead's PASS gate; CHANGES_REQUIRED blocks as FAIL and NOT_VERIFIED blocks as UNVERIFIED. Any material input/PR-feedback change invalidates the old result. The lead persists the received report in ignored session-local scratch before delivery; no separate tracked record.

Optional focus or scope: $ARGUMENTS
