# worker.py
from __future__ import annotations

import time
from typing import Optional, Dict, Any, List

import serial
import serial.tools.list_ports
from serial import SerialException

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot, QTimer

from devices_vesc import VESCDevice, VESCValues
from devices_psu_riden import RidenPSU
from logger_csv import CSVLogger
from cyclograms import START_CYCLE, COOLING_CYCLE


class ControllerWorker(QObject):
    sample = pyqtSignal(object)   # dict
    status = pyqtSignal(object)   # dict
    error = pyqtSignal(str)
    log = pyqtSignal(str)

    def __init__(self, dt: float = 0.05, parent=None):
        super().__init__(parent)

        self.dt = float(dt)
        self._timer = QTimer(self)
        self._timer.setInterval(max(10, int(self.dt * 1000)))
        self._timer.timeout.connect(self._on_tick)

        self._polling = False
        self._t0 = time.time()
        self.stage = "idle"

        # devices
        self.pump = VESCDevice()
        self.starter = VESCDevice()
        self.psu = RidenPSU()

        self.pole_pairs_pump = 3
        self.pole_pairs_starter = 3

        # targets (continuous send like visualVESC)
        self.pump_target = {"mode": "duty", "value": 0.0}
        self.starter_target = {"mode": "duty", "value": 0.0}

        self.psu_target = {"v": 0.0, "i": 0.0, "out": False}
        self._psu_cmd_dirty = False
        self._psu_last_read: Dict[str, Any] = {}
        self._psu_next_read_t = 0.0
        self._psu_next_cmd_t = 0.0

        # cyclogram
        self.cycle_active = False
        self.cycle: List = []
        self.cycle_idx = 0
        self._step_deadline = 0.0

        # logger
        self.logger = CSVLogger()
        self.logging_on = False
        self._next_flush_t = 0.0

        # last values
        self._last_pump = VESCValues()
        self._last_starter = VESCValues()

    # ---------- util
    @staticmethod
    def list_ports() -> List[str]:
        return [p.device for p in serial.tools.list_ports.comports()]

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

    def _safe_disconnect_pump(self, reason: str = ""):
        try:
            self.pump.disconnect()
        except Exception:
            pass
        self._last_pump = VESCValues()
        if reason:
            self.error.emit(f"Pump disconnected: {reason}")
        self._emit_connected()

    def _safe_disconnect_starter(self, reason: str = ""):
        try:
            self.starter.disconnect()
        except Exception:
            pass
        self._last_starter = VESCValues()
        if reason:
            self.error.emit(f"Starter disconnected: {reason}")
        self._emit_connected()

    def _safe_disconnect_psu(self, reason: str = ""):
        try:
            self.psu.disconnect()
        except Exception:
            pass
        self._psu_last_read = {}
        if reason:
            self.error.emit(f"PSU disconnected: {reason}")
        self._emit_connected()

    # ---------- lifecycle (called in worker thread)
    @pyqtSlot()
    def start(self) -> None:
        self._t0 = time.time()
        self.stage = "idle"
        self._emit_connected()
        self._timer.start()

    @pyqtSlot()
    def stop(self) -> None:
        # stop timer first
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

        # disconnect all
        self._safe_disconnect_pump()
        self._safe_disconnect_starter()
        self._safe_disconnect_psu()

        # close logger
        try:
            self.logger.stop()
        except Exception:
            pass
        self.logging_on = False

    # ---------- commands from UI
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
            path = self.logger.start(prefix=prefix or "session")
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
        self._psu_cmd_dirty = True
        self._emit_connected()

    # ----- connect/disconnect
    @pyqtSlot(str)
    def cmd_connect_pump(self, port: str) -> None:
        try:
            self.pump.connect(port)
            self.log.emit(f"Pump connected: {port}")
        except Exception as e:
            self._safe_disconnect_pump(str(e))
        self._emit_connected()

    @pyqtSlot()
    def cmd_disconnect_pump(self) -> None:
        self.pump_target = {"mode": "duty", "value": 0.0}
        self._safe_disconnect_pump()

    @pyqtSlot(str)
    def cmd_connect_starter(self, port: str) -> None:
        try:
            self.starter.connect(port)
            self.log.emit(f"Starter connected: {port}")
        except Exception as e:
            self._safe_disconnect_starter(str(e))
        self._emit_connected()

    @pyqtSlot()
    def cmd_disconnect_starter(self) -> None:
        self.starter_target = {"mode": "duty", "value": 0.0}
        self._safe_disconnect_starter()

    @pyqtSlot(str)
    def cmd_connect_psu(self, port: str) -> None:
        try:
            self.psu.connect(port)
            self.log.emit(f"PSU connected: {port}")
        except Exception as e:
            self._safe_disconnect_psu(str(e))
        self._emit_connected()

    @pyqtSlot()
    def cmd_disconnect_psu(self) -> None:
        self.psu_target = {"v": 0.0, "i": 0.0, "out": False}
        self._psu_cmd_dirty = True
        self._safe_disconnect_psu()

    # ----- parameters
    @pyqtSlot(int)
    def cmd_set_pole_pairs_pump(self, pp: int) -> None:
        self.pole_pairs_pump = max(1, int(pp))

    @pyqtSlot(int)
    def cmd_set_pole_pairs_starter(self, pp: int) -> None:
        self.pole_pairs_starter = max(1, int(pp))

    # ----- manual control targets
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

    # ----- PSU control
    @pyqtSlot(float, float)
    def cmd_psu_set_vi(self, v: float, i: float) -> None:
        self.psu_target["v"] = float(v)
        self.psu_target["i"] = float(i)
        self._psu_cmd_dirty = True

    @pyqtSlot(bool)
    def cmd_psu_output(self, on: bool) -> None:
        self.psu_target["out"] = bool(on)
        self._psu_cmd_dirty = True

    # ---------- cyclogram helper
    def _apply_cycle_step(self, step) -> None:
        duration_s, stage, pump_cmd, starter_cmd, psu_cmd = step
        self.stage = stage

        # set targets (continuous)
        self.pump_target = {"mode": pump_cmd.get("mode", "duty"), "value": pump_cmd.get("value", 0.0)}
        self.starter_target = {"mode": starter_cmd.get("mode", "duty"), "value": starter_cmd.get("value", 0.0)}

        self.psu_target = {
            "v": float(psu_cmd.get("v", 0.0)),
            "i": float(psu_cmd.get("i", 0.0)),
            "out": bool(psu_cmd.get("out", False)),
        }
        self._psu_cmd_dirty = True

        self._step_deadline = time.time() + float(duration_s)
        self._emit_connected()

    # ---------- tick
    def _on_tick(self) -> None:
        if self._polling:
            return
        self._polling = True
        try:
            now = time.time()
            t = now - self._t0

            # cycle advance
            if self.cycle_active and self.cycle:
                if now >= self._step_deadline:
                    self.cycle_idx += 1
                    if self.cycle_idx >= len(self.cycle):
                        self.cycle_active = False
                        self.stage = "done"
                        self.pump_target = {"mode": "duty", "value": 0.0}
                        self.starter_target = {"mode": "duty", "value": 0.0}
                        self.psu_target = {"v": 0.0, "i": 0.0, "out": False}
                        self._psu_cmd_dirty = True
                        self._emit_connected()
                    else:
                        self._apply_cycle_step(self.cycle[self.cycle_idx])

            # apply VESC targets (always, like watchdog keep-alive)
            self._apply_vesc_targets()

            # apply PSU commands (rate limited)
            self._apply_psu_commands(now)

            # read devices
            pump_vals = self._read_vesc(self.pump, self.pole_pairs_pump, timeout_s=0.04, label="Pump")
            if pump_vals is not None:
                self._last_pump = pump_vals

            starter_vals = self._read_vesc(self.starter, self.pole_pairs_starter, timeout_s=0.04, label="Starter")
            if starter_vals is not None:
                self._last_starter = starter_vals

            self._read_psu(now)

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
                "psu": self._psu_last_read or {},
            }

            # log
            if self.logging_on and self.logger.path:
                row = [
                    t, self.stage,
                    self._last_pump.rpm_mech, self._last_pump.duty, self._last_pump.current_motor,
                    self._last_starter.rpm_mech, self._last_starter.duty, self._last_starter.current_motor,
                    float(self._psu_last_read.get("v_set", 0.0)) if self._psu_last_read else 0.0,
                    float(self._psu_last_read.get("i_set", 0.0)) if self._psu_last_read else 0.0,
                    float(self._psu_last_read.get("v_out", 0.0)) if self._psu_last_read else 0.0,
                    float(self._psu_last_read.get("i_out", 0.0)) if self._psu_last_read else 0.0,
                    float(self._psu_last_read.get("p_out", 0.0)) if self._psu_last_read else 0.0,
                ]
                try:
                    self.logger.write_row(row)
                    if now >= self._next_flush_t:
                        self.logger.flush()
                        self._next_flush_t = now + 1.0
                except Exception as e:
                    self.error.emit(f"CSV write failed: {e}")

            self.sample.emit(sample)

        finally:
            self._polling = False

    def _apply_vesc_targets(self) -> None:
        # Pump
        if self.pump.is_connected:
            try:
                if self.pump_target["mode"] == "rpm":
                    self.pump.set_rpm_mech(self.pump_target["value"], self.pole_pairs_pump)
                    self.pump.request_values()
                else:
                    self.pump.set_duty(self.pump_target["value"])
                    self.pump.request_values()
            except (SerialException, OSError) as e:
                self._safe_disconnect_pump(str(e))
            except Exception as e:
                # any unexpected error -> disconnect to be safe
                self._safe_disconnect_pump(str(e))

        # Starter
        if self.starter.is_connected:
            try:
                if self.starter_target["mode"] == "rpm":
                    self.starter.set_rpm_mech(self.starter_target["value"], self.pole_pairs_starter)
                    self.starter.request_values()
                else:
                    self.starter.set_duty(self.starter_target["value"])
                    self.starter.request_values()
            except (SerialException, OSError) as e:
                self._safe_disconnect_starter(str(e))
            except Exception as e:
                self._safe_disconnect_starter(str(e))

    def _read_vesc(self, dev: VESCDevice, pp: int, timeout_s: float, label: str) -> Optional[VESCValues]:
        if not dev.is_connected:
            return None
        try:
            v = dev.read_values(pp, timeout_s=timeout_s)
            return v
        except (SerialException, OSError) as e:
            if label == "Pump":
                self._safe_disconnect_pump(str(e))
            else:
                self._safe_disconnect_starter(str(e))
            return None
        except Exception as e:
            if label == "Pump":
                self._safe_disconnect_pump(str(e))
            else:
                self._safe_disconnect_starter(str(e))
            return None

    def _apply_psu_commands(self, now: float) -> None:
        if not self.psu.is_connected:
            return
        if not self._psu_cmd_dirty:
            return
        if now < self._psu_next_cmd_t:
            return
        try:
            self.psu.set_vi(self.psu_target["v"], self.psu_target["i"])
            self.psu.output(self.psu_target["out"])
            self._psu_cmd_dirty = False
            self._psu_next_cmd_t = now + 0.2
        except Exception as e:
            self._safe_disconnect_psu(str(e))

    def _read_psu(self, now: float) -> None:
        if not self.psu.is_connected:
            self._psu_last_read = {}
            return
        if now < self._psu_next_read_t:
            return
        try:
            r = self.psu.read()
            self._psu_last_read = r or {}
            self._psu_next_read_t = now + 0.5
        except Exception as e:
            self._safe_disconnect_psu(str(e))
            self._psu_last_read = {}
