#ifndef EXAMPLE_FRAME_H
#define EXAMPLE_FRAME_H
#include <stddef.h>
#include <stdint.h>
/* 0 on valid frame, nonzero otherwise. */
int parse_frame(const uint8_t *data, size_t length, uint8_t *command);
#endif
