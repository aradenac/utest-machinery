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
  * A guard-less defining header that carries an **anonymous-aggregate** alias is
    the one shape the include-sharing cannot collapse; its mock scenarios are
    refused with a precise limitation (`refused_mocks`) and the affected scenarios
    are skipped, rather than emitting an uncompilable test whose `limitations == []`
    (plan §C). A guard-less *scalar* alias stays admitted (duplicate scalar typedef
    is legal C) — verified end-to-end in the image.

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
  that defines an *anonymous-aggregate* alias and is also the unit header is
  refused (named limitation), not silently broken.
* The changers are code, so the earlier "all green" fingerprint is invalidated; the
  applicable tests and the independent final review are re-run on the new candidate
  before publication.

## References

* `mtm/cscan.py` (`scan_typedefs`, `typedef_statement_map`), `mtm/gen.py`
  (`split_declaration`, `mock_controls`, `_typedef_aliases_for_decl`,
  `_decl_alias_tokens`, `_header_has_guard`, `generate_suite`).
* `tests/test_pr2_runner.py` (four regression tests: opaque alias, pointer-typedef
  depth, interface-header alias, guard-less aggregate limitation),
  `tests/test_pr2_units.py` (opaque-alias scanner test).
* PR 3 §A/§B/§C and DoD 3 in `docs/plan/implementation.md`;
  `worklog/log/2026-10-18-pr3.md` (sessions 4–5).
