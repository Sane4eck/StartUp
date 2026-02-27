# worker.py
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import serial
import serial.tools.list_ports
from serial import SerialException

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot, QTimer

from devices_vesc import VESCDevice, VESCValues
from devices_psu_riden import RidenPSU
from logger_csv import CSVLogger
from cyclograms import START_CYCLE, COOLING_CYCLE


class ControllerWorker(QObject):
    # data to UI
    sample = pyqtSignal(object)   # dict
    status = pyqtSignal(object)   # dict
    error = pyqtSignal(str)
    log = pyqtSignal(str)

    def __init__(self, dt: float = 0.05, parent=None):
        super().__init__(parent)

        self.dt = float(dt)
        self._timer = QTimer(self)
        self._timer.setInterval(max(10, int(self.dt * 1000)))
        self._timer.timeout.connect(self._tick)

        self._t0 = time.time()
        self.stage = "idle"

        # devices (I/O must happen ONLY in this thread)
        self.pump = VESCDevice(timeout=0.01)
        self.starter = VESCDevice(timeout=0.01)
        self.psu = RidenPSU()

        self.pole_pairs_pump = 3
        self.pole_pairs_starter = 3

        # continuous targets (keep-alive like VESC watchdog)
        self.pump_target = {"mode": "duty", "value": 0.0}
        self.starter_target = {"mode": "duty", "value": 0.0}
        self.psu_target = {"v": 0.0, "i": 0.0, "out": False}
        self._psu_dirty = False

        # last values (to keep plot stable even if a read missed)
        self._last_pump = VESCValues()
        self._last_starter = VESCValues()
        self._last_psu: Dict[str, Any] = {}

        # rate limiting PSU
        self._psu_next_read = 0.0
        self._psu_next_cmd = 0.0

        # cyclogram
        self.cycle_active = False
        self.cycle: List = []
        self.cycle_idx = 0
        self._step_end_t = 0.0

        # logger
        self.logger = CSVLogger()
        self.logging_on = False
        self._next_flush_t = 0.0

        self._in_tick = False

    # -------- ports (safe to call from UI thread too)
    @staticmethod
    def list_ports() -> list[str]:
        return [p.device for p in serial.tools.list_ports.comports()]

    # -------- lifecycle (called by UI when thread starts / on close)
    @pyqtSlot()
    def start(self) -> None:
        self._t0 = time.time()
        self.stage = "idle"
        self._emit_connected()
        self._timer.start()

    @pyqtSlot()
    def stop(self) -> None:
        # stop polling first
        try:
            self._timer.stop()
        except Exception:
            pass

        # safe outputs
        try:
            if self.pump.is_connected:
                try:
                    self.pump.set_duty(0.0)
                except Exception:
                    pass
            if self.starter.is_connected:
                try:
                    self.starter.set_duty(0.0)
                except Exception:
                    pass
            if self.psu.is_connected:
                try:
                    self.psu.output(False)
                except Exception:
                    pass
        except Exception:
            pass

        # disconnect
        self._disconnect_pump()
        self._disconnect_starter()
        self._disconnect_psu()

        # logger
        try:
            self.logger.stop()
        except Exception:
            pass
        self.logging_on = False

        self._emit_connected()

    # -------- UI commands (SLOTS)
    @pyqtSlot(str)
    def cmd_ready(self, prefix: str) -> None:
        self._t0 = time.time()
        self.stage = "ready"
        self.cycle_active = False
        self.cycle = []
        self.cycle_idx = 0

        try:
            self.logger.stop()
        except Exception:
            pass

        try:
            path = self.logger.start(prefix=(prefix or "session"))
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
        self.cycle_active = False
        self.cycle = []
        self.cycle_idx = 0
        self._emit_connected()

    @pyqtSlot()
    def cmd_run_cycle(self) -> None:
        self.cycle = list(START_CYCLE)
        if not self.cycle:
            return
        self.cycle_active = True
        self.cycle_idx = 0
        self._apply_cycle_step(self.cycle[0])

    @pyqtSlot()
    def cmd_cooling_cycle(self) -> None:
        self.cycle = list(COOLING_CYCLE)
        if not self.cycle:
            return
        self.cycle_active = True
        self.cycle_idx = 0
        self._apply_cycle_step(self.cycle[0])

    @pyqtSlot()
    def cmd_stop_all(self) -> None:
        self.cycle_active = False
        self.stage = "stop"
        self.pump_target = {"mode": "duty", "value": 0.0}
        self.starter_target = {"mode": "duty", "value": 0.0}
        self.psu_target = {"v": 0.0, "i": 0.0, "out": False}
        self._psu_dirty = True
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
        self.cycle_active = False
        self.pump_target = {"mode": "duty", "value": max(0.0, min(1.0, float(duty)))}

    @pyqtSlot(float)
    def cmd_set_pump_rpm(self, rpm: float) -> None:
        self.cycle_active = False
        self.pump_target = {"mode": "rpm", "value": float(rpm)}

    @pyqtSlot(float)
    def cmd_set_starter_duty(self, duty: float) -> None:
        self.cycle_active = False
        self.starter_target = {"mode": "duty", "value": max(0.0, min(1.0, float(duty)))}

    @pyqtSlot(float)
    def cmd_set_starter_rpm(self, rpm: float) -> None:
        self.cycle_active = False
        self.starter_target = {"mode": "rpm", "value": float(rpm)}

    # ---- PSU
    @pyqtSlot(float, float)
    def cmd_psu_set_vi(self, v: float, i: float) -> None:
        self.psu_target["v"] = float(v)
        self.psu_target["i"] = float(i)
        self._psu_dirty = True

    @pyqtSlot(bool)
    def cmd_psu_output(self, on: bool) -> None:
        self.psu_target["out"] = bool(on)
        self._psu_dirty = True

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

    def _apply_cycle_step(self, step) -> None:
        duration_s, stage, pump_cmd, starter_cmd, psu_cmd = step
        self.stage = stage

        self.pump_target = {"mode": pump_cmd.get("mode", "duty"), "value": pump_cmd.get("value", 0.0)}
        self.starter_target = {"mode": starter_cmd.get("mode", "duty"), "value": starter_cmd.get("value", 0.0)}

        self.psu_target = {
            "v": float(psu_cmd.get("v", 0.0)),
            "i": float(psu_cmd.get("i", 0.0)),
            "out": bool(psu_cmd.get("out", False)),
        }
        self._psu_dirty = True

        self._step_end_t = time.time() + float(duration_s)
        self._emit_connected()

    def _tick(self) -> None:
        if self._in_tick:
            return
        self._in_tick = True
        try:
            now = time.time()
            t = now - self._t0

            # cycle switch
            if self.cycle_active and self.cycle and now >= self._step_end_t:
                self.cycle_idx += 1
                if self.cycle_idx >= len(self.cycle):
                    self.cycle_active = False
                    self.stage = "done"
                    self.pump_target = {"mode": "duty", "value": 0.0}
                    self.starter_target = {"mode": "duty", "value": 0.0}
                    self.psu_target = {"v": 0.0, "i": 0.0, "out": False}
                    self._psu_dirty = True
                    self._emit_connected()
                else:
                    self._apply_cycle_step(self.cycle[self.cycle_idx])

            # VESC keep-alive + request
            self._vesc_send_and_request(self.pump, self.pump_target, self.pole_pairs_pump, label="pump")
            self._vesc_send_and_request(self.starter, self.starter_target, self.pole_pairs_starter, label="starter")

            # VESC read
            pv = self._vesc_read(self.pump, self.pole_pairs_pump, label="pump")
            if pv is not None:
                self._last_pump = pv
            sv = self._vesc_read(self.starter, self.pole_pairs_starter, label="starter")
            if sv is not None:
                self._last_starter = sv

            # PSU command (rate limit)
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

            # PSU read (2 Hz)
            if self.psu.is_connected and now >= self._psu_next_read:
                try:
                    self._last_psu = self.psu.read() or {}
                except Exception as e:
                    self.error.emit(f"PSU read error: {e}")
                    self._disconnect_psu()
                    self._emit_connected()
                self._psu_next_read = now + 0.5

            # emit sample
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
            self.sample.emit(sample)

            # CSV
            if self.logging_on and self.logger.path:
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
