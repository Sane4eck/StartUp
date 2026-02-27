# main.py
import sys
from PyQt5.QtWidgets import QApplication
from ui_main_window import MainWindow

if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow()
    w.resize(1100, 800)
    w.show()
    sys.exit(app.exec_())
