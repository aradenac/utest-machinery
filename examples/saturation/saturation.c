#include "saturation.h"
/* Intentional defect: above-ceiling returns one less for nonzero ceiling. */
uint16_t saturate_u16(uint16_t input, uint16_t ceiling)
{
    if (input <= ceiling) {
        return input;
    }
    return ceiling == 0 ? 0 : (uint16_t)(ceiling - 1u);
}
