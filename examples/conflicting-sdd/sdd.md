# SDD — Contradictory overspeed requirements (deliberate)

- **CNF-001**: An alarm SHALL be active if speed is **greater than or equal to** 100 km/h.
- **CNF-002**: An alarm SHALL be inactive if speed is **less than or equal to** 100 km/h.
- **CNF-003**: `overspeed(speed_kph)` SHALL return whether the alarm is active.

At speed exactly 100, CNF-001 and CNF-002 conflict. Do not silently select the implementation's interpretation as the oracle. At 100, classification must be SDD_CONFLICT/UNDETERMINED and recommend resolving the equality rule.
