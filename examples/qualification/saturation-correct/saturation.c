#include "saturation.h"
/* Reference-correct SUT for R22 sensitivity qualification: conforms to SAT-002. */
uint16_t saturate_u16(uint16_t input, uint16_t ceiling)
{
    if (input <= ceiling) {
        return input;
    }
    return ceiling;
}
