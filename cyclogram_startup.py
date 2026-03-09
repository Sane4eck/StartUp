# cyclogram_startup.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

from cycle_fsm import CycleFSM, CycleInputs, CycleTargets, State, Transition
from pump_profile import PumpProfile, interp_profile


# ---------- helpers ----------
def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def set_pump_rpm(out: CycleTargets, rpm: float):
    out.pump = {"mode": "rpm", "value": float(rpm)}


def set_starter_duty(out: CycleTargets, duty: float):
    out.starter = {"mode": "duty", "value": _clamp01(duty)}


def set_valve(out: CycleTargets, v: float, i: float, on: bool):
    out.psu = {"v": float(v), "i": float(i), "out": bool(on)}


def stop_all(out: CycleTargets):
    set_pump_rpm(out, 0.0)
    set_starter_duty(out, 0.0)
    set_valve(out, 0.0, 0.0, False)


def _hold_ge(mem: dict, state_t: float, value: float, thr: float, hold_s: float) -> bool:
    """True якщо value >= thr безперервно hold_s (по часу state_t)."""
    if value >= thr:
        if mem["armed_at"] is None:
            mem["armed_at"] = state_t
            return False
        return (state_t - float(mem["armed_at"])) >= float(hold_s)
    mem["armed_at"] = None
    return False


class StarterDutySchedule:
    """
    Таблиця duty по RPM, одна й та сама для Starter і FuelRamp.
    steps: [(rpm_threshold, duty), ...] у зростаючому порядку.
    Зміна на наступний крок тільки якщо rpm тримається >= threshold step_hold_s.
    """
    def __init__(self, steps: List[Tuple[float, float]], step_hold_s: float):
        self.steps = sorted([(float(r), float(d)) for r, d in steps], key=lambda x: x[0])
        self.step_hold_s = float(step_hold_s)
        self.idx = 0
        self._armed_at = None  # state_t коли rpm вперше стало >= next_threshold

    def reset_all(self):
        self.idx = 0
        self._armed_at = None

    def reset_timer_only(self):
        self._armed_at = None

    def value(self, rpm: float, state_t: float) -> float:
        if not self.steps:
            return 0.0

        # просуваємось до наступних ступенів (по черзі)
        while self.idx < len(self.steps) - 1:
            next_rpm, _next_duty = self.steps[self.idx + 1]
            if rpm >= next_rpm:
                if self._armed_at is None:
                    self._armed_at = state_t
                    break
                if (state_t - float(self._armed_at)) >= self.step_hold_s:
                    self.idx += 1
                    self._armed_at = None
                    continue
                break
            else:
                self._armed_at = None
                break

        return float(self.steps[self.idx][1])


# ---------- config ----------
@dataclass
class StartupConfig:
    # Таблиця duty стартера для ВСІХ режимів (Starter + FuelRamp)
    # Твоя базова логіка:
    # 0.05 -> (500)0.06 -> (700)0.07 -> (1000)0.08 -> (1500)0.08 -> ...
    starter_steps: List[Tuple[float, float]] = field(default_factory=lambda: [
        (0.0, 0.05),
        (500.0, 0.06),
        (700.0, 0.07),
        (1100.0, 0.08),
        (1500.0, 0.1),  # можна додавати далі
    ])
    starter_step_hold_s: float = 0.2

    # Перехід Starter -> FuelRamp (по RPM стартера)
    to_fuelramp_rpm: float = 1000.0
    to_fuelramp_hold_s: float = 0.2
    starter_timeout_s: float = 180.0

    # FuelRamp: коли starter_rpm >= 10000 (hold) -> закрити клапан і вимкнути стартер
    cutoff_starter_rpm: float = 12000.0
    cutoff_hold_s: float = 0.2

    # Running: коли pump_rpm >= 15000 (hold) -> Running, заморозити насос на поточних обертах
    running_pump_rpm: float = 21000.0
    running_hold_s: float = 0.2

    # Клапан у FuelRamp до cutoff: 18V/20A 1s -> 5V/20A
    valve_boost_v: float = 18.0
    valve_boost_i: float = 20.0
    valve_boost_s: float = 2.0
    valve_hold_v: float = 5.0
    valve_hold_i: float = 20.0

    fuelramp_timeout_s: float = 300.0


def build_startup_fsm(
    pump_profile: PumpProfile,      # _Cyclogram_Pump.xlsx (RPM)
    starter_profile: PumpProfile,   # не потрібен тут, залишено для сумісності
    cfg: StartupConfig | None = None,
) -> CycleFSM:
    cfg = cfg or StartupConfig()

    sched = StarterDutySchedule(cfg.starter_steps, cfg.starter_step_hold_s)

    # пам’ять для hold-умов у конкретних режимах
    mem_to_fuel = {"armed_at": None}
    mem_cutoff = {"armed_at": None, "latched": False}
    mem_run = {"armed_at": None}
    pump_hold = {"rpm": 0.0}

    def to_fuelramp(i: CycleInputs) -> bool:
        return _hold_ge(mem_to_fuel, i.state_t, i.starter_rpm, cfg.to_fuelramp_rpm, cfg.to_fuelramp_hold_s)

    def to_running(i: CycleInputs) -> bool:
        return _hold_ge(mem_run, i.state_t, i.pump_rpm, cfg.running_pump_rpm, cfg.running_hold_s)

    def stop_enter(_i: CycleInputs, out: CycleTargets):
        stop_all(out)

    def fault_enter(_i: CycleInputs, out: CycleTargets):
        stop_all(out)
        out.meta["fault"] = out.meta.get("transition_reason", "Fault")

    # ---------- Starter ----------
    def starter_enter(_i: CycleInputs, out: CycleTargets):
        sched.reset_all()
        mem_to_fuel["armed_at"] = None
        set_starter_duty(out, sched.value(0.0, 0.0))
        set_pump_rpm(out, 0.0)
        set_valve(out, 0.0, 0.0, False)

    def starter_tick(i: CycleInputs, out: CycleTargets):
        set_starter_duty(out, sched.value(i.starter_rpm, i.state_t))
        set_pump_rpm(out, 0.0)
        set_valve(out, 0.0, 0.0, False)

    # ---------- FuelRamp ----------
    def fuelramp_enter(_i: CycleInputs, out: CycleTargets):
        # schedule продовжуємо, але таймер ступені скидаємо (бо state_t знову з 0)
        sched.reset_timer_only()

        mem_cutoff["armed_at"] = None
        mem_cutoff["latched"] = False
        mem_run["armed_at"] = None

    def fuelramp_tick(i: CycleInputs, out: CycleTargets):
        # насос по циклограмі (поки не увійдемо в Running)
        set_pump_rpm(out, interp_profile(pump_profile, i.state_t))

        # cutoff по starter_rpm: >=10000 hold -> latch (valve off + starter off)
        if mem_cutoff["latched"] or _hold_ge(mem_cutoff, i.state_t, i.starter_rpm, cfg.cutoff_starter_rpm, cfg.cutoff_hold_s):
            mem_cutoff["latched"] = True
            set_starter_duty(out, 0.0)
            set_valve(out, 0.0, 0.0, False)
        else:
            # стартер за тією ж таблицею duty, що і в Starter
            set_starter_duty(out, sched.value(i.starter_rpm, i.state_t))

            # клапан відкритий (boost -> hold)
            if i.state_t < cfg.valve_boost_s:
                set_valve(out, cfg.valve_boost_v, cfg.valve_boost_i, True)
            else:
                set_valve(out, cfg.valve_hold_v, cfg.valve_hold_i, True)

    # ---------- Running ----------
    def running_enter(i: CycleInputs, out: CycleTargets):
        # заморозити насос на поточних обертах, де досягли 15000
        pump_hold["rpm"] = float(i.pump_rpm)
        set_pump_rpm(out, pump_hold["rpm"])

        # стартер і клапан вимкнені
        set_starter_duty(out, 0.0)
        set_valve(out, 0.0, 0.0, False)

        # важливо для worker.py: застосувати команду насоса 1 раз на вході в Running
        out.meta["apply_pump_once_on_running_entry"] = True

    def running_tick(_i: CycleInputs, out: CycleTargets):
        # якщо захочеш щоб FSM “тримав” насос — залишаємо (worker може не перезаписувати в Running)
        set_pump_rpm(out, pump_hold["rpm"])
        set_starter_duty(out, 0.0)
        set_valve(out, 0.0, 0.0, False)

    states = {
        "Stop": State("Stop", on_enter=stop_enter, terminal=True),
        "Fault": State("Fault", on_enter=fault_enter, terminal=True),

        "Starter": State(
            "Starter",
            on_enter=starter_enter,
            on_tick=starter_tick,
            transitions=[
                Transition(to_fuelramp, "FuelRamp", reason="OK: Starter reached 1000 rpm"),
            ],
            timeout_s=cfg.starter_timeout_s,
            on_timeout="Fault",
            timeout_reason=f"Stop: Starter timeout {cfg.starter_timeout_s:.0f}s",
        ),

        "FuelRamp": State(
            "FuelRamp",
            on_enter=fuelramp_enter,
            on_tick=fuelramp_tick,
            transitions=[
                Transition(to_running, "Running", reason="OK: Pump reached 15000 rpm"),
            ],
            timeout_s=cfg.fuelramp_timeout_s,
            on_timeout="Fault",
            timeout_reason=f"Stop: FuelRamp timeout {cfg.fuelramp_timeout_s:.0f}s",
        ),

        "Running": State(
            "Running",
            on_enter=running_enter,
            on_tick=running_tick,
        ),
    }

    return CycleFSM(states=states, initial="Starter", stop_state="Stop")


def build_cooling_fsm(duty: float, duration_s: float = 8.0) -> CycleFSM:
    duty = _clamp01(duty)

    def cooling_enter(_i: CycleInputs, out: CycleTargets):
        set_starter_duty(out, duty)
        set_pump_rpm(out, 0.0)
        set_valve(out, 0.0, 0.0, False)

    def stop_enter(_i: CycleInputs, out: CycleTargets):
        stop_all(out)

    states = {
        "Cooling": State(
            "Cooling",
            on_enter=cooling_enter,
            transitions=[Transition(lambda i: i.state_t >= duration_s, "Stop", reason="Cooling done")],
        ),
        "Stop": State("Stop", on_enter=stop_enter, terminal=True),
    }
    return CycleFSM(states=states, initial="Cooling", stop_state="Stop")
