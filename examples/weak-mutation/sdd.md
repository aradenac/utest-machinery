# SDD — Threshold classification

- **MUT-001**: `is_high(x)` SHALL return 1 for `x >= 100`.
- **MUT-002**: `is_high(x)` SHALL return 0 for `x < 100`.
- **MUT-003**: The function SHALL have no side effects.

The optional pre-authored [weak reference test](weak_test.c) checks only 0 and 200. A relational mutation that changes `>=` to `>` can survive that weak suite; adding the boundary input 100 should kill it. This case qualifies mutation reporting, not the LLM's ability to infer an oracle when the expected result is already known.
