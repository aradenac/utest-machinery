#include "platform_logic.h"
#include "vendor_adc.h" /* Deliberately absent proprietary SDK header. */
uint16_t read_adc_or_zero(uint8_t channel)
{
    vendor_adc_sample_t sample = {0};
    if (vendor_adc_read(channel, &sample) != VENDOR_ADC_OK) {
        return 0u;
    }
    return sample.raw > 4095u ? 4095u : sample.raw;
}
