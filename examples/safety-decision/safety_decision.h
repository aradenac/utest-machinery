#ifndef EXAMPLE_SAFETY_DECISION_H
#define EXAMPLE_SAFETY_DECISION_H
#include <stdbool.h>
#include <stdint.h>
bool permit_enable(bool ignition_on, uint16_t supply_mv, bool fault_latched);
#endif
