#ifndef EXAMPLE_MODE_H
#define EXAMPLE_MODE_H
#include <stdint.h>
typedef enum { MODE_IDLE = 0, MODE_RUN = 1, MODE_INVALID = 2 } mode_t;
mode_t decode_mode(uint8_t value);
#endif
