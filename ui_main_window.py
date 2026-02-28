# ui_main_window.py
from __future__ import annotations

import os

from PyQt5.QtCore import QTimer, pyqtSignal, Qt, QThread, QMetaObject
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox, QLineEdit,
    QTabWidget, QGroupBox, QSizePolicy, QFileDialog
)
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as Canvas
from matplotlib.figure import Figure

from worker import ControllerWorker


class Lamp(QLabel):
    def __init__(self):
        super().__init__()
        self.setFixedSize(12, 12)
        self.set_on(False)

    def set_on(self, on: bool):
        color = "#00b050" if on else "#c00000"
        self.setStyleSheet(f"background-color: {color}; border-radius: 6px;")


class MainWindow(QWidget):
    # UI -> worker signals
    sig_ready = pyqtSignal(str)
    sig_update_reset = pyqtSignal()
    sig_run_cycle = pyqtSignal()
    sig_cooling = pyqtSignal(float)
    sig_stop_all = pyqtSignal()

    sig_connect_pump = pyqtSignal(str)
    sig_disconnect_pump = pyqtSignal()
    sig_connect_starter = pyqtSignal(str)
    sig_disconnect_starter = pyqtSignal()
    sig_connect_psu = pyqtSignal(str)
    sig_disconnect_psu = pyqtSignal()

    sig_set_pp_pump = pyqtSignal(int)
    sig_set_pp_starter = pyqtSignal(int)

    sig_set_pump_duty = pyqtSignal(float)
    sig_set_pump_rpm = pyqtSignal(float)
    sig_set_starter_duty = pyqtSignal(float)
    sig_set_starter_rpm = pyqtSignal(float)

    sig_psu_set_vi = pyqtSignal(float, float)
    sig_psu_output = pyqtSignal(bool)

    # NEW: pump profile on Manual tab (no Loop)
    sig_pump_profile_start = pyqtSignal(str)  # path
    sig_pump_profile_stop = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Dual VESC + PSU (Manual / Cyclogram)")

        self.setStyleSheet(self.styleSheet() + """
        QPushButton[active="true"] {
            background-color: #2d6cdf;
            color: white;
            font-weight: bold;
        }
        """)

        # plot throttle
        self._plot_dirty = False
        self._plot_timer = QTimer(self)
        self._plot_timer.timeout.connect(self._redraw_if_dirty)
        self._plot_timer.start(200)  # 5 Hz redraw

        # worker thread
        self.worker_thread = QThread(self)
        self.worker = ControllerWorker(dt=0.05)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.start)

        self.worker.sample.connect(self.on_sample)
        self.worker.status.connect(self.on_status)
        self.worker.error.connect(self.on_error)
        self.worker.log.connect(self.on_log)

        # connect signals -> slots
        self.sig_ready.connect(self.worker.cmd_ready)
        self.sig_update_reset.connect(self.worker.cmd_update_reset)
        self.sig_run_cycle.connect(self.worker.cmd_run_cycle)
        self.sig_cooling.connect(self.worker.cmd_cooling_cycle)
        self.sig_stop_all.connect(self.worker.cmd_stop_all)

        self.sig_connect_pump.connect(self.worker.cmd_connect_pump)
        self.sig_disconnect_pump.connect(self.worker.cmd_disconnect_pump)
        self.sig_connect_starter.connect(self.worker.cmd_connect_starter)
        self.sig_disconnect_starter.connect(self.worker.cmd_disconnect_starter)
        self.sig_connect_psu.connect(self.worker.cmd_connect_psu)
        self.sig_disconnect_psu.connect(self.worker.cmd_disconnect_psu)

        self.sig_set_pp_pump.connect(self.worker.cmd_set_pole_pairs_pump)
        self.sig_set_pp_starter.connect(self.worker.cmd_set_pole_pairs_starter)

        self.sig_set_pump_duty.connect(self.worker.cmd_set_pump_duty)
        self.sig_set_pump_rpm.connect(self.worker.cmd_set_pump_rpm)
        self.sig_set_starter_duty.connect(self.worker.cmd_set_starter_duty)
        self.sig_set_starter_rpm.connect(self.worker.cmd_set_starter_rpm)

        self.sig_psu_set_vi.connect(self.worker.cmd_psu_set_vi)
        self.sig_psu_output.connect(self.worker.cmd_psu_output)

        # NEW
        self.sig_pump_profile_start.connect(self.worker.cmd_start_pump_profile)
        self.sig_pump_profile_stop.connect(self.worker.cmd_stop_pump_profile)

        self.worker_thread.start()

        # port refresh timer
        self.port_timer = QTimer(self)
        self.port_timer.timeout.connect(self.refresh_ports)
        self.port_timer.start(1000)

        # buffers
        self.t = []
        self.pump_rpm = []
        self.starter_rpm = []
        self.pump_duty = []
        self.starter_duty = []
        self.pump_cur = []
        self.starter_cur = []
        self.psu_v = []
        self.psu_i = []
        self.stage = []

        # plot
        self.canvas = Canvas(Figure(figsize=(9, 6)))
        self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        fig = self.canvas.figure

        self.ax = fig.add_subplot(211)
        self.ax_psu = fig.add_subplot(212)

        (self.l_pump_rpm,) = self.ax.plot([], [], label="Pump RPM", color="red")
        (self.l_starter_rpm,) = self.ax.plot([], [], label="Starter RPM", color="blue")
        self.ax.set_ylabel("RPM")
        self.ax.grid(True)
        self.ax.legend(loc="upper left")

        self.ax_duty = self.ax.twinx()
        (self.l_pump_duty,) = self.ax_duty.plot([], [], linestyle="--", label="Pump Duty", color="red")
        (self.l_starter_duty,) = self.ax_duty.plot([], [], linestyle="--", label="Starter Duty", color="blue")
        self.ax_duty.set_ylabel("Duty")
        self.ax_duty.legend(loc="upper center")

        self.ax_cur = self.ax.twinx()
        self.ax_cur.spines["right"].set_position(("outward", 55))
        (self.l_pump_cur,) = self.ax_cur.plot([], [], linestyle=":", label="Pump Current", color="red")
        (self.l_starter_cur,) = self.ax_cur.plot([], [], linestyle=":", label="Starter Current", color="blue")
        self.ax_cur.set_ylabel("Current (A)")
        self.ax_cur.legend(loc="upper right")

        (self.l_psu_v,) = self.ax_psu.plot([], [], label="PSU Vout", color="green")
        self.ax_psu.set_ylabel("V")
        self.ax_psu.set_xlabel("t (s)")
        self.ax_psu.grid(True)
        self.ax_psu.legend(loc="upper left")

        self.ax_psu_i = self.ax_psu.twinx()
        (self.l_psu_i,) = self.ax_psu_i.plot([], [], linestyle="--", label="PSU Iout", color="green")
        self.ax_psu_i.set_ylabel("A")
        self.ax_psu_i.legend(loc="upper right")

        fig.tight_layout()

        # tabs
        self.tabs = QTabWidget()
        self.tab_manual = QWidget()
        self.tab_cycle = QWidget()
        self.tabs.addTab(self.tab_manual, "Manual")
        self.tabs.addTab(self.tab_cycle, "Cyclogram")

        self._build_manual_tab()
        self._build_cycle_tab()

        # status row
        self.lbl_stage = QLabel("stage: -")
        self.lbl_log = QLabel("log: -")
        self.lbl_error = QLabel("")
        self.lbl_error.setStyleSheet("color: #c00000;")

        root = QVBoxLayout()
        root.addWidget(self.canvas, stretch=3)
        root.addWidget(self.tabs, stretch=2)

        st = QHBoxLayout()
        st.addWidget(self.lbl_stage)
        st.addSpacing(20)
        st.addWidget(self.lbl_log)
        st.addStretch(1)
        st.addWidget(self.lbl_error)
        root.addLayout(st)

        self.setLayout(root)
        self.refresh_ports()

    # ---------------- UI builders
    def _vesc_group(self, title: str, default_pp="3", default_duty="0.0", default_rpm="0", with_pump_profile=False):
        g = QGroupBox(title)
        l = QVBoxLayout()

        row1 = QHBoxLayout()
        lamp = Lamp()
        cb = QComboBox()
        rpm_live = QLabel("0 rpm")
        rpm_live.setStyleSheet("color: #c00000; font-weight: bold; font-size: 16px;")
        rpm_live.setFixedWidth(120)

        row1.addWidget(QLabel("COM:"))
        row1.addWidget(cb)
        row1.addWidget(lamp)
        btn_c = QPushButton("Connect")
        btn_d = QPushButton("Disconnect")
        row1.addWidget(btn_c)
        row1.addWidget(btn_d)
        row1.addStretch(1)
        row1.addWidget(QLabel("RPM:"))
        row1.addWidget(rpm_live)
        l.addLayout(row1)

        row2 = QHBoxLayout()
        pp = QLineEdit(str(default_pp))
        pp.setFixedWidth(60)
        duty = QLineEdit(str(default_duty))
        rpm = QLineEdit(str(default_rpm))

        row2.addWidget(QLabel("pole pairs:"))
        row2.addWidget(pp)
        row2.addSpacing(10)

        row2.addWidget(QLabel("duty:"))
        row2.addWidget(duty)
        btn_set_d = QPushButton("Set duty")
        row2.addWidget(btn_set_d)
        row2.addSpacing(10)

        row2.addWidget(QLabel("rpm(mech):"))
        row2.addWidget(rpm)
        btn_set_r = QPushButton("Set rpm")
        row2.addWidget(btn_set_r)

        btn_stop = QPushButton("Stop")
        row2.addWidget(btn_stop)

        # Pump cyclogram controls in SAME ROW (no Loop, no extra Stop)
        prof_path = None
        prof_browse = None
        prof_start = None
        if with_pump_profile:
            row2.addSpacing(12)
            row2.addWidget(QLabel("Cyclogram:"))

            prof_path = QLineEdit("")
            prof_path.setReadOnly(True)
            prof_path.setPlaceholderText("file.xlsx")
            prof_path.setMinimumWidth(220)
            row2.addWidget(prof_path)

            prof_browse = QPushButton("...")
            prof_browse.setFixedWidth(32)
            row2.addWidget(prof_browse)

            prof_start = QPushButton("Start")
            row2.addWidget(prof_start)

        row2.addStretch(1)
        l.addLayout(row2)

        g.setLayout(l)

        if with_pump_profile:
            return (g, cb, lamp, rpm_live, pp, duty, rpm, btn_c, btn_d, btn_set_d, btn_set_r, btn_stop,
                    prof_path, prof_browse, prof_start)
        return (g, cb, lamp, rpm_live, pp, duty, rpm, btn_c, btn_d, btn_set_d, btn_set_r, btn_stop)

    def _build_manual_tab(self):
        layout = QVBoxLayout()

        # Pump group with inline cyclogram controls
        (self.grp_pump, self.cb_pump, self.lamp_pump, self.lbl_pump_rpm_live, self.pp_pump,
         self.in_pump_duty, self.in_pump_rpm, self.btn_pump_c, self.btn_pump_d,
         self.btn_pump_set_d, self.btn_pump_set_r, self.btn_pump_stop,
         self.in_pump_prof_path, self.btn_pump_prof_browse, self.btn_pump_prof_start
         ) = self._vesc_group("Pump VESC", default_pp="7", default_duty="0.07", default_rpm="2600", with_pump_profile=True)

        (self.grp_starter, self.cb_starter, self.lamp_starter, self.lbl_starter_rpm_live, self.pp_starter,
         self.in_starter_duty, self.in_starter_rpm, self.btn_starter_c, self.btn_starter_d,
         self.btn_starter_set_d, self.btn_starter_set_r, self.btn_starter_stop
         ) = self._vesc_group("Starter VESC", default_pp="3", default_duty="0.05", default_rpm="1000")

        # PSU group
        self.grp_psu = QGroupBox("PSU (RD6024)")
        lpsu = QVBoxLayout()

        r1 = QHBoxLayout()
        self.cb_psu = QComboBox()
        self.lamp_psu = Lamp()
        self.btn_psu_c = QPushButton("Connect")
        self.btn_psu_d = QPushButton("Disconnect")
        self.lbl_psu_live = QLabel("0.0V / 0.0A")
        self.lbl_psu_live.setStyleSheet("color: #c00000; font-weight: bold; font-size: 16px;")
        self.lbl_psu_live.setFixedWidth(160)

        r1.addWidget(QLabel("COM:"))
        r1.addWidget(self.cb_psu)
        r1.addWidget(self.lamp_psu)
        r1.addWidget(self.btn_psu_c)
        r1.addWidget(self.btn_psu_d)
        r1.addStretch(1)
        r1.addWidget(QLabel("V/I:"))
        r1.addWidget(self.lbl_psu_live)
        lpsu.addLayout(r1)

        r2 = QHBoxLayout()
        self.in_psu_v = QLineEdit("0.0")
        self.in_psu_i = QLineEdit("20.0")
        self.btn_psu_set = QPushButton("Set V/I")
        self.btn_psu_on = QPushButton("Output ON")
        self.btn_psu_off = QPushButton("Output OFF")

        r2.addWidget(QLabel("V:"))
        r2.addWidget(self.in_psu_v)
        r2.addWidget(QLabel("I:"))
        r2.addWidget(self.in_psu_i)
        r2.addWidget(self.btn_psu_set)
        r2.addWidget(self.btn_psu_on)
        r2.addWidget(self.btn_psu_off)
        r2.addStretch(1)
        lpsu.addLayout(r2)

        self.grp_psu.setLayout(lpsu)

        # session buttons
        row = QHBoxLayout()
        self.btn_ready = QPushButton("Ready")
        self.btn_update = QPushButton("Update")
        self.btn_stop_all = QPushButton("Stop All")
        row.addWidget(self.btn_ready)
        row.addWidget(self.btn_update)
        row.addWidget(self.btn_stop_all)
        row.addStretch(1)

        layout.addLayout(row)
        layout.addWidget(self.grp_pump)
        layout.addWidget(self.grp_starter)
        layout.addWidget(self.grp_psu)
        layout.addStretch(1)
        self.tab_manual.setLayout(layout)

        # active groups
        self._pump_mode_buttons = [self.btn_pump_set_d, self.btn_pump_set_r, self.btn_pump_stop]
        self._starter_mode_buttons = [self.btn_starter_set_d, self.btn_starter_set_r, self.btn_starter_stop]
        self._psu_output_buttons = [self.btn_psu_on, self.btn_psu_off]

        # wiring
        self.btn_pump_c.clicked.connect(lambda: self.sig_connect_pump.emit(self.cb_pump.currentText()))
        self.btn_pump_d.clicked.connect(self.sig_disconnect_pump.emit)
        self.btn_pump_set_d.clicked.connect(self._set_pump_duty)
        self.btn_pump_set_r.clicked.connect(self._set_pump_rpm)
        self.btn_pump_stop.clicked.connect(self._pump_stop)

        self.btn_starter_c.clicked.connect(lambda: self.sig_connect_starter.emit(self.cb_starter.currentText()))
        self.btn_starter_d.clicked.connect(self.sig_disconnect_starter.emit)
        self.btn_starter_set_d.clicked.connect(self._set_starter_duty)
        self.btn_starter_set_r.clicked.connect(self._set_starter_rpm)
        self.btn_starter_stop.clicked.connect(self._starter_stop)

        self.btn_psu_c.clicked.connect(lambda: self.sig_connect_psu.emit(self.cb_psu.currentText()))
        self.btn_psu_d.clicked.connect(self.sig_disconnect_psu.emit)
        self.btn_psu_set.clicked.connect(self._psu_set_vi)
        self.btn_psu_on.clicked.connect(self._psu_on)
        self.btn_psu_off.clicked.connect(self._psu_off)

        self.btn_ready.clicked.connect(lambda: self.sig_ready.emit("manual"))
        self.btn_update.clicked.connect(self._update_reset)
        self.btn_stop_all.clicked.connect(self._stop_all_clicked)

        # Enter = click
        self.in_pump_duty.returnPressed.connect(self.btn_pump_set_d.click)
        self.in_pump_rpm.returnPressed.connect(self.btn_pump_set_r.click)
        self.in_starter_duty.returnPressed.connect(self.btn_starter_set_d.click)
        self.in_starter_rpm.returnPressed.connect(self.btn_starter_set_r.click)
        self.in_psu_v.returnPressed.connect(self.btn_psu_set.click)
        self.in_psu_i.returnPressed.connect(self.btn_psu_set.click)

        # pump cyclogram (inline)
        self.btn_pump_prof_browse.clicked.connect(self._browse_pump_profile)
        self.btn_pump_prof_start.clicked.connect(self._start_pump_profile)

        # default highlights
        self._set_active_buttons(self._pump_mode_buttons, self.btn_pump_stop)
        self._set_active_buttons(self._starter_mode_buttons, self.btn_starter_stop)
        self._set_active_buttons(self._psu_output_buttons, self.btn_psu_off)

    def _build_cycle_tab(self):
        layout = QVBoxLayout()

        row = QHBoxLayout()
        self.btn_ready2 = QPushButton("Ready")
        self.btn_run = QPushButton("Run")
        self.btn_cooling = QPushButton("Cooling")
        self.btn_stop2 = QPushButton("Stop")
        self.btn_update2 = QPushButton("Update")
        row.addWidget(self.btn_ready2)
        row.addWidget(self.btn_run)
        row.addWidget(self.btn_stop2)
        row.addWidget(self.btn_cooling)
        row.addWidget(self.btn_update2)
        row.addStretch(1)
        layout.addLayout(row)

        gb_info = QGroupBox("Session")
        li = QHBoxLayout()
        self.in_product = QLineEdit("product_1")
        self.lbl_cycle_stage = QLabel("stage: -")
        li.addWidget(QLabel("product:"))
        li.addWidget(self.in_product)
        li.addSpacing(15)
        li.addWidget(QLabel("status:"))
        li.addWidget(self.lbl_cycle_stage)
        li.addStretch(1)
        gb_info.setLayout(li)
        layout.addWidget(gb_info)

        gb_live = QGroupBox("Live")
        ll = QHBoxLayout()
        self.lbl_c_pump = QLabel("Pump: 0 rpm")
        self.lbl_c_pump.setStyleSheet("color: red; font-weight: bold; font-size: 16px;")
        self.lbl_c_starter = QLabel("Starter: 0 rpm")
        self.lbl_c_starter.setStyleSheet("color: blue; font-weight: bold; font-size: 16px;")
        self.lbl_c_valve = QLabel("Valve: 0.0V / 0.0A")
        self.lbl_c_valve.setStyleSheet("color: green; font-weight: bold; font-size: 16px;")
        self.in_cool_duty = QLineEdit("0.05")
        self.in_cool_duty.setFixedWidth(80)
        ll.addWidget(self.lbl_c_pump)
        ll.addSpacing(20)
        ll.addWidget(self.lbl_c_starter)
        ll.addSpacing(20)
        ll.addWidget(self.lbl_c_valve)
        ll.addStretch(1)
        ll.addWidget(QLabel("Cooling duty:"))
        ll.addWidget(self.in_cool_duty)
        gb_live.setLayout(ll)
        layout.addWidget(gb_live)

        gb_ports = QGroupBox("Ports")
        lp = QVBoxLayout()

        def port_row(title: str):
            r = QHBoxLayout()
            cb = QComboBox()
            lamp = Lamp()
            b1 = QPushButton("Connect")
            b2 = QPushButton("Disconnect")
            r.addWidget(QLabel(title))
            r.addWidget(cb)
            r.addWidget(lamp)
            r.addWidget(b1)
            r.addWidget(b2)
            r.addStretch(1)
            lp.addLayout(r)
            return cb, lamp, b1, b2

        self.cb_pump2, self.lamp_pump2, self.btn_pump2_c, self.btn_pump2_d = port_row("Pump VESC:")
        self.cb_starter2, self.lamp_starter2, self.btn_starter2_c, self.btn_starter2_d = port_row("Starter VESC:")
        self.cb_psu2, self.lamp_psu2, self.btn_psu2_c, self.btn_psu2_d = port_row("PSU:")

        gb_ports.setLayout(lp)
        layout.addWidget(gb_ports)

        layout.addStretch(1)
        self.tab_cycle.setLayout(layout)

        self.btn_ready2.clicked.connect(lambda: self.sig_ready.emit(self.in_product.text().strip() or "cycle"))
        self.btn_run.clicked.connect(lambda: self.sig_run_cycle.emit())

        def _cooling_click():
            try:
                d = float(self.in_cool_duty.text())
            except Exception:
                d = 0.05
            self.sig_cooling.emit(d)

        self.btn_cooling.clicked.connect(_cooling_click)
        self.btn_stop2.clicked.connect(self._stop_all_clicked)
        self.btn_update2.clicked.connect(self._update_reset)

        self.btn_pump2_c.clicked.connect(lambda: self.sig_connect_pump.emit(self.cb_pump2.currentText()))
        self.btn_pump2_d.clicked.connect(lambda: self.sig_disconnect_pump.emit())
        self.btn_starter2_c.clicked.connect(lambda: self.sig_connect_starter.emit(self.cb_starter2.currentText()))
        self.btn_starter2_d.clicked.connect(lambda: self.sig_disconnect_starter.emit())
        self.btn_psu2_c.clicked.connect(lambda: self.sig_connect_psu.emit(self.cb_psu2.currentText()))
        self.btn_psu2_d.clicked.connect(lambda: self.sig_disconnect_psu.emit())

    # ---------------- pump profile UI
    def _browse_pump_profile(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select pump cyclogram Excel file",
            "",
            "Excel Files (*.xlsx *.xls)"
        )
        if path:
            self.in_pump_prof_path.setText(path)

    def _start_pump_profile(self):
        path = self.in_pump_prof_path.text().strip()
        self.sig_pump_profile_start.emit(path)

    # ---------------- actions
    def refresh_ports(self):
        ports = self.worker.list_ports()

        combos = [self.cb_pump, self.cb_starter, self.cb_psu]
        for name in ("cb_pump2", "cb_starter2", "cb_psu2"):
            if hasattr(self, name):
                combos.append(getattr(self, name))

        def refill(cb: QComboBox):
            prev = cb.currentText()
            cb.blockSignals(True)
            cb.clear()
            cb.addItems(ports)
            if prev in ports:
                cb.setCurrentText(prev)
            cb.blockSignals(False)

        for cb in combos:
            refill(cb)

    def _apply_pole_pairs(self):
        try:
            pp_p = int(float(self.pp_pump.text()))
        except Exception:
            pp_p = 1
        try:
            pp_s = int(float(self.pp_starter.text()))
        except Exception:
            pp_s = 1
        self.sig_set_pp_pump.emit(pp_p)
        self.sig_set_pp_starter.emit(pp_s)

    def _set_pump_duty(self):
        self._apply_pole_pairs()
        try:
            self.sig_set_pump_duty.emit(float(self.in_pump_duty.text()))
            self._set_active_buttons(self._pump_mode_buttons, self.btn_pump_set_d)
        except Exception:
            pass

    def _set_pump_rpm(self):
        self._apply_pole_pairs()
        try:
            self.sig_set_pump_rpm.emit(float(self.in_pump_rpm.text()))
            self._set_active_buttons(self._pump_mode_buttons, self.btn_pump_set_r)
        except Exception:
            pass

    def _set_starter_duty(self):
        self._apply_pole_pairs()
        try:
            self.sig_set_starter_duty.emit(float(self.in_starter_duty.text()))
            self._set_active_buttons(self._starter_mode_buttons, self.btn_starter_set_d)
        except Exception:
            pass

    def _set_starter_rpm(self):
        self._apply_pole_pairs()
        try:
            self.sig_set_starter_rpm.emit(float(self.in_starter_rpm.text()))
            self._set_active_buttons(self._starter_mode_buttons, self.btn_starter_set_r)
        except Exception:
            pass

    def _psu_set_vi(self):
        try:
            self.sig_psu_set_vi.emit(float(self.in_psu_v.text()), float(self.in_psu_i.text()))
        except Exception:
            pass

    def _pump_stop(self):
        # stop pump profile too
        self.sig_pump_profile_stop.emit()
        self.sig_set_pump_duty.emit(0.0)
        self._set_active_buttons(self._pump_mode_buttons, self.btn_pump_stop)

    def _starter_stop(self):
        self.sig_set_starter_duty.emit(0.0)
        self._set_active_buttons(self._starter_mode_buttons, self.btn_starter_stop)

    def _psu_on(self):
        self.sig_psu_output.emit(True)
        self._set_active_buttons(self._psu_output_buttons, self.btn_psu_on)

    def _psu_off(self):
        self.sig_psu_output.emit(False)
        self._set_active_buttons(self._psu_output_buttons, self.btn_psu_off)

    def _stop_all_clicked(self):
        self.sig_pump_profile_stop.emit()
        self.sig_stop_all.emit()
        self._set_active_buttons(self._pump_mode_buttons, self.btn_pump_stop)
        self._set_active_buttons(self._starter_mode_buttons, self.btn_starter_stop)
        self._set_active_buttons(self._psu_output_buttons, self.btn_psu_off)

    def _update_reset(self):
        self.sig_update_reset.emit()
        self.t.clear()
        self.pump_rpm.clear(); self.starter_rpm.clear()
        self.pump_duty.clear(); self.starter_duty.clear()
        self.pump_cur.clear(); self.starter_cur.clear()
        self.psu_v.clear(); self.psu_i.clear()
        self.stage.clear()
        self._redraw()

    # ---------------- plot update
    def on_sample(self, s: dict):
        t = float(s.get("t", 0.0))
        stage = s.get("stage", "-")
        self.lbl_stage.setText(f"stage: {stage}")
        self.lbl_cycle_stage.setText(f"stage: {stage}")

        pump = s.get("pump", {})
        starter = s.get("starter", {})
        psu = s.get("psu", {})

        self.t.append(t)
        self.stage.append(stage)
        self.pump_rpm.append(float(pump.get("rpm_mech", 0.0)))
        self.starter_rpm.append(float(starter.get("rpm_mech", 0.0)))
        self.pump_duty.append(float(pump.get("duty", 0.0)))
        self.starter_duty.append(float(starter.get("duty", 0.0)))
        self.pump_cur.append(float(pump.get("current_motor", 0.0)))
        self.starter_cur.append(float(starter.get("current_motor", 0.0)))
        self.psu_v.append(float(psu.get("v_out", 0.0)))
        self.psu_i.append(float(psu.get("i_out", 0.0)))

        self.lbl_pump_rpm_live.setText(f"{self.pump_rpm[-1]:.0f} rpm")
        self.lbl_starter_rpm_live.setText(f"{self.starter_rpm[-1]:.0f} rpm")
        self.lbl_psu_live.setText(f"{self.psu_v[-1]:.1f}V / {self.psu_i[-1]:.2f}A")

        self.lbl_c_pump.setText(f"Pump: {self.pump_rpm[-1]:.0f} rpm")
        self.lbl_c_starter.setText(f"Starter: {self.starter_rpm[-1]:.0f} rpm")
        self.lbl_c_valve.setText(f"Valve: {self.psu_v[-1]:.1f}V / {self.psu_i[-1]:.2f}A")

        # visible window
        WINDOW_S = 30.0
        while self.t and (self.t[-1] - self.t[0] > WINDOW_S):
            for arr in (self.t, self.stage, self.pump_rpm, self.starter_rpm, self.pump_duty, self.starter_duty,
                        self.pump_cur, self.starter_cur, self.psu_v, self.psu_i):
                arr.pop(0)

        self._plot_dirty = True

    def _redraw(self):
        if not self.t:
            self.canvas.draw_idle()
            return

        self.l_pump_rpm.set_data(self.t, self.pump_rpm)
        self.l_starter_rpm.set_data(self.t, self.starter_rpm)
        self.l_pump_duty.set_data(self.t, self.pump_duty)
        self.l_starter_duty.set_data(self.t, self.starter_duty)
        self.l_pump_cur.set_data(self.t, self.pump_cur)
        self.l_starter_cur.set_data(self.t, self.starter_cur)
        self.l_psu_v.set_data(self.t, self.psu_v)
        self.l_psu_i.set_data(self.t, self.psu_i)

        tmax = self.t[-1]
        tmin = max(0.0, tmax - 30.0)

        self.ax.set_xlim(tmin, tmax)
        self.ax.relim(); self.ax.autoscale_view(True, True, True)
        self.ax_duty.relim(); self.ax_duty.autoscale_view(True, True, True)
        self.ax_cur.relim(); self.ax_cur.autoscale_view(True, True, True)

        self.ax_psu.set_xlim(tmin, tmax)
        self.ax_psu.relim(); self.ax_psu.autoscale_view(True, True, True)
        self.ax_psu_i.relim(); self.ax_psu_i.autoscale_view(True, True, True)

        self.canvas.draw_idle()

    def _redraw_if_dirty(self):
        if not self._plot_dirty:
            return
        self._plot_dirty = False
        self._redraw()

    def on_status(self, st: dict):
        if st.get("ready"):
            self.lbl_log.setText(f"log: {st.get('log_path', '-')}")
        if "log_path" in st and st.get("log_path"):
            self.lbl_log.setText(f"log: {st.get('log_path')}")

        if "connected" in st:
            c = st["connected"]
            pump_on = bool(c.get("pump", False))
            starter_on = bool(c.get("starter", False))
            psu_on = bool(c.get("psu", False))

            self.lamp_pump.set_on(pump_on)
            self.lamp_starter.set_on(starter_on)
            self.lamp_psu.set_on(psu_on)

            if hasattr(self, "lamp_pump2"):
                self.lamp_pump2.set_on(pump_on)
                self.lamp_starter2.set_on(starter_on)
                self.lamp_psu2.set_on(psu_on)

        # Pump profile active highlight
        if "pump_profile" in st:
            p = st["pump_profile"] or {}
            active = bool(p.get("active", False))
            self.btn_pump_prof_start.setProperty("active", active)
            self.btn_pump_prof_start.style().unpolish(self.btn_pump_prof_start)
            self.btn_pump_prof_start.style().polish(self.btn_pump_prof_start)
            self.btn_pump_prof_start.update()

            # disable browse/start while running
            self.btn_pump_prof_start.setEnabled(not active)
            self.btn_pump_prof_browse.setEnabled(not active)

            # show selected filename if any
            path = p.get("path") or ""
            if path and not self.in_pump_prof_path.text():
                self.in_pump_prof_path.setText(path)

    def on_error(self, msg: str):
        self.lbl_error.setText(msg)

    def on_log(self, msg: str):
        pass

    def _set_active_buttons(self, buttons, active_btn=None):
        for b in buttons:
            b.setProperty("active", (b is active_btn))
            b.style().unpolish(b)
            b.style().polish(b)
            b.update()

    def closeEvent(self, event):
        try:
            self.port_timer.stop()
        except Exception:
            pass
        try:
            QMetaObject.invokeMethod(self.worker, "stop", Qt.BlockingQueuedConnection)
        except Exception:
            pass
        try:
            self.worker_thread.quit()
            if not self.worker_thread.wait(2000):
                self.worker_thread.terminate()
                self.worker_thread.wait(1000)
        except Exception:
            pass
        super().closeEvent(event)
