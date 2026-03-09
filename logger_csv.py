# logger_csv.py
from __future__ import annotations

import csv
import os
from datetime import datetime
from typing import Any, Dict, List, Optional


# ТІЛЬКИ ці поля з VESC GetValues лишаємо в CSV:
VESC_KEEP_KEYS = [
    "temp_fet",
    "avg_motor_current",
    "avg_input_current",
    "duty_cycle_now",
    "rpm",
    "v_in",
    "watt_hours",
    "watt_hours_charged",
    "amp_hours",
    "amp_hours_charged",
]


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


class CSVLogger:
    """
    Відповідає за:
      - створення файлу
      - header
      - збирання одного row (щоб worker не "знав" які поля логуються)
    """

    def __init__(self):
        self.f = None
        self.w = None
        self.path: Optional[str] = None
        self.header: List[str] = []

    def start(self, folder: str = "logs", prefix: str = "session") -> str:
        os.makedirs(folder, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(folder, f"{prefix}_{ts}.csv")

        self.f = open(self.path, "w", newline="", encoding="utf-8", buffering=1)
        self.w = csv.writer(self.f)

        self.header = self.build_header()
        self.w.writerow(self.header)
        self.flush()

        return self.path

    def build_header(self) -> List[str]:
        return [
            "t", "stage",

            # what you set (commands)
            "pump_cmd_mode", "pump_cmd_value", "pump_cmd_rpm", "pump_cmd_duty", "pump_cmd_erpm",
            "starter_cmd_mode", "starter_cmd_value", "starter_cmd_rpm", "starter_cmd_duty", "starter_cmd_erpm",

            # selected VESC data (pump)
            "pump_rpm_mech",
            *[f"pump_{k}" for k in VESC_KEEP_KEYS],

            # selected VESC data (starter)
            "starter_rpm_mech",
            *[f"starter_{k}" for k in VESC_KEEP_KEYS],

            # PSU (як було)
            "psu_v_set", "psu_i_set", "psu_v_out", "psu_i_out", "psu_p_out",
        ]

    def build_row(
        self,
        t: float,
        stage: str,
        pump_target: Dict[str, Any],
        starter_target: Dict[str, Any],
        pole_pairs_pump: int,
        pole_pairs_starter: int,
        pump_vals: Any,    # VESCValues
        starter_vals: Any, # VESCValues
        psu: Dict[str, Any],
    ) -> List[Any]:
        # ---- commands
        p_cmd = self._cmd_cols(pump_target, pole_pairs_pump)
        s_cmd = self._cmd_cols(starter_target, pole_pairs_starter)

        # ---- vesc selected
        pump_cols = self._vesc_selected(pump_vals, prefix="pump_")
        starter_cols = self._vesc_selected(starter_vals, prefix="starter_")

        # ---- psu
        psu_v_set = float(psu.get("v_set", 0.0)) if psu else 0.0
        psu_i_set = float(psu.get("i_set", 0.0)) if psu else 0.0
        psu_v_out = float(psu.get("v_out", 0.0)) if psu else 0.0
        psu_i_out = float(psu.get("i_out", 0.0)) if psu else 0.0
        psu_p_out = float(psu.get("p_out", 0.0)) if psu else 0.0

        row_map: Dict[str, Any] = {
            "t": t,
            "stage": stage,

            **p_cmd,
            **s_cmd,

            **pump_cols,
            **starter_cols,

            "psu_v_set": psu_v_set,
            "psu_i_set": psu_i_set,
            "psu_v_out": psu_v_out,
            "psu_i_out": psu_i_out,
            "psu_p_out": psu_p_out,
        }

        return [row_map.get(col, "") for col in self.header]

    def write_row(self, row: List[Any]) -> None:
        if self.w:
            self.w.writerow(row)

    def flush(self) -> None:
        if self.f:
            try:
                self.f.flush()
            except Exception:
                pass

    def stop(self) -> None:
        try:
            self.flush()
            if self.f:
                self.f.close()
        finally:
            self.f = None
            self.w = None
            self.path = None
            self.header = []

    # -------- helpers
    def _cmd_cols(self, target: Dict[str, Any], pole_pairs: int) -> Dict[str, Any]:
        mode = str(target.get("mode", "duty"))
        val = float(target.get("value", 0.0))
        pp = max(1, int(pole_pairs))

        out = {
            "cmd_mode": mode,
            "cmd_value": val,
            "cmd_rpm": "",
            "cmd_duty": "",
            "cmd_erpm": "",
        }
        if mode == "rpm":
            out["cmd_rpm"] = val
            out["cmd_erpm"] = val * pp
        else:
            out["cmd_duty"] = _clamp01(val)

        return out

    def _vesc_selected(self, vesc_vals: Any, prefix: str) -> Dict[str, Any]:
        # vesc_vals: має мати rpm_mech і raw(dict)
        rpm_mech = float(getattr(vesc_vals, "rpm_mech", 0.0) or 0.0)
        raw = getattr(vesc_vals, "raw", {}) or {}

        out: Dict[str, Any] = {f"{prefix}rpm_mech": rpm_mech}
        for k in VESC_KEEP_KEYS:
            out[f"{prefix}{k}"] = raw.get(k, "")
        return out
