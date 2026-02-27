# devices_psu_riden.py
from __future__ import annotations

from typing import Optional, Dict


try:
    from riden import Riden
except Exception:
    Riden = None


class RidenPSU:
    """
    Wrapper for Riden_RD6024.
    All calls must happen from worker thread.
    """

    def __init__(self, baudrate: int = 115200, address: int = 1):
        self.baudrate = int(baudrate)
        self.address = int(address)
        self.dev: Optional["Riden"] = None
        self.port: Optional[str] = None

    @property
    def available(self) -> bool:
        return Riden is not None

    @property
    def is_connected(self) -> bool:
        return self.dev is not None

    def connect(self, port: str) -> None:
        if not self.available:
            raise RuntimeError("riden not installed / import failed")
        self.disconnect()
        # close_after_call=False (hook bug)
        self.dev = Riden(port=port, baudrate=self.baudrate, address=self.address, close_after_call=False)
        self.port = port

    def disconnect(self) -> None:
        if self.dev is not None:
            # try to close underlying serial
            try:
                s = getattr(self.dev, "serial", None)
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass
            except Exception:
                pass
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

    def read(self) -> Optional[Dict]:
        if not self.dev:
            return None
        self.dev.update()
        return {
            "v_set": float(getattr(self.dev, "v_set", 0.0)),
            "i_set": float(getattr(self.dev, "i_set", 0.0)),
            "v_out": float(getattr(self.dev, "v_out", 0.0)),
            "i_out": float(getattr(self.dev, "i_out", 0.0)),
            "p_out": float(getattr(self.dev, "p_out", 0.0)),
            "cv_cc": getattr(self.dev, "cv_cc", None),
            "ovp_ocp": getattr(self.dev, "ovp_ocp", None),
        }
