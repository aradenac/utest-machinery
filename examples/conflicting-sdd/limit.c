#include "limit.h"
bool overspeed(uint16_t speed_kph)
{
    return speed_kph > 100u;
}
