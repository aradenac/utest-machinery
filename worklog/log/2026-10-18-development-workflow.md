# 2026-10-18 — Workflow RETEX: coherent slices, parallel critiques, native hooks

**Decision:** Keep one user-invoked `/develop-verified` skill and an autonomous lead. The product milestone sets acceptance, not implementation-slice boundaries. The lead may dynamically create/split/merge coherent technical slices, use native Tasks optionally, and overlap isolated snapshot reviews with disjoint work. No mandatory review after each slice, no Dynamic Workflow, `/goal`, `/loop`, standalone dev-gate state machine, periodic watchdog, new Beads tracker or auto-merge.

**Implementation:** Update `.claude/skills/develop-verified/SKILL.md`, `verify-feature`, reviewer agents, `CLAUDE.md` and developer setup. Register session-scoped `command` hooks for `SubagentStop`, `TaskCompleted` and `Stop`, each mechanically narrow and fail-open for malformed input. Keep lead arbitration of findings and full-test ownership. Include stdlib-only hook smoke tests.

**Safety/limits:** SubagentStop allows a single short clarification if the final reviewer summary is empty; TaskCompleted only validates an explicitly declared `Proof-file`; Stop only detects an explicit final READY with active background jobs. No semantic LLM replacements. Agent Teams/CCR-specific delivery must still be observed in practice; no measured speedup claimed.

**Evidence:** Source and hook schema inspected against Claude Code reference as of 2026-10-18. Smoke-test command: `python3 -m unittest discover -s tests -p test_dev_workflow_hooks.py`. Documentation/process change is merged without CI at owner request. Monitor R25/IMP-16/AP-07 in next real development session.

## Quick wins de convergence — suite de la PR #33

Le lead vérifie désormais les consommateurs directs et quelques catégories contrastées d'un invariant partagé avant de multiplier les patches. Un critique peut remonter immédiatement un finding obligatoire reproduit (message Team unique, ou retour final précoce d'un subagent isolé), puis le lead poursuit les autres lots réellement indépendants. Les campagnes complètes utilisent des entrées effectives stables et conservent le code retour réel ; les reviewers finaux rendent un verdict utile dès qu'il est étayé, sans exploration non bornée. Le hook Stop ignore les tâches déjà terminées. Aucune nouvelle orchestration ni exigence produit. Efficacité à mesurer en session réelle.
