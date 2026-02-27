# worker.py
from __future__ import annotations

import time
import threading
import serial.tools.list_ports
from PyQt5.QtCore import QObject, pyqtSignal, QThread

from devices_vesc import VESCDevice
from devices_psu_riden import RidenPSU
from logger_csv import CSVLogger
from cyclograms import START_CYCLE, COOLING_CYCLE


class ControllerWorker(QObject):
    sample = pyqtSignal(object)          # dict
    status = pyqtSignal(object)          # dict

    def __init__(self, dt: float = 0.1):
        super().__init__()
        self.dt = float(dt)
        self.lock = threading.Lock()

        self.pump = VESCDevice()
        self.starter = VESCDevice()
        self.psu = RidenPSU()

        self.pole_pairs_pump = 3
        self.pole_pairs_starter = 3

        self._running = False
        self._t0 = time.time()

        self.logger = CSVLogger()
        self.logging_on = False
        self.stage = "idle"

        # cyclogram state
        self.cycle_active = False
        self.cycle = []
        self.cycle_idx = 0
        self.step_t0 = 0.0

        self._thread = QThread()
        self.moveToThread(self._thread)
        self._thread.started.connect(self._loop)
        self._thread.start()

    # -------- ports
    @staticmethod
    def list_ports() -> list[str]:
        return [p.device for p in serial.tools.list_ports.comports()]

    # -------- connect/disconnect
    def connect_pump(self, port: str) -> None:
        with self.lock:
            self.pump.connect(port)

    def connect_starter(self, port: str) -> None:
        with self.lock:
            self.starter.connect(port)

    def connect_psu(self, port: str) -> None:
        with self.lock:
            self.psu.connect(port)

    def disconnect_all(self) -> None:
        with self.lock:
            self.pump.disconnect()
            self.starter.disconnect()
            self.psu.disconnect()

    # -------- manual commands
    def set_pump_duty(self, duty: float) -> None:
        with self.lock:
            self.pump.set_duty(duty)

    def set_starter_duty(self, duty: float) -> None:
        with self.lock:
            self.starter.set_duty(duty)

    def set_pump_rpm(self, rpm: float) -> None:
        with self.lock:
            self.pump.set_rpm_mech(rpm, self.pole_pairs_pump)

    def set_starter_rpm(self, rpm: float) -> None:
        with self.lock:
            self.starter.set_rpm_mech(rpm, self.pole_pairs_starter)

    def psu_set_vi(self, v: float, i: float) -> None:
        with self.lock:
            self.psu.set_vi(v, i)

    def psu_output(self, on: bool) -> None:
        with self.lock:
            self.psu.output(on)

    # -------- session buttons
    def ready(self, log_prefix: str = "session") -> None:
        with self.lock:
            self._t0 = time.time()
            self.stage = "ready"
            self.cycle_active = False
            self.cycle = []
            self.cycle_idx = 0

            self.logger.stop()
            path = self.logger.start(prefix=log_prefix)
            self.logging_on = True

        self.status.emit({"ready": True, "log_path": path})

    def update_reset(self) -> None:
        with self.lock:
            self._t0 = time.time()
            self.stage = "idle"
            self.cycle_active = False
            self.cycle = []
            self.cycle_idx = 0
            # графік чиститься в UI

    def run_cycle(self) -> None:
        with self.lock:
            self.cycle = list(START_CYCLE)
            self.cycle_active = True
            self.cycle_idx = 0
            self.step_t0 = time.time()
            self._apply_step(self.cycle[0])

    def cooling_cycle(self) -> None:
        with self.lock:
            self.cycle = list(COOLING_CYCLE)
            self.cycle_active = True
            self.cycle_idx = 0
            self.step_t0 = time.time()
            self._apply_step(self.cycle[0])

    def stop_all(self) -> None:
        with self.lock:
            self.pump.set_duty(0.0)
            self.starter.set_duty(0.0)
            self.psu.output(False)
            self.stage = "stop"
            self.cycle_active = False

    # -------- internals
    def _apply_step(self, step) -> None:
        duration_s, stage, pump_cmd, starter_cmd, psu_cmd = step
        self.stage = stage

        # PSU
        if self.psu.is_connected:
            self.psu.set_vi(psu_cmd.get("v", 0.0), psu_cmd.get("i", 0.0))
            self.psu.output(bool(psu_cmd.get("out", False)))

        # Pump
        if self.pump.is_connected:
            if pump_cmd.get("mode") == "rpm":
                self.pump.set_rpm_mech(pump_cmd.get("value", 0.0), self.pole_pairs_pump)
            else:
                self.pump.set_duty(pump_cmd.get("value", 0.0))

        # Starter
        if self.starter.is_connected:
            if starter_cmd.get("mode") == "rpm":
                self.starter.set_rpm_mech(starter_cmd.get("value", 0.0), self.pole_pairs_starter)
            else:
                self.starter.set_duty(starter_cmd.get("value", 0.0))

    def _loop(self) -> None:
        self._running = True
        while self._running:
            t_now = time.time()
            t = t_now - self._t0

            with self.lock:
                # cyclogram step switching
                if self.cycle_active and self.cycle:
                    duration_s, _, _, _, _ = self.cycle[self.cycle_idx]
                    if (t_now - self.step_t0) >= float(duration_s):
                        self.cycle_idx += 1
                        if self.cycle_idx >= len(self.cycle):
                            self.cycle_active = False
                            self.stage = "done"
                            try:
                                self.pump.set_duty(0.0)
                                self.starter.set_duty(0.0)
                                self.psu.output(False)
                            except Exception:
                                pass
                        else:
                            self.step_t0 = t_now
                            self._apply_step(self.cycle[self.cycle_idx])

                pump_vals = self.pump.get_values(self.pole_pairs_pump) if self.pump.is_connected else None
                starter_vals = self.starter.get_values(self.pole_pairs_starter) if self.starter.is_connected else None
                psu_vals = self.psu.read() if self.psu.is_connected else None

                sample = {
                    "t": t,
                    "stage": self.stage,

                    "pump": pump_vals or {},
                    "starter": starter_vals or {},
                    "psu": psu_vals or {},

                    "connected": {
                        "pump": self.pump.is_connected,
                        "starter": self.starter.is_connected,
                        "psu": self.psu.is_connected,
                    }
                }

                # CSV
                if self.logging_on:
                    row = [
                        t, self.stage,
                        (pump_vals or {}).get("rpm_mech", 0.0),
                        (pump_vals or {}).get("duty", 0.0),
                        (pump_vals or {}).get("current_motor", 0.0),

                        (starter_vals or {}).get("rpm_mech", 0.0),
                        (starter_vals or {}).get("duty", 0.0),
                        (starter_vals or {}).get("current_motor", 0.0),

                        (psu_vals or {}).get("v_set", 0.0),
                        (psu_vals or {}).get("i_set", 0.0),
                        (psu_vals or {}).get("v_out", 0.0),
                        (psu_vals or {}).get("i_out", 0.0),
                        (psu_vals or {}).get("p_out", 0.0),
                    ]
                    self.logger.write_row(row)
                    self.logger.flush()

            self.sample.emit(sample)
            time.sleep(self.dt)

    def shutdown(self) -> None:
        self._running = False
        with self.lock:
            self.stop_all()
            self.disconnect_all()
            self.logger.stop()
        try:
            self._thread.quit()
            self._thread.wait(1000)
        except Exception:
            pass

    # worker.py (всередині class ControllerWorker)

    def disconnect_pump(self) -> None:
        with self.lock:
            self.pump.disconnect()

    def disconnect_starter(self) -> None:
        with self.lock:
            self.starter.disconnect()

    def disconnect_psu(self) -> None:
        with self.lock:
            self.psu.disconnect()
