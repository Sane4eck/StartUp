# logger_csv.py
from __future__ import annotations

import csv
import os
from datetime import datetime
from typing import Optional, List


class CSVLogger:
    def __init__(self):
        self.f = None
        self.w = None
        self.path: Optional[str] = None

    def start(self, folder: str = "logs", prefix: str = "session") -> str:
        os.makedirs(folder, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(folder, f"{prefix}_{ts}.csv")

        # line-buffered
        self.f = open(self.path, "w", newline="", encoding="utf-8", buffering=1)
        self.w = csv.writer(self.f)
        self.w.writerow([
            "t", "stage",
            "pump_rpm", "pump_duty", "pump_current",
            "starter_rpm", "starter_duty", "starter_current",
            "psu_v_set", "psu_i_set", "psu_v_out", "psu_i_out", "psu_p_out",
        ])
        self.flush()
        return self.path

    def write_row(self, row: List) -> None:
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
