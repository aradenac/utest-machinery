# Reference C/SDD inputs

These small fixtures are **inputs to qualify the future machinery**, not generated tests or a claim that verification has run. Each SDD defines requirement IDs. None requires an automotive vendor SDK.

| Fixture | Intent |
| --- | --- |
| saturation | Numeric boundaries, intentional off-by-one implementation defect |
| safety-decision | Boolean decisions useful for MC/DC and relational mutations |
| mocked-sensor | External dependency and error/recovery interactions |
| underspecified | SDD omits an observable behavior; do not invent oracle |
| state-machine | State transitions and valid-input invariants |
| frame-parser | Intentionally defective bounds/checksum handling for sanitizer and fuzz tests |
| vendor-adapter | Proprietary SDK header absent; generated private host shim/mock and fidelity assumptions |
| conflicting-sdd | Requirements conflict at one boundary; oracle remains undetermined |
| weak-mutation | Weak independent seed test exposes a surviving boundary mutant |

The [independent outcome reference](expected-outcomes.md) must not be provided to the generating LLM during blind qualification.

Additional fixtures required by [qualification specification](../docs/qualification/qualification.md): workflow/tool failures, revision and collaboration, credentials/prompt injection, timing/resource observation.

Independent test expectations should be maintained separately from generated tests so qualification does not merely reproduce the LLM's own assumptions.
