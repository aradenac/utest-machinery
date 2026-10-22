# 0015 — Typedef fidelity reaches the mock path and the private interface header

**Status:** accepted (PR 3, milestone 3)

## Context

The pointer-`typedef` fix (`dd122e7`, F3) resolved a `typedef int *P` alias on the
**SUT parameter** path only. The early `test-challenger` (fresh isolated review on
the frozen `dd122e7`) independently reproduced **three CONFIRMED MANDATORY**
defects in the same §A/§C class; the lead reproduced each and accepted all three:

1. **Array/function-pointer alias never recorded.** `cscan.scan_typedefs` extracted
   the alias name with `([A-Za-z_]\w*)\s*$`, which returns `None` for
   `typedef int Arr[4]` (declarator ends `]`) and `typedef void (*CB)(int)` (ends
   `)`). The `opaque` marker was therefore never set and
   `_unsupported_param_shape`'s opaque refusal was **dead code**. A bench with
   `int first(Arr a)` produced `limitations == []` and emitted `first((Arr)5);`,
   which does not compile (`error: cast specifies array type`) — an opaque
   `TOOL_FAILURE` instead of a named limitation.
2. **Mock path did not resolve the alias.** `split_declaration`/`mock_controls`
   were called without a typedef map (by design, "the dependency's aliases are left
   unresolved"), so `typedef int *P; void dep_write(P *pp);` (a real `int **`) was
   reported at depth 1 and emitted an uncompilable `P v = (P)5;` plus a
   `ReturnThruPtr` writing into the mock's pointer object — an unjustified pointer
   cast, the class §C forbids.
3. **Interface header lacked the alias definition.** The private CMock header was
   emitted as `#include <stdint.h>` + the raw declaration. CMock compiles the
   header standalone and the test includes it before the unit header, so
   `void dep_write(P p);` compiled with `unknown type name 'P'` (also for a plain
   alias such as `vendor_adc_sample_t`) and *every* scenario mocking the symbol
   died as an opaque build failure.

**F3 correction — two strategies failed, third accepted.** The first fix emitted
the alias **verbatim** into the interface header (over `typedef_statement_map`).
Two distinct failures followed, so the skill's "two failed strategies" escalation
applied and the root invariant was restated: **each translation unit must see each
type alias exactly once.**

* *Strategy 1 (verbatim, all sources):* re-declared an alias the included shim
  already defines → duplicate definition; the vendor-adapter docker fixture exited
  30. Restricted the source to copied *sources* only.
* *Strategy 2 (verbatim, copied sources only):* still conflicts whenever the alias
  is an **anonymous aggregate** the test's own unit header also defines —
  `typedef struct { int v; } S;` twice names *two different* anonymous types:
  gcc `error: conflicting types for 'S'`. (Mechanically confirmed: duplicate
  *scalar/pointer* typedefs are legal C; duplicate *anonymous-aggregate* typedefs
  are not.)
* *Strategy 3 (accepted):* the interface header **`#include`s the copied header
  that defines the alias** instead of re-declaring it, so the defining file's own
  include guard yields one shared definition per TU. Verbatim text is the fallback
  only for an alias with no includable header (one written in a `.c`).

**F-A1 correction — the neighbouring `enum` shape (independent PRE_PR review).**
The one bounded independent PRE_PR `contract-reviewer` returned CHANGES_REQUIRED
with one mandatory finding the lead reproduced and accepted: the guard-less-header
check keyed on `scan_typedefs`'s `aggregate` flag, whose classifier
(`cscan._AGGREGATE_DEF_RE`) matched only `struct|union`. An anonymous **`enum`**
alias (`typedef enum { … } e_t;`) was therefore not recognised, the guard-less
refusal was bypassed, and an unguarded header's enum was `#include`d twice →
`redeclaration of enumerator`. The refined invariant — *an alias whose defining
statement has an inline `{…}` body cannot be duplicated and must be guarded* — is
derived from `typedef_statement_map` (already recorded) as `alias_inline_body`
(statement contains `{`), which covers `struct`, `union` and `enum` uniformly and
correctly excludes tag references and plain/pointer typedefs.

**F-A2 correction — F-A1 narrowed the predicate and regressed the tag-reference
shape (second independent PRE_PR review).** The reviewer ran on revision 3 (head
`fa753d9`) and returned CHANGES_REQUIRED with one mandatory new finding the lead
reproduced and accepted. F-A1's `"{" in alias_statement` predicate is
*complementary to*, not a substitute for, the earlier `aggregate` predicate:

* `typedef struct { … } S;` (anonymous body): `aggregate=True`, alias statement has `{`.
* `struct S { … }; typedef struct S S_t;` (tag reference): `aggregate=True`, alias
  statement has **no** `{` — the body lives in the *same header*, not in the alias
  statement.

So keying only on the alias statement dropped the tag-reference shape: a guard-less
header `struct S {…}; typedef struct S S_t;` with `void dep_send(S_t s)` emitted
`limitations == []` and an interface header `#include`ing the unguarded header →
`redefinition of 'struct S'` / `conflicting types for 'S_t'`. (Honesty note: at the
true base `dd122e7` this shape was *also* broken — verified in a `dd122e7`
worktree — so F-A2 is a *narrowing* of a fix, not a fresh regression; it is
nonetheless correct on the merits — though F-A4 below later showed the predicate
at this level was still unsound without the guard-enclosure check.) **F-A2 decision
— restate the invariant at the right level:** *a header is unsafe to include twice
iff it **defines** a `struct`/`union`/`enum` with an inline `{…}` body* — a
**header-level** property (initially `_header_defines_inline_type`,
comment/string-stripped), covering anonymous and tag-named bodies uniformly while
still admitting a header whose only typedefs are plain/pointer.

**F-A3 (self-found, pre-existing) — statement-line anchoring of the typedef
scanners.** An exhaustive guard-predicate probe surfaced a *separate* pre-existing
gap: `_TYPEDEF_RE`, `_AGGREGATE_REF_RE` and `_TYPEDEF_STMT_RE` all anchored at
`^[ \t]*typedef`, so a file-scope alias following an earlier declaration on the
**same line** (`struct S { int v; }; typedef struct S T;`, or
`int g(void); typedef unsigned short vendor_t;`) was never recorded; a mocked
declaration through it then lost its defining header and the interface header
carried the alias unresolved → `unknown type name '…'`, i.e. uncompilable C with
`limitations == []`. Confirmed pre-existing at base `dd122e7`. **Decision:** anchor
each statement on the start of a line *or* a `(?<=[;}])` lookbehind, so the
separating `;` is **not consumed** (a candidate that consumed it, `[;}][ \t]*typedef`,
was rejected by the probe: it swallowed the alias after a `;` and leaked a leading
`;` into the verbatim statement). Scope stays a statement-level scanner — a
`typedef` inside a function body on its own line keeps its pre-existing behaviour.

**F-A5 correction — the predicate must reason over branches, not text spans (fourth
independent PRE_PR review).** The reviewer on `7718932` returned CHANGES_REQUIRED
with a mandatory *narrower residual of the F-A4 class*: `_header_shares_safely`
accepted a `#define X` merely *appearing* between an `#ifndef X` and its `#endif`,
without preprocessor semantics. Two shapes read as guarded while a second inclusion
still repeats the inline body — an **`#else`/`#elif` branch that repeats the body**
(taken on the second inclusion) and a **`#define X` that is dead** (nested under
`#if 0`, so the guard never collapses). **F-A5 decision — restrict the claim instead
of chasing spans:** replace `_header_shares_safely` + `_guard_spans` with a
**sound-by-construction** predicate that proves safety only for a *simple* guard
(`_simple_guard_span`: prologue of comments/includes, `#ifndef X` / `#if !defined(X)`
**immediately** followed by `#define X`, a matching `#endif` with **no**
`#else`/`#elif` at the guard's own level) with every inline body inside it, and
refuses everything else as a **named limitation** (§C permits recording a precise
exclusion). `#pragma once`, a no-inline-body header and a nested inner `#if` inside
the guard stay admitted. **Escalation (binding):** fourth mandatory hole in this
class (F-A1→F-A2→F-A4→F-A5); if the next review finds another, the lead escalates to
owner arbitration rather than opening a fifth corrective pass.

**F-A4 correction — share-safety must prove the body is *inside* a guard (third
independent PRE_PR review).** The reviewer on `d7eb00e` returned CHANGES_REQUIRED
with a mandatory finding the lead reproduced and accepted: `_header_has_guard`
accepted *any* `#ifndef X`/`#define X` pair, so a guard-like block that does **not**
enclose the definition read as guarded — a mid-file feature toggle
(`#ifndef FEATURE_X … #endif` before an unguarded `typedef struct {…} S;`) or a
definition written **before** the real guard. The interface header then `#include`d
the header, the TU repeated the `{…}`, and gcc failed `conflicting types for 'S'`
with `limitations == []`. **F-A4 decision — restate the invariant soundly:** *a
header is safe to include twice iff **every** inline `struct`/`union`/`enum`
definition it contains lies inside an include guard, or the header is `#pragma
once`.* `_header_has_guard` + `_header_defines_inline_type` were replaced by
`_header_shares_safely` (+ `_guard_spans`: real guard regions `#ifndef X` /
`#if !defined(X)` with a matching `#define X`, measured to the correct `#endif` by
a nesting stack; `#pragma once` treated as guarded). This also closed the
reviewer's advisory finding (a `#pragma once` header with an inline body is no
longer false-refused). **Escalation note:** the share-safety class has now taken
three corrective passes (F-A1→F-A2, then F-A4); per the skill's escalation rule the
lead restated the invariant and adopted a *sound* predicate plus a **named
limitation** for the genuinely-unsupported shape (which §C permits), rather than a
third blind patch. If the next review finds another mandatory hole in this class,
escalate to owner arbitration.



## Options considered

1. **Refuse every parameter/return that names any typedef alias.** Simplest, but it
   would blanket-refuse legitimate scalar aliases (`uint8_t`, `mode_t`,
   `vendor_adc_sample_t`) that the generator renders faithfully — the plan forbids
   blanket-refusing a representable shape (§C). Rejected.
2. **Resolve typedefs only where they are already resolved (SUT path) and document
   the mock gap as a limitation.** That leaves an admitted-but-uncompilable bench,
   which §A/§C explicitly forbid ("never a silent PASS"). Rejected.
3. **Make typedef resolution common to every path that emits C, and make each
   private interface header self-sufficient.** Accepted.

## Decision

* `mtm/cscan.py`
  * `scan_typedefs` records the alias name explicitly for an opaque declarator
    (`_FNPTR_ALIAS_RE` for `(*name)(`, an identifier-before-`(`/`[` fallback for a
    function *type* alias `typedef int Func(int);`, and an array fallback), so the
    `opaque` marker is real and the refusal fires.
  * `typedef_statement_map(text)` returns the **verbatim** `typedef` statement for
    each alias in a C text (an inline struct/union/enum body included), so a header
    can re-declare an alias with its exact meaning rather than a re-spelling.
* `mtm/gen.py`
  * `split_declaration` and `mock_controls` take the resolved `typedefs`; the real
    call site passes the same map the SUT path uses, so a mocked dependency's
    pointer alias resolves to its true depth and `_case_problem` refuses an
    unfaithful write-through with its named limitation.
  * `generate_suite` builds an alias→statement map and an alias→defining-header map
    from every copied source/header; `_typedef_aliases_for_decl` **includes** the
    defining header (falling back to verbatim text for a `.c`-defined alias) for the
    aliases a mock declaration references (transitively, to a fixpoint).
  * A defining header that is **not safe to include twice** — one that repeats an
    inline `struct`/`union`/`enum` definition because no include guard encloses it
    (anonymous or tag-named body; a guard-like `#ifndef` that does not enclose the
    definition does not count) — is the one shape the include-sharing cannot
    collapse; its mock scenarios are refused with a precise limitation
    (`refused_mocks`) and the affected scenarios are skipped, rather than emitting
    an uncompilable test whose `limitations == []` (plan §C). A header whose only
    typedefs are plain/pointer stays admitted, as does a `#pragma once` header or a
    header whose every inline body is guarded — verified end-to-end in the image.

## Consequences

* **Positive:** the three shapes are now either faithfully rendered or refused with
  a **named limitation**; no unjustified pointer cast, no uncompilable C admitted
  with `limitations == []`. Regression tests added for each, the interface header
  is compiled for real with `gcc -Wall -Werror` in the test (a real compile, not a
  string equality), and the F3 and anonymous-aggregate mock paths are executed
  **end-to-end in the product image** (compile + link + `PASS`), including two
  mocked symbols sharing one defining header. The refusal message is the existing
  "function-pointer/array `typedef` … not supported" class, so no new vocabulary.
* **Cost / limit:** the array/function-*type* alias fallback is a heuristic over
  the declarator text (the scanner is not a full C parser); it is exercised by unit
  tests for the forms this generator supports and re-marked for the plain
  `([A-Za-z_]\w*)$` name only when the declarator is opaque. A guard-less header
  that defines an alias with an inline body (anonymous `struct`/`union`/`enum`) and
  is also the unit header is refused (named limitation), not silently broken.
* The changers are code, so the earlier "all green" fingerprint is invalidated; the
  applicable tests and the independent final review are re-run on the new candidate
  before publication.

## References

* `mtm/cscan.py` (`scan_typedefs`, `typedef_statement_map`, `_TYPEDEF_RE`,
  `_AGGREGATE_REF_RE`, `_TYPEDEF_STMT_RE`), `mtm/gen.py`
  (`split_declaration`, `mock_controls`, `_typedef_aliases_for_decl`,
  `_decl_alias_tokens`, `_header_shares_safely`, `_simple_guard_span`, `generate_suite`).
* `tests/test_pr2_runner.py` (twelve §C regression tests: opaque alias, pointer-typedef
  depth, interface-header alias, anonymous-aggregate sharing, guard-less aggregate
  limitation, guard-less enum limitation + guarded-enum compile, guard-less inline
  tag-body limitation, guard-like-block-not-enclosing-the-body limitation,
  `#pragma once` inline body shareable, conditional-branch-of-guard limitation,
  guarded-nested-conditional body shareable, combined-line alias + in-image basics),
  `tests/test_pr2_units.py` (opaque-alias scanner test).
* PR 3 §A/§B/§C and DoD 3 in `docs/plan/implementation.md`;
  `worklog/log/2026-10-18-pr3.md` (sessions 4–10).
