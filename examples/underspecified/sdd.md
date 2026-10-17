# SDD — Mode decoding (intentionally underspecified)

- **MODE-001**: Input byte 0 SHALL represent MODE_IDLE.
- **MODE-002**: Input byte 1 SHALL represent MODE_RUN.
- **MODE-003**: The function SHALL not modify external or persistent state.

**Intentional specification gap:** behavior for input bytes 2..255 is not defined. Generated tests may observe the implementation, but MUST NOT claim SDD conformance or assign a normative expected result for these inputs. Report a concrete proposal to specify rejection/default handling, with rationale and test conditions.
