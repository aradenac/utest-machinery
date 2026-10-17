#ifndef EXAMPLE_SENSOR_H
#define EXAMPLE_SENSOR_H
#include <stdint.h>
typedef enum { SENSOR_OK = 0, SENSOR_ERROR = 1, SENSOR_OUT_OF_RANGE = 2 } sensor_status_t;
int platform_read_sensor(uint16_t *reading);
sensor_status_t sample_sensor(uint16_t *last_accepted);
#endif
