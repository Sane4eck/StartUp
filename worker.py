# worker.py
from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

import serial.tools.list_ports
from serial import SerialException
from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot, QTimer

from devices_vesc import VESCDevice, VESCValues
from devices_psu_riden import RidenPSU
from logger_csv import CSVLogger

from pump_profile import load_pump_profile_xlsx, interp_profile, PumpProfile

from cycle_fsm import CycleInputs, CycleFSM
from cyclogram_startup import build_startup_fsm, StartupConfig, build_cooling_fsm


# Run-cycle profiles:
PUMP_PROFILE_XLSX = "_Cyclogram_Pump.xlsx"
STARTER_PROFILE_XLSX = "_Cyclogram_Starter.xlsx"


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


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

        # Manual targets: pump & starter support BOTH rpm and duty
        self.pump_target = {"mode": "rpm", "value": 0.0}       # "rpm" | "duty"
        self.starter_target = {"mode": "duty", "value": 0.0}   # "rpm" | "duty"

        self.psu_target = {"v": 0.0, "i": 0.0, "out": False}
        self._psu_dirty = False
        self._psu_applied = {"v": None, "i": None, "out": None}

        self._last_pump = VESCValues()
        self._last_starter = VESCValues()
        self._last_psu: Dict[str, Any] = {}

        self._psu_next_read = 0.0
        self._psu_next_cmd = 0.0

        self.logger = CSVLogger()
        self.logging_on = False
        self._next_flush_t = 0.0

        self._in_tick = False

        # UI/log throttling (5 Hz)
        self.ui_hz = 5.0
        self.log_hz = 5.0
        self._ui_dt = 1.0 / self.ui_hz
        self._log_dt = 1.0 / self.log_hz
        self._next_ui_emit = 0.0
        self._next_log_write = 0.0

        # Run/Cooling cyclogram FSM
        self._fsm: Optional[CycleFSM] = None
        self._fsm_prev_state: Optional[str] = None
        self.startup_cfg = StartupConfig()

        # Run-cycle profiles cache
        self._pump_profile: Optional[PumpProfile] = None
        self._pump_profile_mtime: Optional[float] = None

        self._starter_profile: Optional[PumpProfile] = None
        self._starter_profile_mtime: Optional[float] = None

        # Manual pump profile runner (Manual tab) — без Loop
        self._pump_prof_active: bool = False
        self._pump_prof_path: str = ""
        self._pump_prof_mtime: Optional[float] = None
        self._pump_prof: Optional[PumpProfile] = None
        self._pump_prof_t0: float = 0.0
        self._pump_prof_prev_stage: str = "idle"

    @staticmethod
    def list_ports() -> list[str]:
        return [p.device for p in serial.tools.list_ports.comports()]

    # ---------------- lifecycle
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

        self._fsm = None
        self._stop_pump_profile_internal()
        self.stage = "stop"
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

    # ---------------- UI commands
    @pyqtSlot(str)
    def cmd_ready(self, prefix: str) -> None:
        self._t0 = time.time()
        self.stage = "ready"
        self._fsm = None
        self._stop_pump_profile_internal()

        try:
            self.logger.stop()
        except Exception:
            pass

        try:
            path = self.logger.start(prefix=(prefix or "session"))
            self.logging_on = True
            now = time.time()
            self._next_flush_t = now + 1.0
            self._next_ui_emit = now
            self._next_log_write = now
            self.status.emit({"ready": True, "log_path": path})
        except Exception as e:
            self.logging_on = False
            self.error.emit(f"Logger start failed: {e}")

        # warm-up both profiles so Run is instant
        self._ensure_run_profiles()

        self._emit_connected()

    @pyqtSlot()
    def cmd_update_reset(self) -> None:
        self._t0 = time.time()
        self.stage = "idle"
        self._fsm = None
        self._stop_pump_profile_internal()
        self._emit_connected()

    @pyqtSlot()
    def cmd_run_cycle(self) -> None:
        self._stop_pump_profile_internal()

        if not self._ensure_run_profiles():
            return

        now = time.time()
        inp = self._make_inputs(now)

        # FuelRamp uses two profiles:
        # pump_profile: RPM, starter_profile: DUTY
        self._fsm = build_startup_fsm(self._pump_profile, self._starter_profile, self.startup_cfg)
        self._fsm_prev_state = None
        self._fsm.start(inp)
        self.stage = self._fsm.state
        self._emit_connected()

    @pyqtSlot(float)
    def cmd_cooling_cycle(self, duty: float) -> None:
        self._stop_pump_profile_internal()

        now = time.time()
        inp = self._make_inputs(now)

        self._fsm = build_cooling_fsm(duty)
        self._fsm_prev_state = None
        self._fsm.start(inp)
        self.stage = self._fsm.state
        self._emit_connected()

    @pyqtSlot()
    def cmd_stop_all(self) -> None:
        self._fsm = None
        self._stop_pump_profile_internal()
        self.stage = "stop"
        self._force_all_off()
        self._emit_connected()

    # ---------------- Manual pump profile (Manual tab)
    @pyqtSlot(str)
    def cmd_start_pump_profile(self, path: str) -> None:
        path = (path or "").strip()
        if not path:
            self.error.emit("Pump profile: empty file path")
            return
        if not os.path.exists(path):
            self.error.emit(f"Pump profile: file not found: {path}")
            return

        self._fsm = None

        try:
            mtime = os.path.getmtime(path)
            if (self._pump_prof is None) or (self._pump_prof_path != path) or (self._pump_prof_mtime != mtime):
                prof = load_pump_profile_xlsx(path, sheet_name=None)
                if not prof.t:
                    raise RuntimeError("profile is empty")
                self._pump_prof = prof
                self._pump_prof_path = path
                self._pump_prof_mtime = mtime
        except Exception as e:
            self._pump_prof = None
            self._pump_prof_path = ""
            self._pump_prof_mtime = None
            self.error.emit(f"Pump profile load error: {e}")
            return

        self._pump_prof_prev_stage = self.stage
        self.stage = "PumpProfile"
        self._pump_prof_active = True
        self._pump_prof_t0 = time.time()
        self._emit_connected()

    @pyqtSlot()
    def cmd_stop_pump_profile(self) -> None:
        self._stop_pump_profile_internal()

    def _stop_pump_profile_internal(self) -> None:
        if not self._pump_prof_active:
            return
        self._pump_prof_active = False
        self._pump_prof_t0 = 0.0
        self.stage = self._pump_prof_prev_stage if self._pump_prof_prev_stage else "idle"
        self._emit_connected()

    # ---------------- connect/disconnect
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
        self._stop_pump_profile_internal()
        self.pump_target = {"mode": "rpm", "value": 0.0}
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
        self._stop_pump_profile_internal()
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
        self._stop_pump_profile_internal()
        self._set_psu_target(0.0, 0.0, False)
        self._disconnect_psu()
        self._emit_connected()

    # ---------------- params
    @pyqtSlot(int)
    def cmd_set_pole_pairs_pump(self, pp: int) -> None:
        self.pole_pairs_pump = max(1, int(pp))

    @pyqtSlot(int)
    def cmd_set_pole_pairs_starter(self, pp: int) -> None:
        self.pole_pairs_starter = max(1, int(pp))

    # ---------------- manual control
    @pyqtSlot(float)
    def cmd_set_pump_rpm(self, rpm: float) -> None:
        self._stop_pump_profile_internal()
        if self._fsm is not None and self._fsm.state == "Running":
            self.pump_target = {"mode": "rpm", "value": float(rpm)}
            return
        self._fsm = None
        self.pump_target = {"mode": "rpm", "value": float(rpm)}

    @pyqtSlot(float)
    def cmd_set_pump_duty(self, duty: float) -> None:
        self._stop_pump_profile_internal()
        if self._fsm is not None and self._fsm.state == "Running":
            self.pump_target = {"mode": "duty", "value": _clamp01(duty)}
            return
        self._fsm = None
        self.pump_target = {"mode": "duty", "value": _clamp01(duty)}

    @pyqtSlot(float)
    def cmd_set_starter_duty(self, duty: float) -> None:
        self._stop_pump_profile_internal()
        self._fsm = None
        self.starter_target = {"mode": "duty", "value": _clamp01(duty)}

    @pyqtSlot(float)
    def cmd_set_starter_rpm(self, rpm: float) -> None:
        self._stop_pump_profile_internal()
        self._fsm = None
        self.starter_target = {"mode": "rpm", "value": float(rpm)}

    # ---------------- PSU manual
    @pyqtSlot(float, float)
    def cmd_psu_set_vi(self, v: float, i: float) -> None:
        self._stop_pump_profile_internal()
        self._fsm = None
        self._set_psu_target(float(v), float(i), bool(self.psu_target.get("out", False)))

    @pyqtSlot(bool)
    def cmd_psu_output(self, on: bool) -> None:
        self._stop_pump_profile_internal()
        self._fsm = None
        self._set_psu_target(float(self.psu_target.get("v", 0.0)), float(self.psu_target.get("i", 0.0)), bool(on))

    # ---------------- helpers
    def _emit_connected(self) -> None:
        self.status.emit({
            "connected": {
                "pump": self.pump.is_connected,
                "starter": self.starter.is_connected,
                "psu": self.psu.is_connected,
            },
            "stage": self.stage,
            "log_path": self.logger.path,
            "pump_profile": {
                "active": self._pump_prof_active,
                "path": self._pump_prof_path,
            }
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
        self.pump_target = {"mode": "rpm", "value": 0.0}
        self.starter_target = {"mode": "duty", "value": 0.0}
        self._set_psu_target(0.0, 0.0, False)
        try:
            if self.pump.is_connected:
                self.pump.set_rpm_mech(0.0, self.pole_pairs_pump)
            if self.starter.is_connected:
                self.starter.set_duty(0.0)
            if self.psu.is_connected:
                self.psu.output(False)
        except Exception:
            pass

    def _set_psu_target(self, v: float, i: float, out: bool):
        v = float(v); i = float(i); out = bool(out)
        if self.psu_target.get("v") == v and self.psu_target.get("i") == i and self.psu_target.get("out") == out:
            return
        self.psu_target = {"v": v, "i": i, "out": out}
        self._psu_dirty = True
        self._psu_next_cmd = 0.0

    def _ensure_run_profiles(self) -> bool:
        # load pump profile
        try:
            base = os.path.dirname(os.path.abspath(__file__))

            p_path = os.path.join(base, PUMP_PROFILE_XLSX)
            p_mtime = os.path.getmtime(p_path)
            if (self._pump_profile is None) or (self._pump_profile_mtime != p_mtime):
                self._pump_profile = load_pump_profile_xlsx(p_path, sheet_name=None)
                self._pump_profile_mtime = p_mtime
            if not (self._pump_profile and self._pump_profile.t):
                raise RuntimeError("pump profile empty")

            s_path = os.path.join(base, STARTER_PROFILE_XLSX)
            s_mtime = os.path.getmtime(s_path)
            if (self._starter_profile is None) or (self._starter_profile_mtime != s_mtime):
                self._starter_profile = load_pump_profile_xlsx(s_path, sheet_name=None)
                self._starter_profile_mtime = s_mtime
            if not (self._starter_profile and self._starter_profile.t):
                raise RuntimeError("starter profile empty")

            return True

        except Exception as e:
            self.error.emit(f"Cannot load run profiles: {e}")
            return False

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

    # ---------------- tick
    def _tick(self) -> None:
        if self._in_tick:
            return
        self._in_tick = True
        try:
            now = time.time()
            t = now - self._t0

            # read vesc
            pv = self._vesc_read(self.pump, self.pole_pairs_pump, label="pump")
            if pv is not None:
                self._last_pump = pv
            sv = self._vesc_read(self.starter, self.pole_pairs_starter, label="starter")
            if sv is not None:
                self._last_starter = sv

            # read psu (2 Hz)
            if self.psu.is_connected and now >= self._psu_next_read:
                try:
                    self._last_psu = self.psu.read() or {}
                except Exception as e:
                    self.error.emit(f"PSU read error: {e}")
                    self._disconnect_psu()
                    self._emit_connected()
                self._psu_next_read = now + 0.5

            # Manual pump profile takes control ONLY if no FSM
            if self._fsm is None and self._pump_prof_active and self._pump_prof is not None:
                elapsed = now - self._pump_prof_t0
                end_t = self._pump_prof.end_time
                if end_t > 0.0 and elapsed >= end_t:
                    self._stop_pump_profile_internal()
                    self.pump_target = {"mode": "rpm", "value": 0.0}
                if self._pump_prof_active:
                    rpm_cmd = interp_profile(self._pump_prof, elapsed)
                    self.pump_target = {"mode": "rpm", "value": float(rpm_cmd)}
                    self.stage = "PumpProfile"

            # Run/Cooling cyclogram
            if self._fsm is not None:
                inp = self._make_inputs(now)
                targets = self._fsm.tick(inp)
                self.stage = self._fsm.state

                if self._fsm.state != self._fsm_prev_state:
                    self._fsm_prev_state = self._fsm.state
                    reason = targets.meta.get("transition_reason")
                    if self._fsm.state == "Fault" and reason:
                        self.error.emit(reason)

                # In Running: pump is manual (do NOT overwrite)
                if self._fsm.state != "Running":
                    self.pump_target = targets.pump
                self.starter_target = targets.starter
                self._set_psu_target(targets.psu["v"], targets.psu["i"], targets.psu["out"])

                # NOTE: Running is infinite, so FSM will not auto-stop.
                # It stops only when user presses Stop (cmd_stop_all) or manual overrides cancel FSM.

            # PSU apply only changed
            if self.psu.is_connected and self._psu_dirty and now >= self._psu_next_cmd:
                try:
                    v = self.psu_target["v"]
                    i = self.psu_target["i"]
                    out = self.psu_target["out"]

                    if self._psu_applied["v"] != v or self._psu_applied["i"] != i:
                        self.psu.set_vi(v, i)
                        self._psu_applied["v"] = v
                        self._psu_applied["i"] = i

                    if self._psu_applied["out"] != out:
                        self.psu.output(out)
                        self._psu_applied["out"] = out

                    self._psu_dirty = False
                    self._psu_next_cmd = now + 0.2
                except Exception as e:
                    self.error.emit(f"PSU cmd error: {e}")
                    self._disconnect_psu()
                    self._emit_connected()

            # send targets + request values
            self._vesc_send_and_request(self.pump, self.pump_target, self.pole_pairs_pump, "pump")
            self._vesc_send_and_request(self.starter, self.starter_target, self.pole_pairs_starter, "starter")

            # sample
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
                },
                "starter": {
                    "rpm_mech": self._last_starter.rpm_mech,
                    "duty": self._last_starter.duty,
                    "current_motor": self._last_starter.current_motor,
                },
                "psu": self._last_psu,
            }

            # emit 5 Hz
            if now >= self._next_ui_emit:
                self.sample.emit(sample)
                self._next_ui_emit = now + self._ui_dt

            # CSV 5 Hz
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
            mode = str(target.get("mode", "duty"))
            val = float(target.get("value", 0.0))
            if mode == "rpm":
                dev.set_rpm_mech(val, pp)
            else:
                dev.set_duty(_clamp01(val))
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
