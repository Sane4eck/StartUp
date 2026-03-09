# main.py
import sys
import faulthandler

from PyQt5.QtWidgets import QApplication
from ui_main_window import MainWindow

faulthandler.enable(all_threads=True)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow()
    w.resize(1100, 800)
    w.show()
    sys.exit(app.exec_())
