#ifndef OP_HAL_H
#define OP_HAL_H

#include <stdbool.h>
#include <stdint.h>

#include "op_fw.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Implemented once per target: firmware/host (simulated plant), firmware/nrf52840 (real hardware). */

typedef enum {
    OP_HAL_OK = 0,
    OP_HAL_NO_POWER = 1,        /* safety MCU has cut motor power */
    OP_HAL_END_OF_TRAVEL = 2,   /* plunger reached the end stop: reservoir empty */
    OP_HAL_FAULT = 3,           /* stall, driver fault, anything else */
} op_hal_result_t;

/* Advance the plunger by exactly one pulse (pulse_cu of insulin). */
op_hal_result_t op_hal_motor_pulse(op_fw_t *fw, uint8_t kind);

/* Start retracting the plunger to the home switch. May complete asynchronously (see op_hal_busy). */
bool op_hal_motor_rewind(op_fw_t *fw);

/* True while the motor is busy with an asynchronous operation (e.g. rewinding). */
bool op_hal_busy(op_fw_t *fw);

/* Force on the plunger in calibrated units (0xFFFF if the sensor cannot be read: fails safe). */
uint16_t op_hal_line_force(op_fw_t *fw);

uint8_t op_hal_battery_pct(op_fw_t *fw);

/* Persistence hooks: called after the change has been applied in RAM. */
void op_hal_history_changed(op_fw_t *fw, const op_record_t *rec);
void op_hal_state_changed(op_fw_t *fw);
void op_hal_time_set(op_fw_t *fw, uint32_t minute);

#ifdef __cplusplus
}
#endif

#endif
