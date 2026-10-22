# 0016 — §C interface-header share-safety: escalate the unbounded predicate class

**Status:** open — escalated to owner (PR 3 pushed as draft for external review)
**Supersedes:** nothing. **Amends:** [0015](0015-typedef-fidelity-mock-path.md) (the
F-A1…F-A6 sub-class of the typedef/interface-header work).

## Context

Milestone 3 PR 3 hardens the §A/§B/§C chain "declared C type → backing storage →
call argument → post-call observable → Unity assertion" ([plan §C](../../docs/plan/implementation.md)).
One sub-path is the **private CMock interface header**: when a mocked dependency's
parameter type is a `typedef`, that alias must be visible to both CMock's mock TU and
the unit's TU. The chosen strategy (decision 0015, strategy 3) is to **`#include` the
defining copied header**, which is safe only if that header may be included twice in
one TU (its own include guard collapses the repeat).

That safety question — *is this header safe to include twice?* — was answered by a
static text predicate (`_header_shares_safely` / `_simple_guard_span`, `mtm/gen.py`).

## The blockage

Five independent PRE_PR `contract-reviewer` passes each found a **mandatory** hole in
that predicate, all on the **same class**. Each was reproduced by the lead and
accepted; each fix was real and shrank the *observable*, not the class:

| ID | Shape | Mechanism |
| --- | --- | --- |
| F-A1 | anonymous `enum` alias, unguarded header | classifier knew only `struct`/`union` |
| F-A2 | `struct S {…}; typedef struct S S_t;` | a *narrowing* introduced by F-A1's fix |
| F-A4 | guard-like `#ifndef` **not enclosing** the definition | predicate = *(defines inline body)* **AND** *(has some guard)* — two weaker properties |
| F-A5 | `#else`/`#elif` repeats the body; dead `#define` under `#if 0` | reasoned over **text spans**, not preprocessor branches |
| F-A6 | `#undef` of the guard macro | never proved the macro *stays defined* |

**Root cause:** the predicate **approximates a C preprocessor with regular
expressions**. "Safe to include twice?" is a preprocessor-semantics question;
answering it by static text analysis is **unbounded** — every pass surfaces one more
directive the model omits. Patching each finding relocates the hole rather than
closing the class.

This is the skill's explicit escalation trigger: *"If two distinct corrective
strategies fail for the same defect class, STOP local patch/review cycling on that
class; restate the root invariant, evaluate a simpler architecture or explicit
unsupported-case limitation if permitted, and select a bounded new strategy or seek
owner arbitration for genuine scope/product decisions."* A binding note to escalate
was recorded before the fifth pass (sessions 9–10; decision 0015; Beads `utm-u8x`).

## What is *not* blocked

* PR 3's actual DoD is **done and evidenced** (offline `mtm verify`; five truthful
  profiles; §A/§B/§C compile+execute fidelity; R04; R22; AP-02 real CLI in image;
  AP-03 invariants). Full suite green (248 passed / 2 pre-existing skips); docker
  tier green.
* **Product behaviour respects §C today**: every shape that defeats the predicate
  yields a **named limitation** and a skipped scenario — **no uncompilable C with
  `limitations == []`**. The predicate is deliberately **conservative** (proves only
  a simple guard; refuses the rest). The residual risk is **completeness** (a header
  that is actually safe may be refused → lost mock coverage), **not** correctness
  (no fake PASS, no fabricated defect, no silently-broken emitted C).

## Options considered

1. **Bound the claim explicitly (current direction).** Document the share-safety
   contract as: *supported for a provably-simple guard (a prologue of comments/
   includes, `#ifndef X` / `#if !defined(X)` immediately followed by `#define X`, a
   matching `#endif` with no `#else`/`#elif` at the guard's level and no `#undef X`),
   enclosing every inline `struct`/`union`/`enum`; anything else is a named
   limitation.* §C explicitly permits this ("either generate a well-typed, compilable,
   semantically faithful test **or record a precise limitation/exclusion**"). Cheap,
   honest, ships the milestone; accepts conservative over-refusal on exotic headers.
2. **Structural redesign (rejected for now).** Drop include-sharing for inline-body
   aliases entirely: emit verbatim declarations for aliases that duplicate legally
   (scalar/pointer typedefs; `typedef struct Tag S;`) and **refuse** any alias whose
   *defining statement* carries an inline `{…}` body. Sound by construction with **no**
   guard analysis — but it **regresses the mock-path aggregate coverage** that F3
   introduced, which is exactly what PR 3 §C targets. Would need owner sign-off on the
   capability regression.
3. **A sixth predicate pass.** Rejected: unbounded (see root cause); the next exotic
   directive reopens the class.

## Decision

**Escalate to the owner.** Push PR 3 as a clearly-labelled **draft** for external
review, with the blockage, the root cause and options 1–2 stated in the PR body, this
record, the worklog (session 11) and Beads `utm-u8x`. The lead **will not** open a
sixth predicate pass without owner direction. The recommended resolution is **option 1**
(bound the claim + ship), which preserves milestone scope and §C compliance while
naming the limitation honestly.

## Consequences

* PR 3 can be reviewed externally now; the §C share-safety limit is disclosed rather
  than hidden behind green tests (per AP-01: a named `NOT_DEMONSTRATED`/limitation is
  acceptable, a fabricated READY is not).
* If the owner prefers option 2, the change is bounded (remove `_header_shares_safely`
  usage; refuse inline-body aliases by statement shape) but trades away mock-path
  aggregate coverage — a scope decision that belongs to the owner, not the agent.
* The `_header_shares_safely` predicate stays as-is (conservative, F-A6-fixed) in the
  draft; it is not claimed complete.

## References

* `mtm/gen.py` (`_header_shares_safely`, `_simple_guard_span`, `unsafe_headers` path),
  `tests/test_pr2_runner.py` (§C guard/typedef regression family).
* [Decision 0015](0015-typedef-fidelity-mock-path.md); worklog
  [`2026-10-18-pr3.md`](../log/2026-10-18-pr3.md) sessions 4–11.
* Plan §A/§B/§C and DoD 3 in `docs/plan/implementation.md`.
