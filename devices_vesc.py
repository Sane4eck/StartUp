# devices_vesc.py
from __future__ import annotations

import time
import serial
from pyvesc import encode, encode_request, decode
from pyvesc.VESC.messages import SetDutyCycle, SetRPM, GetValues


class VESCDevice:
    def __init__(self, baudrate: int = 115200, timeout: float = 0.05):
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser: serial.Serial | None = None
        self.port: str | None = None

    @property
    def is_connected(self) -> bool:
        return bool(self.ser and self.ser.is_open)

    def connect(self, port: str) -> None:
        self.disconnect()
        self.ser = serial.Serial(port, self.baudrate, timeout=self.timeout)
        try:
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
        except Exception:
            pass
        self.port = port
        time.sleep(0.05)

    def disconnect(self) -> None:
        if self.ser and self.ser.is_open:
            try:
                self.set_duty(0.0)
            except Exception:
                pass
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.port = None

    def set_duty(self, duty: float) -> None:
        # duty in [-1..1]
        if not self.is_connected:
            return
        duty = max(-1.0, min(1.0, float(duty)))
        pkt = encode(SetDutyCycle(duty))
        self.ser.write(pkt)

    def set_rpm_mech(self, rpm_mech: float, pole_pairs: int) -> None:
        if not self.is_connected:
            return
        pole_pairs = max(1, int(pole_pairs))
        erpm = int(float(rpm_mech) * pole_pairs)
        pkt = encode(SetRPM(erpm))
        self.ser.write(pkt)

    def get_values(self, pole_pairs: int, timeout: float = 0.15) -> dict | None:
        """
        Return:
          rpm_mech, erpm, duty, current_motor, v_in
        """
        if not self.is_connected:
            return None

        pole_pairs = max(1, int(pole_pairs))

        # request
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        self.ser.write(encode_request(GetValues))

        deadline = time.time() + timeout
        buf = b""

        while time.time() < deadline:
            chunk = self.ser.read(256)
            if chunk:
                buf += chunk

            msg, consumed = decode(buf)
            if consumed:
                buf = buf[consumed:]

            if isinstance(msg, GetValues):
                erpm = float(getattr(msg, "rpm", 0.0))
                rpm_mech = erpm / pole_pairs
                duty = float(getattr(msg, "duty_cycle_now", 0.0))
                current = float(getattr(msg, "avg_motor_current", 0.0))
                v_in = float(getattr(msg, "v_in", 0.0))
                return {
                    "rpm_mech": rpm_mech,
                    "erpm": erpm,
                    "duty": duty,
                    "current_motor": current,
                    "v_in": v_in,
                }

            # safety: prevent unbounded buffer growth
            if len(buf) > 4096:
                buf = buf[-1024:]

        return None
