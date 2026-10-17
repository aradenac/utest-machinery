#include "mode.h"
mode_t decode_mode(uint8_t value)
{
    if (value == 0u) return MODE_IDLE;
    if (value == 1u) return MODE_RUN;
    return MODE_INVALID;
}
