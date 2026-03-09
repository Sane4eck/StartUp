# main.py
import os
import sys

from PyQt5.QtWidgets import QApplication
from ui_main_window import MainWindow

def _fix_std_streams_for_windowed_exe():
    # PyInstaller --windowed може робити sys.stdout/sys.stderr = None
    if sys.stdout is None or sys.stderr is None:
        devnull = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = devnull
        if sys.stderr is None:
            sys.stderr = devnull

if getattr(sys, "frozen", False):
    _fix_std_streams_for_windowed_exe()

# faulthandler вмикаємо тільки коли stderr гарантовано існує
try:
    import faulthandler
    faulthandler.enable(all_threads=True)
except Exception:
    pass

if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow()
    w.resize(1100, 800)
    w.show()
    sys.exit(app.exec_())
