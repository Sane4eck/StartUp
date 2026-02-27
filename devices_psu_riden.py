# devices_psu_riden.py
from __future__ import annotations

from typing import Optional, Dict, Any

from serial import Serial
from serial import SerialException

from modbus_tk.defines import READ_HOLDING_REGISTERS, WRITE_SINGLE_REGISTER
from modbus_tk.modbus_rtu import RtuMaster
from modbus_tk.exceptions import ModbusInvalidResponseError, ModbusError


# Register map from korjaa/Riden_RD6024
# ID=0, FW=3, V_SET=8, I_SET=9, V_OUT=10, I_OUT=11, P_OUT=13, V_IN=14, OUTPUT=18
# :contentReference[oaicite:1]{index=1}
class R:
    ID = 0
    FW = 3
    V_SET = 8
    I_SET = 9
    V_OUT = 10
    I_OUT = 11
    P_OUT = 13
    V_IN = 14
    OUTPUT = 18


class RidenPSU:
    """
    Safe minimal RD60xx Modbus client.
    No infinite retries. Fast connect failure on wrong port/device.
    All calls must be from worker thread.
    """
    def __init__(self, baudrate: int = 115200, address: int = 1, timeout: float = 0.2, retries: int = 1):
        self.baudrate = int(baudrate)
        self.address = int(address)
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))

        self.serial: Optional[Serial] = None
        self.master: Optional[RtuMaster] = None
        self.port: Optional[str] = None

        # scale for RD6024 defaults (V/I/P in 0.01 units)
        self.v_multi = 100.0
        self.i_multi = 100.0
        self.p_multi = 100.0

        self._last: Dict[str, Any] = {}

    @property
    def is_connected(self) -> bool:
        return self.master is not None and self.serial is not None

    def connect(self, port: str) -> None:
        self.disconnect()
        try:
            self.serial = Serial(
                port=port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                write_timeout=self.timeout,
            )
            self.master = RtuMaster(self.serial)
            self.master.set_timeout(self.timeout)
            self.port = port

            # quick handshake: read ID..FW (0..3) once
            data = self._read_regs(R.ID, R.FW - R.ID + 1)
            _id = int(data[0])
            _fw = int(data[3])
            # if garbage, this will likely be weird; still accept, but at least read succeeded
            self._last["id"] = _id
            self._last["fw"] = _fw

        except Exception:
            self.disconnect()
            raise

    def disconnect(self) -> None:
        try:
            if self.serial is not None:
                try:
                    self.serial.close()
                except Exception:
                    pass
        finally:
            self.serial = None
            self.master = None
            self.port = None
            self._last = {}

    def set_vi(self, v: float, i: float) -> None:
        if not self.is_connected:
            return
        self._write_reg(R.V_SET, int(round(float(v) * self.v_multi)))
        self._write_reg(R.I_SET, int(round(float(i) * self.i_multi)))

    def output(self, on: bool) -> None:
        if not self.is_connected:
            return
        self._write_reg(R.OUTPUT, 1 if on else 0)

    def read(self) -> Optional[Dict[str, Any]]:
        if not self.is_connected:
            return None
        # read a compact block V_SET..OUTPUT (8..18) – 11 regs
        data = self._read_regs(R.V_SET, R.OUTPUT - R.V_SET + 1)

        v_set = data[0] / self.v_multi
        i_set = data[1] / self.i_multi
        v_out = data[2] / self.v_multi
        i_out = data[3] / self.i_multi
        # data[4] is AH (reg 12), skip
        p_out = data[5] / self.p_multi  # reg 13
        v_in = data[6] / 100.0          # reg 14 (per korjaa default)
        output = bool(data[10])         # reg 18

        self._last = {
            "v_set": v_set,
            "i_set": i_set,
            "v_out": v_out,
            "i_out": i_out,
            "p_out": p_out,
            "v_in": v_in,
            "output": output,
        }
        return self._last

    # ---------- internal safe modbus ops
    def _read_regs(self, start: int, length: int) -> tuple:
        if not self.master:
            raise RuntimeError("PSU not connected")
        last_exc = None
        for _ in range(self.retries + 1):
            try:
                return self.master.execute(self.address, READ_HOLDING_REGISTERS, start, length)
            except (ModbusInvalidResponseError, ModbusError, SerialException, OSError) as e:
                last_exc = e
        raise last_exc

    def _write_reg(self, reg: int, value: int) -> int:
        if not self.master:
            raise RuntimeError("PSU not connected")
        last_exc = None
        for _ in range(self.retries + 1):
            try:
                return self.master.execute(self.address, WRITE_SINGLE_REGISTER, reg, 1, int(value))[0]
            except (ModbusInvalidResponseError, ModbusError, SerialException, OSError) as e:
                last_exc = e
        raise last_exc
