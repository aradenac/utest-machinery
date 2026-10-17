# Reference C/SDD inputs

These small fixtures are **inputs to qualify the future machinery**, not generated tests or a claim that verification has run. Each SDD defines requirement IDs. None requires an automotive vendor SDK.

| Fixture | Intent |
| --- | --- |
| saturation | Numeric boundaries, intentional off-by-one implementation defect |
| safety-decision | Boolean decisions useful for MC/DC and relational mutations |
| mocked-sensor | External dependency and error/recovery interactions |
| underspecified | SDD omits an observable behavior; do not invent oracle |

Additional fixtures required by [qualification specification](../docs/qualification/qualification.md): state machine, packet parser with injectable buffer fault, vendor-style header/ABI adaptation, conflicting SDD, mutation survivor, workflow/tool failures, revision and collaboration, credentials/prompt injection, timing/resource observation.

Independent test expectations should be maintained separately from generated tests so qualification does not merely reproduce the LLM's own assumptions.
