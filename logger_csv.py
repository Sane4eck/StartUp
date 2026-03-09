# logger_csv.py
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

# ТІЛЬКИ ці поля беремо з VESC GetValues (raw)
VESC_KEEP_KEYS: List[str] = [
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


@dataclass
class CsvConfig:
    folder: str = "logs"
    prefix: str = "session"
    flush_period_s: float = 1.0  # як часто робимо flush()


class CSVLogger:
    """
    Відповідає ТІЛЬКИ за файл і формат CSV.
    Worker передає: t/stage, targets, vesc values, psu dict.
    """

    def __init__(self, cfg: Optional[CsvConfig] = None):
        self.cfg = cfg or CsvConfig()
        self.path: Optional[str] = None
        self._f = None
        self._w: Optional[csv.writer] = None
        self.header: List[str] = []
        self._next_flush_t: float = 0.0

    def start(self, prefix: Optional[str] = None) -> str:
        os.makedirs(self.cfg.folder, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        pref = (prefix or self.cfg.prefix).strip() or self.cfg.prefix
        self.path = os.path.join(self.cfg.folder, f"{pref}_{ts}.csv")

        # line-buffered
        self._f = open(self.path, "w", newline="", encoding="utf-8", buffering=1)
        self._w = csv.writer(self._f)

        self.header = self.build_header()
        self._w.writerow(self.header)
        self.flush(force=True)

        return self.path

    def stop(self) -> None:
        try:
            self.flush(force=True)
        finally:
            try:
                if self._f:
                    self._f.close()
            except Exception:
                pass
            self._f = None
            self._w = None
            self.path = None
            self.header = []
            self._next_flush_t = 0.0

    def flush(self, force: bool = False, now: Optional[float] = None) -> None:
        if not self._f:
            return
        if force:
            try:
                self._f.flush()
            except Exception:
                pass
            return
        if now is None:
            return
        if now >= self._next_flush_t:
            try:
                self._f.flush()
            except Exception:
                pass
            self._next_flush_t = now + float(self.cfg.flush_period_s)

    # ---------- format ----------
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

    def write(
        self,
        *,
        now: float,
        t: float,
        stage: str,
        pump_target: Dict[str, Any],
        starter_target: Dict[str, Any],
        pole_pairs_pump: int,
        pole_pairs_starter: int,
        pump_vals: Any,    # VESCValues
        starter_vals: Any, # VESCValues
        psu: Dict[str, Any],
    ) -> None:
        if not self._w:
            return

        row_map: Dict[str, Any] = {
            "t": t,
            "stage": stage,
            **self._cmd_cols(pump_target, pole_pairs_pump, "pump_"),
            **self._cmd_cols(starter_target, pole_pairs_starter, "starter_"),
            **self._vesc_selected(pump_vals, "pump_"),
            **self._vesc_selected(starter_vals, "starter_"),
            "psu_v_set": float(psu.get("v_set", 0.0)) if psu else 0.0,
            "psu_i_set": float(psu.get("i_set", 0.0)) if psu else 0.0,
            "psu_v_out": float(psu.get("v_out", 0.0)) if psu else 0.0,
            "psu_i_out": float(psu.get("i_out", 0.0)) if psu else 0.0,
            "psu_p_out": float(psu.get("p_out", 0.0)) if psu else 0.0,
        }

        self._w.writerow([row_map.get(col, "") for col in self.header])
        self.flush(now=now)

    def _cmd_cols(self, target: Dict[str, Any], pole_pairs: int, prefix: str) -> Dict[str, Any]:
        mode = str(target.get("mode", "duty"))
        val = float(target.get("value", 0.0))
        pp = max(1, int(pole_pairs))

        out = {
            f"{prefix}cmd_mode": mode,
            f"{prefix}cmd_value": val,
            f"{prefix}cmd_rpm": "",
            f"{prefix}cmd_duty": "",
            f"{prefix}cmd_erpm": "",
        }
        if mode == "rpm":
            out[f"{prefix}cmd_rpm"] = val
            out[f"{prefix}cmd_erpm"] = val * pp
        else:
            out[f"{prefix}cmd_duty"] = _clamp01(val)
        return out

    def _vesc_selected(self, vesc_vals: Any, prefix: str) -> Dict[str, Any]:
        rpm_mech = float(getattr(vesc_vals, "rpm_mech", 0.0) or 0.0)
        raw = getattr(vesc_vals, "raw", {}) or {}

        out: Dict[str, Any] = {f"{prefix}rpm_mech": rpm_mech}
        for k in VESC_KEEP_KEYS:
            out[f"{prefix}{k}"] = raw.get(k, "")
        return out
