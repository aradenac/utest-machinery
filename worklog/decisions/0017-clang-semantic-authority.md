# 0017 — Clang semantic authority for generated C tests

**Date:** 2026-10-18  
**Status:** Approved by product owner; implementation pending in existing PR #35  
**Scope:** uTest Machinery, milestone 3, C type/interface fidelity only

## Context / incident

Draft [PR #35](https://github.com/aradenac/utest-machinery/pull/35) implemented substantial offline verification and qualification work but became blocked on CMock interface-header sharing. Independent reviews found F-A1/A2/A4/A5/A6: repeated same-mechanism counterexamples to regex-based include-guard/preprocessor-safety heuristics. Correcting one syntactic case did not establish the general semantic condition. This is **product-design complexity** (R20/R24, IMP-17), separate from the development-agent convergence failure (R25/IMP-16, AP-07).

## Decision

1. **Keep user-visible scope and milestones.** Preserve existing Ceedling/Unity/CMock test bench, frozen no-LLM verify, R04/R22 qualification and previously demonstrated behavior. Rework only the unsound type/interface slice in PR #35.
2. **Clang 18/libclang 18 is the sole authoritative semantic reader** for selected host-build C function declarations, resolved typedefs, pointer depth, qualifiers and record/enum identities. Use the pinned in-image library and matching versioned bindings with the effective selected compilation arguments/includes/defines. Implement a minimal adapter to existing generator structures; no general C parser, parallel AST database, second public command or new LLM component.
3. **Use the native mock generator and preprocessor:** retain Ceedling/CMock; use its `:use_test_preprocessor: :mocks` capability as needed. Favor project declarations already visible in the selected build context. Stop adding homegrown include-guard, branch or preprocessor-regex classifiers, including `_header_shares_safely`; retire redundant code and tests as the new path replaces them. Avoid duplicating inline type bodies.
4. **Evidence, not guesses:** compiled real generated C is a mandatory gate; checks require the correct and seeded-faulty SUT contrasts for key pointer/typedef/mocked-aggregate families, named exclusions and no false findings. A missing SDK or unsuitable compilation context blocks affected units with actionable diagnostics. A broken Clang/CMock tool is a tool failure. A generated harness build error is not a SUT defect. Compilation alone does not prove the oracle or post-call observable.
5. **Finite scope:** preserve the already approved pointer/void cases when representable. Do not claim generic C support; do not silently reduce an accepted domain. Reuse unaffected PR #35 verification evidence and rerun only affected tests plus the required integrated candidate qualification; no competitive design spikes or unbounded syntactic-variant suites.

## Alternatives deliberately not selected

- Extend the existing regex/include-guard analyzer: rejected because its underlying model of preprocessor semantics is incomplete and led to repeated same-family failures.
- Broad replacement of all scanner/inventory logic or a proprietary AST framework: rejected as disproportionate to the milestone. Lexical scanning may remain non-authoritative for cheap discovery.
- Blanket-refuse pointer aliases, void cases or aggregates to make tests pass: rejected where the existing normative contract requires representable behavior.
- Switch the development LLM as a substitute for an architecture decision: no evidence that a model swap would resolve this semantic responsibility mismatch.

## Consequences / acceptance

Updated normative sources: [architecture](../../docs/architecture/architecture.md), [plan milestone 3](../../docs/plan/implementation.md), [verification strategy](../../docs/architecture/verification-strategy.md), [data contracts](../../docs/architecture/data-contracts.md); preventive impact/action: [risk register](../../docs/project/risks.md) (IMP-17/AP-08). The prior branch's decision 0016 records the blocked attempts and user arbitration request; this decision resolves the architecture question without claiming implementation success. The developer resumes **the existing** draft PR #35 after reconciling up-to-date main instructions, works to the revised finite DoD and stops for independent review; no automatic feature merge or next milestone.

**References:** [official libclang API](https://clang.llvm.org/docs/LibClang.html), [Ceedling mock-preprocessing settings](https://docs.throwtheswitch.org/Ceedling/1.2.0/configuration/reference/project/).
