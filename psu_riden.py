# psu_riden.py
from riden import Riden

class RidenPSU:
    def __init__(self, port: str, baudrate: int = 115200, address: int = 1, close_after_call: bool = False):
        self.dev = Riden(port=port, baudrate=baudrate, address=address, close_after_call=close_after_call)

    def set_vi(self, v: float, i: float) -> None:
        self.dev.set_v_set(v)
        self.dev.set_i_set(i)

    def output(self, on: bool) -> None:
        self.dev.set_output(on)

    def read(self) -> dict:
        self.dev.update()  # масове читання (швидше/менше запитів)
        return {
            "v_set": self.dev.v_set,
            "i_set": self.dev.i_set,
            "v_out": self.dev.v_out,
            "i_out": self.dev.i_out,
            "p_out": self.dev.p_out,
            "cv_cc": self.dev.cv_cc,      # "CV" або "CC"
            "ovp_ocp": self.dev.ovp_ocp,  # "OVP"/"OCP"/None
            "v_in": self.dev.v_in,
        }
