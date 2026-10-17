# Claude Code — uTest Machinery

Claude Code loads this file automatically. Start by reading [README](README.md), the [architecture](docs/architecture/architecture.md), [implementation plan](docs/plan/implementation.md), [LLM policy](docs/architecture/automation-policy.md), and [qualification plan](docs/qualification/ci-and-manual-tests.md). Do not reread everything on every tool call; inspect the relevant parts.

## Start of every development session

1. Inspect `git status`, current branch, merged PRs, code and existing checks. Reconcile the documented feature milestones with **what has actually merged**. Never infer completion from a checklist alone.
2. Check Beads (`bd prime`, `bd ready`, `bd list`). Initialize only if absent (`bd init --skip-agents`; pin/check version); preserve the repository's instructions. **Reconcile** backlog, dependencies and comments with the plan, PRs and evidence. `docs/plan/implementation.md` is the scope authority; Beads tracks execution, not a second product specification.
3. Select **only the next approved feature milestone**. Complete its implementation, tests and local verification; update Beads status; open **one PR** with evidence. **STOP after opening the PR. Do not merge, start another milestone or continue automatically.** Wait for review and an explicit instruction before proceeding.
4. On a subsequent session, inspect PR review comments, fix findings in the same PR, rerun local checks and update Beads; stop again. Only after an externally merged PR and a new user instruction may you start the next milestone.

## Working rules

- Read the [risk register](docs/project/risks.md) as context, **but never edit it** (including risk scores, statuses or mitigation actions). Risk reassessment belongs to an independent review.
- **Complexity watch (R20):** make routine implementation decisions autonomously. Before significant new glue, wrappers, custom engines or duplicate data models, check existing tool capabilities, simpler integrations and whether the requirement can be fulfilled with less complexity. For a nontrivial choice, add a **brief justification in the relevant Beads item/comment**, covering the chosen option, meaningful alternatives and maintenance trade-off; do not produce a separate architecture report or duplicate this rationale in the PR. Keep Beads notes synchronized/versioned so reviewers can inspect them; the PR only references the corresponding Beads IDs.
- If the simplest workable design is still disproportionate to its feature value, **flag the concern and recommended alternatives to the user before committing to a scope or major architecture change**. Do not silently drop requirements or assume permission to change scope.

- Work inside the repository and local virtual environments/container. Ask before installing anything outside the project or modifying host system settings.
- Input C, headers, SDDs and other selected artifacts are immutable. Product-specific files are in one external test-bench directory; do not modify application source or silently adjust an SDD-derived oracle.
- Run **all relevant tests locally first**; CI is the short independent reproducibility gate, **not your debugging environment**. Record local heavy/full campaign results in the PR.
- **Acceptance evidence (AP-01):** before opening or updating a feature PR, check **every applicable acceptance criterion** of the current milestone against a test/command actually executed and its observed result. Record a concise **criterion → evidence** reference (command/result/log or artifact), or explicitly mark it **NOT_DEMONSTRATED** with the reason, in the relevant **Beads feature item/comment**; the PR links Beads and reports real test results instead of duplicating a matrix. A mandatory unproven criterion is a **feature-gate blocker** even if CI is green; report it rather than declaring the feature ready.
- **After PR review findings (AP-01):** identify the **general contract/invariant violated** by each finding, fix the observed case and test the **relevant neighboring or contrasting cases** that could exhibit the same defect. Record the additional evidence or remaining limits in Beads before re-requesting review. Keep these checks proportional to the current milestone; do not create unrelated tests, tools or a second tracking system.
- Each PR implements a complete user-visible capability with its fixtures, necessary failure cases, updated docs/diagram and exact test commands/results. Do not split a feature into artificial PRs.
- Never fabricate a test result, tool metric, mutation outcome, target-MCU guarantee or implementation status. Distinguish a detected product-code defect from a broken test harness.
- Honor the [error/stop contract](docs/architecture/workflow-failures.md). A failed selected unit does not imply aborting independent checks; a broken workflow/infrastructure cannot report success.
- Product's **initial LLM configuration is Claude Code + Claude Code Router (CCR) + DeepSeek V4.1 Flash**. Do not replace it with a generic multi-provider framework. See [developer setup](docs/development/setup.md); credentials always outside repo/image.
- Only free deterministic test tools. Dagu is the preferred text-defined/headless runner pending a real gate test. QEMU/Renode are later, separate scope.
- No GitHub issues required for planning. Use **Beads (`bd`)** for the developer backlog: one feature milestone per major work item, optional implementation subtasks, comments and dependencies. Before PR: export the current Beads issue view to `.beads/issues.jsonl` for Git review, and sync the Dolt state according to the pinned version's docs. **JSONL is not a full Dolt backup**; never claim otherwise. Exclude local DB/cache/secrets from ordinary source commits.
- Use the [PR template](.github/PULL_REQUEST_TEMPLATE.md) with exact local checks, fast CI results, Beads IDs and remaining risks.

## Completion rule

The milestone is **ready for review** only when its acceptance methods have been run locally, limitations are documented, the PR contains reproducible evidence, and Beads reflects the actual PR state. Its status remains `in_progress`/under review until the PR is merged. Then **stop**; do not start the next milestone without authorization.
