# SDD — Mockable sensor read

- **SEN-001**: `sample_sensor` SHALL call `platform_read_sensor` exactly once per invocation.
- **SEN-002**: On dependency error (nonzero status), it SHALL return SENSOR_ERROR and not modify the previous accepted reading.
- **SEN-003**: On success and reading <= 1000, it SHALL store the reading and return SENSOR_OK.
- **SEN-004**: On success and reading > 1000, it SHALL return SENSOR_OUT_OF_RANGE and retain the previous accepted reading.
- **SEN-005**: A subsequent valid reading after an error SHALL permit normal recovery.

`platform_read_sensor` is an external dependency deliberately missing from this fixture. The generated bench should mock it and verify returned values, stored state, invocation count, fault/recovery sequences, and the absence of unintended updates.
