#include "threshold.h"
uint8_t is_high(uint16_t value)
{
    return (uint8_t)(value >= 100u);
}
