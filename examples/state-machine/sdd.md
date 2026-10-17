# SDD — Controller state machine

- **STM-001**: Any EV_FAULT event SHALL transition to ST_FAULT.
- **STM-002**: In ST_FAULT, only EV_RESET SHALL return ST_STOPPED.
- **STM-003**: ST_STOPPED + EV_START SHALL transition to ST_RUNNING.
- **STM-004**: ST_RUNNING + EV_STOP SHALL transition to ST_STOPPED.
- **STM-005**: Every other valid state/event pair SHALL leave state unchanged.

Fixture scope: valid enumeration values only. Invalid underlying values are unspecified and must not acquire invented SDD oracles. Exercise transitions, sequences and state invariants.
