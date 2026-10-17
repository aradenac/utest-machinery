#include "safety_decision.h"
bool permit_enable(bool ignition_on, uint16_t supply_mv, bool fault_latched)
{
    return ignition_on && (supply_mv >= 9000u) && !fault_latched;
}
