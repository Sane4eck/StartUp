# ui_main_window.py
from __future__ import annotations

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QLineEdit, QTabWidget, QGroupBox, QSizePolicy
)
from PyQt5.QtCore import QTimer
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as Canvas
from matplotlib.figure import Figure

from worker import ControllerWorker


class Lamp(QLabel):
    def __init__(self):
        super().__init__()
        self.setFixedSize(12, 12)
        self.set_on(False)

    def set_on(self, on: bool):
        color = "#00b050" if on else "#c00000"  # green / red
        self.setStyleSheet(f"background-color: {color}; border-radius: 16px;")


class MainWindow(QWidget):
    def __init__(self):
        self.time_WINDOW_S = 30

        super().__init__()
        self.setWindowTitle("Dual VESC + PSU (Manual / Cyclogram)")

        self.ctrl = ControllerWorker(dt=0.1)
        self.ctrl.sample.connect(self.on_sample)
        self.ctrl.status.connect(self.on_status)

        self.port_timer = QTimer()
        self.port_timer.timeout.connect(self.refresh_ports)
        self.port_timer.start(1000)

        # ---------- data buffers
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

        # ---------- plot
        self.canvas = Canvas(Figure(figsize=(9, 6)))
        self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        fig = self.canvas.figure
        self.ax = fig.add_subplot(211)
        self.ax_psu = fig.add_subplot(212)

        # top axes: rpm + duty + current
        (self.l_pump_rpm,) = self.ax.plot([], [], label="Pump RPM")
        (self.l_starter_rpm,) = self.ax.plot([], [], label="Starter RPM")
        self.ax.set_ylabel("RPM")
        self.ax.grid(True)
        self.ax.legend(loc="upper left")

        self.ax_duty = self.ax.twinx()
        (self.l_pump_duty,) = self.ax_duty.plot([], [], linestyle="--", label="Pump Duty")
        (self.l_starter_duty,) = self.ax_duty.plot([], [], linestyle="--", label="Starter Duty")
        self.ax_duty.set_ylabel("Duty")
        self.ax_duty.legend(loc="upper center")

        self.ax_cur = self.ax.twinx()
        self.ax_cur.spines["right"].set_position(("outward", 55))
        (self.l_pump_cur,) = self.ax_cur.plot([], [], linestyle=":", label="Pump Current")
        (self.l_starter_cur,) = self.ax_cur.plot([], [], linestyle=":", label="Starter Current")
        self.ax_cur.set_ylabel("Current (A)")
        self.ax_cur.legend(loc="upper right")

        # bottom: PSU V/I
        (self.l_psu_v,) = self.ax_psu.plot([], [], label="PSU Vout")
        self.ax_psu.set_ylabel("V")
        self.ax_psu.set_xlabel("t (s)")
        self.ax_psu.grid(True)
        self.ax_psu.legend(loc="upper left")

        self.ax_psu_i = self.ax_psu.twinx()
        (self.l_psu_i,) = self.ax_psu_i.plot([], [], linestyle="--", label="PSU Iout")
        self.ax_psu_i.set_ylabel("A")
        self.ax_psu_i.legend(loc="upper right")

        fig.tight_layout()

        # ---------- tabs
        self.tabs = QTabWidget()
        self.tab_manual = QWidget()
        self.tab_cycle = QWidget()
        self.tabs.addTab(self.tab_manual, "Manual")
        self.tabs.addTab(self.tab_cycle, "Cyclogram")

        self._build_manual_tab()
        self._build_cycle_tab()

        # ---------- status row
        self.lbl_stage = QLabel("stage: -")
        self.lbl_log = QLabel("log: -")

        root = QVBoxLayout()
        root.addWidget(self.canvas, stretch=3)
        root.addWidget(self.tabs, stretch=2)
        self.tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        st = QHBoxLayout()
        st.addWidget(self.lbl_stage)
        st.addWidget(self.lbl_log)
        st.addStretch(1)
        root.addLayout(st)

        self.setLayout(root)

        self.refresh_ports()

    # ---------------- UI BUILDERS
    def _vesc_group(self, title: str):
        g = QGroupBox(title)
        l = QVBoxLayout()

        # Row1: port + lamp + connect/disconnect + RPM label
        row1 = QHBoxLayout()
        lamp = Lamp()
        cb = QComboBox()

        rpm_live = QLabel("0 rpm")
        rpm_live.setStyleSheet("color: #c00000; font-weight: bold; font-size: 16px;")
        rpm_live.setFixedWidth(100)

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

        # Row2: pole pairs + duty + rpm + buttons
        row2 = QHBoxLayout()
        pp = QLineEdit("3")
        pp.setFixedWidth(60)

        duty = QLineEdit("0.0")
        rpm = QLineEdit("0")

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
        row2.addStretch(1)
        l.addLayout(row2)

        g.setLayout(l)
        return g, cb, lamp, rpm_live, pp, duty, rpm, btn_c, btn_d, btn_set_d, btn_set_r, btn_stop

    def _build_manual_tab(self):
        layout = QVBoxLayout()

        self.grp_pump, self.cb_pump, self.lamp_pump, self.lbl_pump_rpm_live, self.pp_pump, \
            self.in_pump_duty, self.in_pump_rpm, self.btn_pump_c, self.btn_pump_d, \
            self.btn_pump_set_d, self.btn_pump_set_r, self.btn_pump_stop = self._vesc_group("Pump VESC")

        self.grp_starter, self.cb_starter, self.lamp_starter, self.lbl_starter_rpm_live, self.pp_starter, \
            self.in_starter_duty, self.in_starter_rpm, self.btn_starter_c, self.btn_starter_d, \
            self.btn_starter_set_d, self.btn_starter_set_r, self.btn_starter_stop = self._vesc_group("Starter VESC")

        # PSU group
        self.grp_psu = QGroupBox("PSU (RD6024 via riden)")
        lpsu = QVBoxLayout()
        r1 = QHBoxLayout()
        self.cb_psu = QComboBox()
        r1.addWidget(QLabel("COM:"))
        r1.addWidget(self.cb_psu)
        self.btn_psu_c = QPushButton("Connect")
        self.btn_psu_d = QPushButton("Disconnect")
        self.lamp_psu = Lamp()
        self.lbl_psu_live = QLabel("0.0V / 0.0A")
        self.lbl_psu_live.setStyleSheet("color: #c00000; font-weight: bold; font-size: 16px;")
        self.lbl_psu_live.setFixedWidth(100)



        r1.addWidget(self.lamp_psu)
        r1.addWidget(self.btn_psu_c)
        r1.addWidget(self.btn_psu_d)
        r1.addStretch(1)
        r1.addWidget(QLabel("V/I:"))
        r1.addWidget(self.lbl_psu_live)
        lpsu.addLayout(r1)

        r2 = QHBoxLayout()
        self.in_psu_v = QLineEdit("24.0")
        self.in_psu_i = QLineEdit("5.0")
        r2.addWidget(QLabel("V:"))
        r2.addWidget(self.in_psu_v)
        r2.addWidget(QLabel("I:"))
        r2.addWidget(self.in_psu_i)
        self.btn_psu_set = QPushButton("Set V/I")
        self.btn_psu_on = QPushButton("Output ON")
        self.btn_psu_off = QPushButton("Output OFF")
        r2.addWidget(self.btn_psu_set)
        r2.addWidget(self.btn_psu_on)
        r2.addWidget(self.btn_psu_off)
        lpsu.addLayout(r2)

        self.grp_psu.setLayout(lpsu)

        # Session buttons
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

        # wiring
        self.btn_pump_c.clicked.connect(lambda: self.ctrl.connect_pump(self.cb_pump.currentText()))
        self.btn_pump_d.clicked.connect(self.ctrl.disconnect_pump)
        self.btn_starter_d.clicked.connect(self.ctrl.disconnect_starter)
        self.btn_psu_d.clicked.connect(self.ctrl.disconnect_psu)
        self.btn_pump_stop.clicked.connect(lambda: self.ctrl.set_pump_duty(0.0))

        self.btn_starter_c.clicked.connect(lambda: self.ctrl.connect_starter(self.cb_starter.currentText()))
        self.btn_starter_d.clicked.connect(self.ctrl.disconnect_all)
        self.btn_starter_set_d.clicked.connect(self._set_starter_duty)
        self.btn_starter_set_r.clicked.connect(self._set_starter_rpm)
        self.btn_starter_stop.clicked.connect(lambda: self.ctrl.set_starter_duty(0.0))

        self.btn_psu_c.clicked.connect(lambda: self.ctrl.connect_psu(self.cb_psu.currentText()))
        self.btn_psu_d.clicked.connect(self.ctrl.disconnect_all)
        self.btn_psu_set.clicked.connect(self._psu_set_vi)
        self.btn_psu_on.clicked.connect(lambda: self.ctrl.psu_output(True))
        self.btn_psu_off.clicked.connect(lambda: self.ctrl.psu_output(False))

        self.btn_ready.clicked.connect(lambda: self.ctrl.ready("manual"))
        self.btn_update.clicked.connect(self._update_reset)
        self.btn_stop_all.clicked.connect(self.ctrl.stop_all)

    def _build_cycle_tab(self):
        layout = QVBoxLayout()

        # Top controls
        row = QHBoxLayout()
        self.btn_ready2 = QPushButton("Ready")
        self.btn_run = QPushButton("Run")
        self.btn_cooling = QPushButton("Cooling")
        self.btn_update2 = QPushButton("Update")
        row.addWidget(self.btn_ready2)
        row.addWidget(self.btn_run)
        row.addWidget(self.btn_cooling)
        row.addWidget(self.btn_update2)
        row.addStretch(1)
        layout.addLayout(row)

        # Product / status
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

        # Ports + connect/disconnect (compact)
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

        self.tab_cycle.setLayout(layout)

        # wiring (cycle tab)
        self.btn_ready2.clicked.connect(lambda: self.ctrl.ready(self.in_product.text().strip() or "cycle"))
        self.btn_run.clicked.connect(self.ctrl.run_cycle)
        self.btn_cooling.clicked.connect(self.ctrl.cooling_cycle)
        self.btn_update2.clicked.connect(self._update_reset)

        self.btn_pump2_c.clicked.connect(lambda: self.ctrl.connect_pump(self.cb_pump2.currentText()))
        self.btn_pump2_d.clicked.connect(self.ctrl.disconnect_pump)
        self.btn_starter2_c.clicked.connect(lambda: self.ctrl.connect_starter(self.cb_starter2.currentText()))
        self.btn_starter2_d.clicked.connect(self.ctrl.disconnect_starter)
        self.btn_psu2_c.clicked.connect(lambda: self.ctrl.connect_psu(self.cb_psu2.currentText()))
        self.btn_psu2_d.clicked.connect(self.ctrl.disconnect_psu)

    # ---------------- actions
    def refresh_ports(self):
        ports = self.ctrl.list_ports()

        combos = [self.cb_pump, self.cb_starter, self.cb_psu]
        # якщо cyclogram tab вже створився
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
            self.ctrl.pole_pairs_pump = int(float(self.pp_pump.text()))
        except Exception:
            self.ctrl.pole_pairs_pump = 1
        try:
            self.ctrl.pole_pairs_starter = int(float(self.pp_starter.text()))
        except Exception:
            self.ctrl.pole_pairs_starter = 1

    def _set_pump_duty(self):
        self._apply_pole_pairs()
        self.ctrl.set_pump_duty(float(self.in_pump_duty.text()))

    def _set_starter_duty(self):
        self._apply_pole_pairs()
        self.ctrl.set_starter_duty(float(self.in_starter_duty.text()))

    def _set_pump_rpm(self):
        self._apply_pole_pairs()
        self.ctrl.set_pump_rpm(float(self.in_pump_rpm.text()))

    def _set_starter_rpm(self):
        self._apply_pole_pairs()
        self.ctrl.set_starter_rpm(float(self.in_starter_rpm.text()))

    def _psu_set_vi(self):
        self.ctrl.psu_set_vi(float(self.in_psu_v.text()), float(self.in_psu_i.text()))

    def _update_reset(self):
        self.ctrl.update_reset()
        self.t.clear()
        self.pump_rpm.clear();
        self.starter_rpm.clear()
        self.pump_duty.clear();
        self.starter_duty.clear()
        self.pump_cur.clear();
        self.starter_cur.clear()
        self.psu_v.clear();
        self.psu_i.clear()
        self.stage.clear()
        self._redraw()

    # ---------------- plot update
    def on_sample(self, s: dict):
        t = float(s.get("t", 0.0))
        self.lbl_stage.setText(f"stage: {s.get('stage', '-')}")

        pump = s.get("pump", {})
        starter = s.get("starter", {})
        psu = s.get("psu", {})

        self.t.append(t)
        self.stage.append(s.get("stage", ""))

        self.pump_rpm.append(float(pump.get("rpm_mech", 0.0)))
        self.starter_rpm.append(float(starter.get("rpm_mech", 0.0)))

        self.pump_duty.append(float(pump.get("duty", 0.0)))
        self.starter_duty.append(float(starter.get("duty", 0.0)))

        self.pump_cur.append(float(pump.get("current_motor", 0.0)))
        self.starter_cur.append(float(starter.get("current_motor", 0.0)))

        self.psu_v.append(float(psu.get("v_out", 0.0)))
        self.psu_i.append(float(psu.get("i_out", 0.0)))

        psu_v = float(psu.get("v_out", 0.0))
        psu_i = float(psu.get("i_out", 0.0))
        self.lbl_psu_live.setText(f"{psu_v:.1f}V / {psu_i:.2f}A")

        # keep last 120s
        while self.t and (self.t[-1] - self.t[0] > self.time_WINDOW_S ):
            for arr in (self.t, self.stage, self.pump_rpm, self.starter_rpm, self.pump_duty, self.starter_duty,
                        self.pump_cur, self.starter_cur, self.psu_v, self.psu_i):
                arr.pop(0)

        conn = s.get("connected", {})
        pump_on = bool(conn.get("pump", False))
        starter_on = bool(conn.get("starter", False))
        psu_on = bool(conn.get("psu", False))

        self.lamp_pump.set_on(pump_on)
        self.lamp_starter.set_on(starter_on)
        self.lamp_psu.set_on(psu_on)

        self.lbl_pump_rpm_live.setText(f"{self.pump_rpm[-1]:.0f} rpm")
        self.lbl_starter_rpm_live.setText(f"{self.starter_rpm[-1]:.0f} rpm")

        if hasattr(self, "lamp_pump2"):
            self.lamp_pump2.set_on(pump_on)
            self.lamp_starter2.set_on(starter_on)
            self.lamp_psu2.set_on(psu_on)

        if hasattr(self, "lbl_cycle_stage"):
            self.lbl_cycle_stage.setText(f"stage: {s.get('stage', '-')}")


        self._redraw()

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
        tmin = max(0.0, tmax - self.time_WINDOW_S)

        self.ax.set_xlim(tmin, tmax)
        self.ax.relim();
        self.ax.autoscale_view(True, True, True)
        self.ax_duty.relim();
        self.ax_duty.autoscale_view(True, True, True)
        self.ax_cur.relim();
        self.ax_cur.autoscale_view(True, True, True)

        self.ax_psu.set_xlim(tmin, tmax)
        self.ax_psu.relim();
        self.ax_psu.autoscale_view(True, True, True)
        self.ax_psu_i.relim();
        self.ax_psu_i.autoscale_view(True, True, True)

        self.canvas.draw_idle()

    def on_status(self, st: dict):
        if st.get("ready"):
            self.lbl_log.setText(f"log: {st.get('log_path', '-')}")

    def closeEvent(self, event):
        try:
            self.ctrl.shutdown()
        except Exception:
            pass
        super().closeEvent(event)
