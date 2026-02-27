# worker.py
from __future__ import annotations

import time
import os
from typing import Any, Dict, Optional

import serial.tools.list_ports
from serial import SerialException
from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot, QTimer

from devices_vesc import VESCDevice, VESCValues
from devices_psu_riden import RidenPSU
from logger_csv import CSVLogger

from cyclograms import (
    STARTER_DUTY, STARTER_WAIT_S, STARTER_MIN_RPM,
    STARTER_IGNITION_DUTY_PROFILE, IGNITION_TARGET_STARTER_RPM,
    VALVE_V_1, VALVE_V_2, VALVE_I, VALVE_SWITCH_S,
    PUMP_PROFILE_XLSX, PUMP_PROFILE_SHEET, IGNITION_TIMEOUT_S,
    RUNNING_STARTER_DUTY,
    COOLING_DURATION_S, COOLING_DEFAULT_DUTY,
)
from pump_profile import load_pump_profile_xlsx, interp_profile, PumpProfile


def _interp_time_value(points, t: float) -> float:
    """points: [(time, value)] linear interp, clamped."""
    if not points:
        return 0.0
    x = float(t)
    if x <= points[0][0]:
        return float(points[0][1])
    if x >= points[-1][0]:
        return float(points[-1][1])
    for i in range(1, len(points)):
        if x <= points[i][0]:
            t0, t1 = float(points[i-1][0]), float(points[i][0])
            y0, y1 = float(points[i-1][1]), float(points[i][1])
            if t1 <= t0:
                return y1
            a = (x - t0) / (t1 - t0)
            return y0 + a * (y1 - y0)
    return float(points[-1][1])


class ControllerWorker(QObject):
    sample = pyqtSignal(object)
    status = pyqtSignal(object)
    error = pyqtSignal(str)
    log = pyqtSignal(str)

    def __init__(self, dt: float = 0.05, parent=None):
        super().__init__(parent)
        self.dt = float(dt)

        self.ui_hz = 5.0
        self.log_hz = 5.0
        self._ui_dt = 1.0 / self.ui_hz
        self._log_dt = 1.0 / self.log_hz
        self._next_ui_emit = 0.0
        self._next_log_write = 0.0

        self._pump_profile_mtime = None

        self._timer = QTimer(self)
        self._timer.setInterval(max(10, int(self.dt * 1000)))
        self._timer.timeout.connect(self._tick)

        self._t0 = time.time()
        self.stage = "idle"

        self.pump = VESCDevice(timeout=0.01)
        self.starter = VESCDevice(timeout=0.01)
        self.psu = RidenPSU()

        self.pole_pairs_pump = 7
        self.pole_pairs_starter = 3

        # targets
        self.pump_target = {"mode": "duty", "value": 0.0}
        self.starter_target = {"mode": "duty", "value": 0.0}
        self.psu_target = {"v": 0.0, "i": 0.0, "out": False}
        self._psu_dirty = False

        # last values
        self._last_pump = VESCValues()
        self._last_starter = VESCValues()
        self._last_psu: Dict[str, Any] = {}

        # rate limits PSU
        self._psu_next_read = 0.0
        self._psu_next_cmd = 0.0

        # logger
        self.logger = CSVLogger()
        self.logging_on = False
        self._next_flush_t = 0.0

        self._in_tick = False

        # -------- Cyclogram FSM
        self.cycle_active = False
        self.cycle_state = "Idle"
        self._state_t0 = 0.0

        self._pump_profile: Optional[PumpProfile] = None
        self._ignition_entered = 0.0

        # running: allow manual pump without cancelling cycle
        self._running_manual_pump_enabled = False

        # cooling duty from UI
        self.cooling_duty = COOLING_DEFAULT_DUTY

    @staticmethod
    def list_ports() -> list[str]:
        return [p.device for p in serial.tools.list_ports.comports()]

    def _ensure_pump_profile(self) -> bool:
        try:
            base = os.path.dirname(os.path.abspath(__file__))
            path = os.path.join(base, PUMP_PROFILE_XLSX)
            mtime = os.path.getmtime(path)
            if (self._pump_profile is None) or (self._pump_profile_mtime != mtime):
                self._pump_profile = load_pump_profile_xlsx(path, sheet_name=PUMP_PROFILE_SHEET)
                self._pump_profile_mtime = mtime
            return bool(self._pump_profile and self._pump_profile.t)
        except Exception as e:
            self._fault(f"Cannot load pump cyclogram: {e}")
            return False

    # -------- lifecycle
    @pyqtSlot()
    def start(self) -> None:
        self._t0 = time.time()
        self.stage = "idle"
        self._emit_connected()
        self._timer.start()

    @pyqtSlot()
    def stop(self) -> None:
        try:
            self._timer.stop()
        except Exception:
            pass
        self._force_all_off()
        self._disconnect_pump()
        self._disconnect_starter()
        self._disconnect_psu()
        try:
            self.logger.stop()
        except Exception:
            pass
        self.logging_on = False
        self._emit_connected()

    # -------- UI commands
    @pyqtSlot(str)
    def cmd_ready(self, prefix: str) -> None:
        self._t0 = time.time()
        self.stage = "ready"
        self._stop_cycle_internal()

        try:
            self.logger.stop()
        except Exception:
            pass

        try:
            path = self.logger.start(prefix=(prefix or "session"))
            now = time.time()
            self._next_ui_emit = now
            self._next_log_write = now
            self.logging_on = True
            self._next_flush_t = time.time() + 1.0
            self.status.emit({"ready": True, "log_path": path})
        except Exception as e:
            self.logging_on = False
            self.error.emit(f"Logger start failed: {e}")

        self._emit_connected()

    @pyqtSlot()
    def cmd_update_reset(self) -> None:
        self._t0 = time.time()
        self.stage = "idle"
        self._stop_cycle_internal()
        self._emit_connected()

    @pyqtSlot()
    def cmd_run_cycle(self) -> None:
        # load pump profile
        try:
            base = os.path.dirname(os.path.abspath(__file__))
            path = os.path.join(base, PUMP_PROFILE_XLSX)
            self._pump_profile = load_pump_profile_xlsx(path, sheet_name=PUMP_PROFILE_SHEET)
            if not self._ensure_pump_profile():
                return
            if not self._pump_profile.t:
                raise RuntimeError("Pump profile is empty")
        except Exception as e:
            self._fault(f"Cannot load pump cyclogram: {e}")
            return

        self.cycle_active = True
        self._running_manual_pump_enabled = False
        self._enter_state("Starter")

    @pyqtSlot(float)
    def cmd_cooling_cycle(self, duty: float) -> None:
        self.cooling_duty = max(0.0, min(1.0, float(duty)))
        self.cycle_active = True
        self._running_manual_pump_enabled = False
        self._enter_state("Cooling")

    @pyqtSlot()
    def cmd_stop_all(self) -> None:
        self._stop_cycle_internal()
        self.stage = "stop"
        self._force_all_off()
        self._emit_connected()

    # ---- connect/disconnect
    @pyqtSlot(str)
    def cmd_connect_pump(self, port: str) -> None:
        if not port:
            return
        try:
            self.pump.connect(port)
            self.log.emit(f"Pump connected: {port}")
        except Exception as e:
            self.error.emit(f"Pump connect error: {e}")
            self._disconnect_pump()
        self._emit_connected()

    @pyqtSlot()
    def cmd_disconnect_pump(self) -> None:
        self.pump_target = {"mode": "duty", "value": 0.0}
        self._disconnect_pump()
        self._emit_connected()

    @pyqtSlot(str)
    def cmd_connect_starter(self, port: str) -> None:
        if not port:
            return
        try:
            self.starter.connect(port)
            self.log.emit(f"Starter connected: {port}")
        except Exception as e:
            self.error.emit(f"Starter connect error: {e}")
            self._disconnect_starter()
        self._emit_connected()

    @pyqtSlot()
    def cmd_disconnect_starter(self) -> None:
        self.starter_target = {"mode": "duty", "value": 0.0}
        self._disconnect_starter()
        self._emit_connected()

    @pyqtSlot(str)
    def cmd_connect_psu(self, port: str) -> None:
        if not port:
            return
        try:
            self.psu.connect(port)
            self.log.emit(f"PSU connected: {port}")
        except Exception as e:
            self.error.emit(f"PSU connect error: {e}")
            self._disconnect_psu()
        self._emit_connected()

    @pyqtSlot()
    def cmd_disconnect_psu(self) -> None:
        self.psu_target = {"v": 0.0, "i": 0.0, "out": False}
        self._psu_dirty = True
        self._disconnect_psu()
        self._emit_connected()

    # ---- params
    @pyqtSlot(int)
    def cmd_set_pole_pairs_pump(self, pp: int) -> None:
        self.pole_pairs_pump = max(1, int(pp))

    @pyqtSlot(int)
    def cmd_set_pole_pairs_starter(self, pp: int) -> None:
        self.pole_pairs_starter = max(1, int(pp))

    # ---- manual targets
    @pyqtSlot(float)
    def cmd_set_pump_duty(self, duty: float) -> None:
        # In Running: allow manual pump without stopping cycle
        if self.cycle_active and self.cycle_state == "Running":
            self.pump_target = {"mode": "duty", "value": max(0.0, min(1.0, float(duty)))}
            return
        self._stop_cycle_internal()
        self.pump_target = {"mode": "duty", "value": max(0.0, min(1.0, float(duty)))}

    @pyqtSlot(float)
    def cmd_set_pump_rpm(self, rpm: float) -> None:
        if self.cycle_active and self.cycle_state == "Running":
            self.pump_target = {"mode": "rpm", "value": float(rpm)}
            return
        self._stop_cycle_internal()
        self.pump_target = {"mode": "rpm", "value": float(rpm)}

    @pyqtSlot(float)
    def cmd_set_starter_duty(self, duty: float) -> None:
        # manual starter cancels cyclogram always
        self._stop_cycle_internal()
        self.starter_target = {"mode": "duty", "value": max(0.0, min(1.0, float(duty)))}

    @pyqtSlot(float)
    def cmd_set_starter_rpm(self, rpm: float) -> None:
        self._stop_cycle_internal()
        self.starter_target = {"mode": "rpm", "value": float(rpm)}

    # ---- PSU manual
    @pyqtSlot(float, float)
    def cmd_psu_set_vi(self, v: float, i: float) -> None:
        # manual valve cancels cyclogram always
        self._stop_cycle_internal()
        self.psu_target["v"] = float(v)
        self.psu_target["i"] = float(i)
        self._psu_dirty = True
        self._psu_next_cmd = 0.0

    @pyqtSlot(bool)
    def cmd_psu_output(self, on: bool) -> None:
        self._stop_cycle_internal()
        self.psu_target["out"] = bool(on)
        self._psu_dirty = True
        self._psu_next_cmd = 0.0

    # -------- Cyclogram internal helpers
    def _enter_state(self, name: str):
        self.cycle_state = name
        self._state_t0 = time.time()
        self.stage = name
        self._emit_connected()

        if name == "Starter":
            # starter duty 0.05, pump off, valve off
            self.starter_target = {"mode": "duty", "value": STARTER_DUTY}
            self.pump_target = {"mode": "duty", "value": 0.0}
            self.psu_target = {"v": 0.0, "i": 0.0, "out": False}
            self._psu_dirty = True

        elif name == "Ignition":
            self._ignition_entered = time.time()
            # valve on V1 first
            self.psu_target = {"v": VALVE_V_1, "i": VALVE_I, "out": True}
            self._psu_dirty = True

        elif name == "Running":
            self._running_manual_pump_enabled = True
            # keep valve on V2
            self.psu_target = {"v": VALVE_V_2, "i": VALVE_I, "out": True}
            self._psu_dirty = True
            # keep starter at chosen duty (can change later)
            self.starter_target = {"mode": "duty", "value": RUNNING_STARTER_DUTY}

        elif name == "Cooling":
            # valve off, pump off, starter duty from UI
            self.psu_target = {"v": 0.0, "i": 0.0, "out": False}
            self._psu_dirty = True
            self.pump_target = {"mode": "duty", "value": 0.0}
            self.starter_target = {"mode": "duty", "value": self.cooling_duty}

        elif name == "Fault":
            self._force_all_off()

    def _stop_cycle_internal(self):
        self.cycle_active = False
        self.cycle_state = "Idle"
        self._running_manual_pump_enabled = False

    def _fault(self, msg: str):
        self.cycle_active = False
        self._enter_state("Fault")
        self.error.emit(msg)

    # -------- internals
    def _emit_connected(self) -> None:
        self.status.emit({
            "connected": {
                "pump": self.pump.is_connected,
                "starter": self.starter.is_connected,
                "psu": self.psu.is_connected,
            },
            "stage": self.stage,
            "log_path": self.logger.path,
        })

    def _disconnect_pump(self) -> None:
        try:
            self.pump.disconnect()
        except Exception:
            pass
        self._last_pump = VESCValues()

    def _disconnect_starter(self) -> None:
        try:
            self.starter.disconnect()
        except Exception:
            pass
        self._last_starter = VESCValues()

    def _disconnect_psu(self) -> None:
        try:
            self.psu.disconnect()
        except Exception:
            pass
        self._last_psu = {}

    def _force_all_off(self):
        self.pump_target = {"mode": "duty", "value": 0.0}
        self.starter_target = {"mode": "duty", "value": 0.0}
        self.psu_target = {"v": 0.0, "i": 0.0, "out": False}
        self._psu_dirty = True
        try:
            if self.pump.is_connected:
                self.pump.set_duty(0.0)
            if self.starter.is_connected:
                self.starter.set_duty(0.0)
            if self.psu.is_connected:
                self.psu.output(False)
        except Exception:
            pass

    def _state_time(self) -> float:
        return time.time() - self._state_t0

    def _tick_cyclogram(self, now: float):
        if not self.cycle_active:
            return

        st = self.cycle_state
        st_t = self._state_time()

        # Starter: wait for rpm>=500 in 10s
        if st == "Starter":
            # keep starter duty, pump off
            self.starter_target = {"mode": "duty", "value": STARTER_DUTY}
            self.pump_target = {"mode": "duty", "value": 0.0}
            self.psu_target = {"v": 0.0, "i": 0.0, "out": False}
            self._psu_dirty = True

            if self._last_starter.rpm_mech >= STARTER_MIN_RPM:
                self._enter_state("Ignition")
                return
            if st_t >= STARTER_WAIT_S:
                self._fault(f"Stop: Starter did not reach {STARTER_MIN_RPM:.0f} rpm in {STARTER_WAIT_S:.0f} s")
                return

        # Ignition
        if st == "Ignition":
            # safety timeout
            if st_t >= IGNITION_TIMEOUT_S:
                self._fault(f"Stop: Ignition timeout {IGNITION_TIMEOUT_S:.0f} s")
                return

            # starter duty profile
            duty = _interp_time_value(STARTER_IGNITION_DUTY_PROFILE, st_t)
            self.starter_target = {"mode": "duty", "value": duty}

            # valve V1->V2
            v = VALVE_V_1 if st_t < VALVE_SWITCH_S else VALVE_V_2
            self.psu_target = {"v": v, "i": VALVE_I, "out": True}
            self._psu_dirty = True

            # pump rpm from excel profile
            if self._pump_profile:
                pump_cmd = interp_profile(self._pump_profile, st_t)
                self.pump_target = {"mode": "rpm", "value": pump_cmd}
            else:
                self.pump_target = {"mode": "duty", "value": 0.0}

            # transition to Running: profile finished AND starter rpm >= 20000
            prof_end = self._pump_profile.end_time if self._pump_profile else 0.0
            if (st_t >= prof_end) and (self._last_starter.rpm_mech >= IGNITION_TARGET_STARTER_RPM):
                self._enter_state("Running")
                return

        # Running: keep valve on V2 + keep starter duty; pump is manual
        if st == "Running":
            self.psu_target = {"v": VALVE_V_2, "i": VALVE_I, "out": True}
            self._psu_dirty = True
            self.starter_target = {"mode": "duty", "value": RUNNING_STARTER_DUTY}
            # pump_target is NOT overridden here

        # Cooling: keep starter duty for duration, then stop all
        if st == "Cooling":
            self.psu_target = {"v": 0.0, "i": 0.0, "out": False}
            self._psu_dirty = True
            self.pump_target = {"mode": "duty", "value": 0.0}
            self.starter_target = {"mode": "duty", "value": self.cooling_duty}
            if st_t >= COOLING_DURATION_S:
                self.cycle_active = False
                self.stage = "done"
                self._force_all_off()
                return

    def _tick(self) -> None:
        if self._in_tick:
            return
        self._in_tick = True
        try:
            now = time.time()
            t = now - self._t0

            # --- read VESC
            pv = self._vesc_read(self.pump, self.pole_pairs_pump, label="pump")
            if pv is not None:
                self._last_pump = pv

            sv = self._vesc_read(self.starter, self.pole_pairs_starter, label="starter")
            if sv is not None:
                self._last_starter = sv

            # --- read PSU (2 Hz)
            if self.psu.is_connected and now >= self._psu_next_read:
                try:
                    self._last_psu = self.psu.read() or {}
                except Exception as e:
                    self.error.emit(f"PSU read error: {e}")
                    self._disconnect_psu()
                    self._emit_connected()
                self._psu_next_read = now + 0.5

            # --- cyclogram logic updates targets
            self._tick_cyclogram(now)

            # --- PSU apply (rate limit)
            if self.psu.is_connected and self._psu_dirty and now >= self._psu_next_cmd:
                try:
                    self.psu.set_vi(self.psu_target["v"], self.psu_target["i"])
                    self.psu.output(self.psu_target["out"])
                    self._psu_dirty = False
                    self._psu_next_cmd = now + 0.2
                except Exception as e:
                    self.error.emit(f"PSU cmd error: {e}")
                    self._disconnect_psu()
                    self._emit_connected()

            # --- send VESC targets + request
            self._vesc_send_and_request(self.pump, self.pump_target, self.pole_pairs_pump, label="pump")
            self._vesc_send_and_request(self.starter, self.starter_target, self.pole_pairs_starter, label="starter")

            # --- emit sample
            sample = {
                "t": t,
                "stage": self.stage,
                "connected": {
                    "pump": self.pump.is_connected,
                    "starter": self.starter.is_connected,
                    "psu": self.psu.is_connected,
                },
                "pump": {
                    "rpm_mech": self._last_pump.rpm_mech,
                    "duty": self._last_pump.duty,
                    "current_motor": self._last_pump.current_motor,
                    "v_in": self._last_pump.v_in,
                },
                "starter": {
                    "rpm_mech": self._last_starter.rpm_mech,
                    "duty": self._last_starter.duty,
                    "current_motor": self._last_starter.current_motor,
                    "v_in": self._last_starter.v_in,
                },
                "psu": self._last_psu,
            }

            # --- emit to UI at 5 Hz
            if now >= self._next_ui_emit:
                self.sample.emit(sample)
                self._next_ui_emit = now + self._ui_dt

            # --- CSV at 5 Hz
            if self.logging_on and self.logger.path and (now >= self._next_log_write):
                row = [
                    t, self.stage,
                    self._last_pump.rpm_mech, self._last_pump.duty, self._last_pump.current_motor,
                    self._last_starter.rpm_mech, self._last_starter.duty, self._last_starter.current_motor,
                    float(self._last_psu.get("v_set", 0.0)) if self._last_psu else 0.0,
                    float(self._last_psu.get("i_set", 0.0)) if self._last_psu else 0.0,
                    float(self._last_psu.get("v_out", 0.0)) if self._last_psu else 0.0,
                    float(self._last_psu.get("i_out", 0.0)) if self._last_psu else 0.0,
                    float(self._last_psu.get("p_out", 0.0)) if self._last_psu else 0.0,
                ]
                try:
                    self.logger.write_row(row)
                    self._next_log_write = now + self._log_dt
                    if now >= self._next_flush_t:
                        self.logger.flush()
                        self._next_flush_t = now + 1.0
                except Exception as e:
                    self.error.emit(f"CSV error: {e}")

        finally:
            self._in_tick = False

    def _vesc_send_and_request(self, dev: VESCDevice, target: Dict[str, Any], pp: int, label: str) -> None:
        if not dev.is_connected:
            return
        try:
            if target.get("mode") == "rpm":
                dev.set_rpm_mech(float(target.get("value", 0.0)), pp)
            else:
                dev.set_duty(float(target.get("value", 0.0)))
            dev.request_values()
        except (SerialException, OSError) as e:
            self.error.emit(f"{label} disconnected: {e}")
            if label == "pump":
                self._disconnect_pump()
            else:
                self._disconnect_starter()
            self._emit_connected()
        except Exception as e:
            self.error.emit(f"{label} error: {e}")

    def _vesc_read(self, dev: VESCDevice, pp: int, label: str) -> Optional[VESCValues]:
        if not dev.is_connected:
            return None
        try:
            return dev.read_values(pp, timeout_s=0.01)
        except (SerialException, OSError) as e:
            self.error.emit(f"{label} disconnected: {e}")
            if label == "pump":
                self._disconnect_pump()
            else:
                self._disconnect_starter()
            self._emit_connected()
            return None
        except Exception as e:
            self.error.emit(f"{label} read error: {e}")
            return None
