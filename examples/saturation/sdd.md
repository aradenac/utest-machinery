# SDD — Saturating unsigned value

- **SAT-001**: `saturate_u16(input, ceiling)` SHALL return `input` if `input <= ceiling`.
- **SAT-002**: The function SHALL return `ceiling` if `input > ceiling`.
- **SAT-003**: The function SHALL have no persistent state or external side effects.
- **SAT-004**: All `uint16_t` input and ceiling values, including 0 and 65535, SHALL be supported.

Target test partitions include below, equal and above the ceiling, zero ceiling and maximum inputs. The seed implementation intentionally violates SAT-002, which is a **known independent ground-truth defect**, not a passing reference.
