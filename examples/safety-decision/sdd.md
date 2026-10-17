# SDD — Safety interlock

- **SAFE-001**: `permit_enable` SHALL return true only if ignition is on, supply voltage is at least 9000 mV and no fault is latched.
- **SAFE-002**: If any requirement from SAFE-001 is false, it SHALL return false.
- **SAFE-003**: The function SHALL be pure and support supply values throughout `uint16_t`.

MC/DC planning must include independent influence of ignition, voltage threshold and latched-fault conditions, with feasible short-circuit evaluation.
