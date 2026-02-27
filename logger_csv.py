# logger_csv.py
from __future__ import annotations

import csv
import os
from datetime import datetime


class CSVLogger:
    def __init__(self):
        self.f = None
        self.w = None
        self.path = None

    def start(self, folder: str = "logs", prefix: str = "session") -> str:
        os.makedirs(folder, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(folder, f"{prefix}_{ts}.csv")
        self.f = open(self.path, "w", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)

        self.w.writerow([
            "t",
            "stage",
            "pump_rpm", "pump_duty", "pump_current",
            "starter_rpm", "starter_duty", "starter_current",
            "psu_v_set", "psu_i_set", "psu_v_out", "psu_i_out", "psu_p_out",
        ])
        self.f.flush()
        return self.path

    def write_row(self, row: list) -> None:
        if not self.w:
            return
        self.w.writerow(row)

    def flush(self) -> None:
        if self.f:
            self.f.flush()

    def stop(self) -> None:
        try:
            if self.f:
                self.f.flush()
                self.f.close()
        finally:
            self.f = None
            self.w = None
            self.path = None
