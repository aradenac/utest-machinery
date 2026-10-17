#ifndef EXAMPLE_CONTROLLER_H
#define EXAMPLE_CONTROLLER_H
typedef enum { ST_STOPPED, ST_RUNNING, ST_FAULT } state_t;
typedef enum { EV_START, EV_STOP, EV_FAULT, EV_RESET } event_t;
state_t transition(state_t state, event_t event);
#endif
