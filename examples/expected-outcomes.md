# Independent fixture truth — qualification only

**Do not supply this file to the generation agent when measuring blind discovery.** It is a human-authored acceptance reference for qualification and CI. Keep it outside the selected SDD/source scopes of the test machinery.

| Fixture | Independent expected outcome |
| --- | --- |
| saturation | The above-ceiling nonzero case contradicts SAT-002 (returns ceiling-1). |
| safety-decision | Conforms on all specified valid boolean and uint16_t inputs; feasible MC/DC pairs should be identified. |
| mocked-sensor | The unresolved external symbol is mockable; tests must exercise success, error, out-of-range and recovery. |
| underspecified | Input bytes 2–255 remain undecidable against the SDD despite implementation returning MODE_INVALID. |
| state-machine | Valid-state/event transitions should conform; invalid enums deliberately unspecified. |
| frame-parser | Short buffers can cause an out-of-bounds read; malformed four-byte frames can be wrongly accepted, violating FRM-001–004. |

This file is not a substitute for implementing the full Q-01–Q-18 fixture suite in the qualification plan.
