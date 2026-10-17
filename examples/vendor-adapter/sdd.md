# SDD — External vendor ADC adapter

- **VND-001**: `read_adc_or_zero(channel)` SHALL call `vendor_adc_read(channel, &sample)` once.
- **VND-002**: If vendor read returns anything except `VENDOR_ADC_OK`, the function SHALL return zero.
- **VND-003**: On successful read, return `sample.raw` saturated at 4095.

The proprietary `vendor_adc.h` is **intentionally unavailable**. The SDD documents the necessary external interface: `vendor_adc_sample_t` contains unsigned 16-bit member `raw`; `vendor_adc_read(uint8_t, vendor_adc_sample_t *)` returns a status value and `VENDOR_ADC_OK` denotes success. The feasibility engine must generate a private compatible shim/mock in its **bench**, not change the C or create a vendor header in the original source directory. Status must be ADAPTED with declared type/ABI assumptions, or BLOCKED with concrete reasons.
