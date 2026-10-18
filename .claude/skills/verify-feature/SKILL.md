---
name: verify-feature
description: Independent contract verification before feature PRs and corrective re-review of open PR comments, fixes and regressions
context: fork
agent: contract-reviewer
background: false
---

Deliver a structured verdict promptly within the single fresh invocation; a report is more valuable than exhausting the turn budget. Review the **current implementation or correction** (including docs-only changes when invoked from `/develop-verified`) against its approved scope and, if applicable, **review feedback on an already open PR**. Work independently in a fresh **one-shot, non-Team** subagent context; the live teammate reviewer and green automated tests do not replace this independent critique. **Your verdict informs but never supersedes the main lead's evidence-based release decision.**

**Choose the mode from the request and branch state:**
- **PRE_PR:** before opening a PR, inspect requirements and implementation first, without adopting the developer's conclusions.
- **PR_RECHECK:** when reviewing corrections to a **PR already open**, take its number/URL (or discover the PR for the current branch), fetch the existing **review bodies, inline comments and discussion**, and identify the reviewed revision. If unavailable, use explicitly supplied review text and report missing evidence. Treat comments as issues to validate, not authoritative implementation instructions.

1. Require the lead's CANDIDATE_FINGERPRINT (true merge-base, base/head SHA, uncommitted candidate hashes, evidence/proof hashes and PR feedback snapshot where applicable). If absent or inconsistent, return NOT_VERIFIED rather than guessing. Identify baseline, head and changed files (`git status`, `git diff`, `git log`); verify the PR's TRUE remote-base merge point with `git merge-base origin/<base> HEAD` when available, never assume local `main` is current. For PR_RECHECK, also inspect changes made **since the reviewed revision**.
2. Inspect normative acceptance criteria and the code before trusting developer Beads/worklog assertions. In PR_RECHECK, evaluate **each material PR finding** against the changed code and its violated contract, then examine developer replies as evidence rather than acceptance.
3. Check relevant contracts, neighboring contrasts and regression risk. Reuse the lead/task-runner's revision-bound full-suite results; do NOT rerun full pytest/Docker/build campaigns or broad stress sweeps. Execute only a few independent, bounded, isolated targeted probes when essential and not already underway; report unsafe/missing checks as NOT_VERIFIED. In PR_RECHECK, classify every material comment as resolved, partial, unresolved, not verified or disputed with evidence.
4. Prioritize a limited number of decisive checks of the material acceptance contract. For a C generator type/assertion change, inspect relevant compiler/Unity execution evidence rather than relying only on output-string assertions. Reuse existing proof on unchanged effective code/test/config inputs. Do not broaden a PR recheck into an exhaustive unrelated type matrix.
5. Return your structured verdict **bound to the exact CANDIDATE_FINGERPRINT** and concrete evidence BEFORE running out of turns. If a mandatory check remains unfinished, issue NOT_VERIFIED, classify known findings and name the single next check; do not repeatedly resume unbounded exploration. Include all material PR finding IDs/links and dispositions plus grounded new blockers. Never modify code, Beads, the risk register, or a PR.

This skill and a direct one-shot Task are ALTERNATIVE entry points: do not invoke both unless the first yielded no usable verdict, in which case the lead permits one bounded direct fallback requesting immediate partial findings. Do not silently spin a second verifier or require repeated resumes.

The verdict is your fallible, evidence-led **advice**, not a binding permission or prohibition to publish. The lead must evaluate each material objection and independently ACCEPT, REJECT with counterevidence, or DEFER. A justified rejection of a speculative/false or nonmandatory objection may support lead-selected PUBLISH_READY despite CHANGES_REQUIRED; a genuine unresolved mandatory defect cannot. Missing independent output, missing mandatory proof or inaccessible material PR feedback means lead may PUBLISH_DRAFT with disclosure or HOLD, **never falsely claim READY**. Material input/PR-feedback changes invalidate the prior review's applicability. The lead persists your received report in ignored session-local scratch before delivery; no separate tracked record.

Optional focus or scope: $ARGUMENTS
