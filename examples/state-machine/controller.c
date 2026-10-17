#include "controller.h"
state_t transition(state_t state, event_t event)
{
    if (event == EV_FAULT) return ST_FAULT;
    if (state == ST_FAULT) return event == EV_RESET ? ST_STOPPED : ST_FAULT;
    if (state == ST_STOPPED && event == EV_START) return ST_RUNNING;
    if (state == ST_RUNNING && event == EV_STOP) return ST_STOPPED;
    return state;
}
