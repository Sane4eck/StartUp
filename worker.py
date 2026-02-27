# worker.py
from __future__ import annotations

import time
from typing import Any, Dict, Optional

import serial.tools.list_ports
from serial import SerialException

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot, QTimer

from devices_vesc import VESCDevice, VESCValues
from devices_psu_riden import RidenPSU
from logger_csv import CSVLogger

from cycle_fsm import CycleInputs
from cyclograms import build_startup_fsm, build_cooling_fsm, StartupCfg, CoolingCfg


class ControllerWorker(QObject):
    sample = pyqtSignal(object)
    status = pyqtSignal(object)
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

        self.pump = VESCDevice(timeout=0.01)
        self.starter = VESCDevice(timeout=0.01)
        self.psu = RidenPSU()

        self.pole_pairs_pump = 7
        self.pole_pairs_starter = 3

        # Manual targets (used when cycle not running)
        self.pump_target = {"mode": "duty", "value": 0.0}
        self.starter_target = {"mode": "duty", "value": 0.0}
        self.psu_target = {"v": 0.0, "i": 0.0, "out": False}
        self._psu_dirty = False

        self._last_pump = VESCValues()
        self._last_starter = VESCValues()
        self._last_psu: Dict[str, Any] = {}

        self._psu_next_read = 0.0
        self._psu_next_cmd = 0.0

        self.logger = CSVLogger()
        self.logging_on = False
        self._next_flush_t = 0.0

        self._in_tick = False

        # --- Cyclogram FSM
        self._fsm = None  # CycleFSM
        self._fsm_enter_t = 0.0

        # configs (edit later)
        self._startup_cfg = StartupCfg()
        self._cooling_cfg = CoolingCfg()

    @staticmethod
    def list_ports() -> list[str]:
        return [p.device for p in serial.tools.list_ports.comports()]

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
        try:
            if self.pump.is_connected:
                self.pump.set_duty(0.0)
            if self.starter.is_connected:
                self.starter.set_duty(0.0)
            if self.psu.is_connected:
                self.psu.output(False)
        except Exception:
            pass

        self._fsm = None
        self._disconnect_pump()
        self._disconnect_starter()
        self._disconnect_psu()

        try:
            self.logger.stop()
        except Exception:
            pass
        self.logging_on = False

        self._emit_connected()

    # -------- UI slots
    @pyqtSlot(str)
    def cmd_ready(self, prefix: str) -> None:
        self._t0 = time.time()
        self.stage = "ready"
        self._fsm = None

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
        self._fsm = None
        self._emit_connected()

    @pyqtSlot()
    def cmd_run_cycle(self) -> None:
        now = time.time()
        self._fsm = build_startup_fsm(self._startup_cfg)
        self._fsm_enter_t = now
        inp = self._make_inputs(now)
        self._fsm.start(inp)
        self.stage = self._fsm.state
        self._emit_connected()
        # force immediate PSU command allowed
        self._psu_next_cmd = 0.0

    @pyqtSlot()
    def cmd_cooling_cycle(self) -> None:
        now = time.time()
        self._fsm = build_cooling_fsm(self._cooling_cfg)
        self._fsm_enter_t = now
        inp = self._make_inputs(now)
        self._fsm.start(inp)
        self.stage = self._fsm.state
        self._emit_connected()
        self._psu_next_cmd = 0.0

    @pyqtSlot()
    def cmd_stop_all(self) -> None:
        self._fsm = None
        self.stage = "stop"

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

    # ---- manual targets (manual cancels cyclogram)
    @pyqtSlot(float)
    def cmd_set_pump_duty(self, duty: float) -> None:
        self._fsm = None
        self.pump_target = {"mode": "duty", "value": max(0.0, min(1.0, float(duty)))}

    @pyqtSlot(float)
    def cmd_set_pump_rpm(self, rpm: float) -> None:
        self._fsm = None
        self.pump_target = {"mode": "rpm", "value": float(rpm)}

    @pyqtSlot(float)
    def cmd_set_starter_duty(self, duty: float) -> None:
        self._fsm = None
        self.starter_target = {"mode": "duty", "value": max(0.0, min(1.0, float(duty)))}

    @pyqtSlot(float)
    def cmd_set_starter_rpm(self, rpm: float) -> None:
        self._fsm = None
        self.starter_target = {"mode": "rpm", "value": float(rpm)}

    # ---- PSU manual
    @pyqtSlot(float, float)
    def cmd_psu_set_vi(self, v: float, i: float) -> None:
        self._fsm = None
        self.psu_target["v"] = float(v)
        self.psu_target["i"] = float(i)
        self._psu_dirty = True
        self._psu_next_cmd = 0.0

    @pyqtSlot(bool)
    def cmd_psu_output(self, on: bool) -> None:
        self._fsm = None
        self.psu_target["out"] = bool(on)
        self._psu_dirty = True
        self._psu_next_cmd = 0.0

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

    def _make_inputs(self, now: float) -> CycleInputs:
        t = now - self._t0
        state_t = 0.0
        if self._fsm is not None:
            state_t = self._fsm.state_time(now)
        return CycleInputs(
            now=now,
            t=t,
            state_t=state_t,
            pump_rpm=float(self._last_pump.rpm_mech),
            starter_rpm=float(self._last_starter.rpm_mech),
            pump_current=float(self._last_pump.current_motor),
            starter_current=float(self._last_starter.current_motor),
            psu_v_out=float(self._last_psu.get("v_out", 0.0)) if self._last_psu else 0.0,
            psu_i_out=float(self._last_psu.get("i_out", 0.0)) if self._last_psu else 0.0,
            psu_output=bool(self._last_psu.get("output", False)) if self._last_psu else False,
        )

    def _apply_targets(self, pump_t: Dict[str, Any], starter_t: Dict[str, Any], psu_t: Dict[str, Any]):
        self.pump_target = pump_t
        self.starter_target = starter_t
        self.psu_target = psu_t
        self._psu_dirty = True
        self._psu_next_cmd = 0.0

    def _tick(self) -> None:
        if self._in_tick:
            return
        self._in_tick = True
        try:
            now = time.time()
            t = now - self._t0

            # ---- Read VESC (previous request)
            pv = self._vesc_read(self.pump, self.pole_pairs_pump, label="pump")
            if pv is not None:
                self._last_pump = pv
            sv = self._vesc_read(self.starter, self.pole_pairs_starter, label="starter")
            if sv is not None:
                self._last_starter = sv

            # ---- Read PSU (2Hz)
            if self.psu.is_connected and now >= self._psu_next_read:
                try:
                    self._last_psu = self.psu.read() or {}
                except Exception as e:
                    self.error.emit(f"PSU read error: {e}")
                    self._disconnect_psu()
                    self._emit_connected()
                self._psu_next_read = now + 0.5

            # ---- Cyclogram decision (FSM)
            if self._fsm is not None:
                inp = self._make_inputs(now)
                targets = self._fsm.tick(inp)
                self.stage = self._fsm.state
                self._apply_targets(targets.pump, targets.starter, targets.psu)
                if not self._fsm.running and self._fsm.state in ("Stop", "Fault"):
                    # finished/terminated
                    self._fsm = None

            # ---- Apply PSU commands (rate limit)
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

            # ---- Send VESC control + request new values (keep-alive)
            self._vesc_send_and_request(self.pump, self.pump_target, self.pole_pairs_pump, label="pump")
            self._vesc_send_and_request(self.starter, self.starter_target, self.pole_pairs_starter, label="starter")

            # ---- emit sample
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

            # ---- CSV
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
