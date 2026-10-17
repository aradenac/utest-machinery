#include "sensor.h"
sensor_status_t sample_sensor(uint16_t *last_accepted)
{
    uint16_t current = 0;
    int status = platform_read_sensor(&current);
    if (status != 0) {
        return SENSOR_ERROR;
    }
    if (current > 1000u) {
        return SENSOR_OUT_OF_RANGE;
    }
    *last_accepted = current;
    return SENSOR_OK;
}
