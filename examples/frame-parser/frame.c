#include "frame.h"
/* Intentionally faulty: length is not checked before reading data[2]. */
int parse_frame(const uint8_t *data, size_t length, uint8_t *command)
{
    if (data == 0 || command == 0) return -1;
    if (length > 4u || data[0] != 0xA5u) return -1;
    *command = data[2];
    return 0;
}
