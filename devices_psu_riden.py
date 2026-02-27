# devices_psu_riden.py
from __future__ import annotations

try:
    from riden import Riden
except Exception:
    Riden = None


class RidenPSU:
    def __init__(self, baudrate: int = 115200, address: int = 1):
        self.baudrate = baudrate
        self.address = address
        self.dev = None
        self.port = None

    @property
    def available(self) -> bool:
        return Riden is not None

    @property
    def is_connected(self) -> bool:
        return self.dev is not None

    def connect(self, port: str) -> None:
        if not self.available:
            raise RuntimeError("riden library not installed or failed to import")
        self.disconnect()
        # close_after_call=False щоб не ловити hook-баг
        self.dev = Riden(port=port, baudrate=self.baudrate, address=self.address, close_after_call=False)
        self.port = port

    def disconnect(self) -> None:
        self.dev = None
        self.port = None

    def set_vi(self, v: float, i: float) -> None:
        if not self.dev:
            return
        self.dev.set_v_set(float(v))
        self.dev.set_i_set(float(i))

    def output(self, on: bool) -> None:
        if not self.dev:
            return
        self.dev.set_output(bool(on))

    def read(self) -> dict | None:
        if not self.dev:
            return None
        self.dev.update()
        return {
            "v_set": getattr(self.dev, "v_set", 0.0),
            "i_set": getattr(self.dev, "i_set", 0.0),
            "v_out": getattr(self.dev, "v_out", 0.0),
            "i_out": getattr(self.dev, "i_out", 0.0),
            "p_out": getattr(self.dev, "p_out", 0.0),
            "cv_cc": getattr(self.dev, "cv_cc", None),
            "ovp_ocp": getattr(self.dev, "ovp_ocp", None),
        }
