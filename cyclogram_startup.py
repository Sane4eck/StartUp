# cyclogram_startup.py
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from cycle_fsm import CycleFSM, CycleInputs, CycleTargets, State, Transition, Hold
from pump_profile import PumpProfile, interp_profile


# ============================
# 1) НАЛАШТУВАННЯ (ТЕ, ЩО ТИ МІНЯЄШ НАЙЧАСТІШЕ)
# ============================
@dataclass
class StartupConfig:
    # --- Starter
    starter_duty_start: float = 0.05
    starter_timeout_s: float = 10.0
    starter_min_rpm: float = 500.0
    starter_min_hold_s: float = 0.15   # щоб rpm трималось, а не "пікнуло"

    # --- Ignition
    ignition_timeout_s: float = 20.0
    valve_v1: float = 18.0
    valve_v2: float = 24.0
    valve_i: float = 5.0
    valve_switch_s: float = 1.0

    # стартер під час Ignition: маленька циклограма duty
    starter_duty_profile_ign: List[Tuple[float, float]] = None

    # умова завершення Ignition (приклад):
    ignition_min_starter_rpm: float = 2000.0
    ignition_hold_s: float = 0.20

    # --- FuelRamp
    fuelramp_timeout_s: float = 20.0
    fuelramp_finish_starter_rpm: float = 20000.0
    fuelramp_finish_hold_s: float = 0.30

    # --- Running
    running_starter_duty: float = 0.075
    # В Running насосом керує оператор вручну.

    def __post_init__(self):
        if self.starter_duty_profile_ign is None:
            self.starter_duty_profile_ign = [
                (0.0, 0.05),
                (2.0, 0.055),
                (4.0, 0.06),
                (6.0, 0.065),
            ]
@dataclass
class CoolingConfig:
    duration_s: float = 8.0

# ============================
# 2) ДОПОМІЖНІ ФУНКЦІЇ (зрозумілі команди)
# ============================
def set_pump_rpm(out: CycleTargets, rpm: float):
    out.pump = {"mode": "rpm", "value": float(rpm)}

def set_starter_duty(out: CycleTargets, duty: float):
    out.starter = {"mode": "duty", "value": float(duty)}

def set_valve(out: CycleTargets, v: float, i: float, on: bool):
    out.psu = {"v": float(v), "i": float(i), "out": bool(on)}

def stop_all(out: CycleTargets):
    set_pump_rpm(out, 0.0)
    set_starter_duty(out, 0.0)
    set_valve(out, 0.0, 0.0, False)

def interp_time_value(points: List[Tuple[float, float]], t: float) -> float:
    """лінійна інтерполяція (час->значення)"""
    if not points:
        return 0.0
    x = float(t)
    if x <= points[0][0]:
        return float(points[0][1])
    if x >= points[-1][0]:
        return float(points[-1][1])
    for k in range(1, len(points)):
        if x <= points[k][0]:
            t0, t1 = float(points[k-1][0]), float(points[k][0])
            y0, y1 = float(points[k-1][1]), float(points[k][1])
            if t1 <= t0:
                return y1
            a = (x - t0) / (t1 - t0)
            return y0 + a * (y1 - y0)
    return float(points[-1][1])


# ============================
# 3) ПОБУДОВА FSM
# ============================
def build_startup_fsm(profile: PumpProfile, cfg: StartupConfig | None = None) -> CycleFSM:
    cfg = cfg or StartupConfig()

    # ----------------------------
    # 3.1 УМОВИ (ТУТ ТИ ДОДАЄШ/ПРАВИШ УМОВИ)
    # ----------------------------

    # Starter -> Ignition: стартер має досягти 500 rpm і потримати 0.15с
    starter_ready = Hold(lambda i: i.starter_rpm >= cfg.starter_min_rpm, cfg.starter_min_hold_s)

    # Ignition -> FuelRamp: приклад умови (можеш замінити на іншу):
    ignition_ready = Hold(lambda i: i.starter_rpm >= cfg.ignition_min_starter_rpm, cfg.ignition_hold_s)

    # FuelRamp -> Running: закінчився профіль насоса + стартер >= 20000 rpm (потримати)
    ramp_done_time = lambda i: i.state_t >= profile.end_time
    ramp_ready = Hold(lambda i: i.starter_rpm >= cfg.fuelramp_finish_starter_rpm, cfg.fuelramp_finish_hold_s)

    def go_running(i: CycleInputs) -> bool:
        return ramp_done_time(i) and ramp_ready(i)

    # ----------------------------
    # 3.2 ЩО РОБИТЬ КОЖЕН СТАН
    # ----------------------------

    # Stop: все вимкнути
    def stop_enter(_i: CycleInputs, out: CycleTargets):
        stop_all(out)

    # Fault: все вимкнути + повідомлення
    def fault_enter(_i: CycleInputs, out: CycleTargets):
        stop_all(out)
        out.meta["fault"] = out.meta.get("transition_reason", "Fault")

    # Starter: стартер duty=0.05, насос 0, клапан off
    def starter_enter(_i: CycleInputs, out: CycleTargets):
        set_starter_duty(out, cfg.starter_duty_start)
        set_pump_rpm(out, 0.0)
        set_valve(out, 0.0, 0.0, False)

    # Ignition: клапан on (V1->V2), стартер по профілю, насос по профілю (підготовка)
    def ignition_enter(_i: CycleInputs, out: CycleTargets):
        set_valve(out, cfg.valve_v1, cfg.valve_i, True)

    def ignition_tick(i: CycleInputs, out: CycleTargets):
        # starter duty by profile
        d = interp_time_value(cfg.starter_duty_profile_ign, i.state_t)
        set_starter_duty(out, d)

        # valve V1->V2 by time
        v = cfg.valve_v1 if i.state_t < cfg.valve_switch_s else cfg.valve_v2
        set_valve(out, v, cfg.valve_i, True)

        # pump rpm from xlsx profile (поки що також тут)
        set_pump_rpm(out, interp_profile(profile, i.state_t))

    # FuelRamp: основний ramp насоса (з Excel), стартер тримаємо останнє duty, клапан on
    def fuelramp_enter(_i: CycleInputs, out: CycleTargets):
        # при вході нічого особливого, усе робимо в tick
        pass

    def fuelramp_tick(i: CycleInputs, out: CycleTargets):
        # valve on (V2)
        set_valve(out, cfg.valve_v2, cfg.valve_i, True)

        # starter: тримаємо останнє значення профілю (або можеш зробити окремий профіль)
        last_d = cfg.starter_duty_profile_ign[-1][1]
        set_starter_duty(out, last_d)

        # pump: по Excel профілю (це і є ramp)
        set_pump_rpm(out, interp_profile(profile, i.state_t))

    # Running: клапан on, стартер фіксовано, насос НЕ ЧІПАЄМО (оператор)
    def running_enter(_i: CycleInputs, out: CycleTargets):
        set_valve(out, cfg.valve_v2, cfg.valve_i, True)
        set_starter_duty(out, cfg.running_starter_duty)
        # спеціальна мітка: в worker насос не перезаписується в Running
        out.meta["running_manual_pump"] = True

    def running_tick(_i: CycleInputs, out: CycleTargets):
        set_valve(out, cfg.valve_v2, cfg.valve_i, True)
        set_starter_duty(out, cfg.running_starter_duty)
        # насос не задаємо

    # ----------------------------
    # 3.3 ПЕРЕХОДИ (ПОСЛІДОВНО, НЕ МОЖНА ПРОПУСТИТИ)
    # ----------------------------
    states = {
        "Stop": State("Stop", on_enter=stop_enter, terminal=True),
        "Fault": State("Fault", on_enter=fault_enter, terminal=True),

        "Starter": State(
            "Starter",
            on_enter=starter_enter,
            transitions=[
                Transition(starter_ready, "Ignition", reason="OK: Starter reached rpm"),
            ],
            timeout_s=cfg.starter_timeout_s,
            on_timeout="Fault",
            timeout_reason=f"Stop: Starter did not reach {cfg.starter_min_rpm:.0f} rpm in {cfg.starter_timeout_s:.0f}s",
        ),

        "Ignition": State(
            "Ignition",
            on_enter=ignition_enter,
            on_tick=ignition_tick,
            transitions=[
                Transition(ignition_ready, "FuelRamp", reason="OK: Ignition condition reached"),
            ],
            timeout_s=cfg.ignition_timeout_s,
            on_timeout="Fault",
            timeout_reason=f"Stop: Ignition timeout {cfg.ignition_timeout_s:.0f}s",
        ),

        "FuelRamp": State(
            "FuelRamp",
            on_enter=fuelramp_enter,
            on_tick=fuelramp_tick,
            transitions=[
                Transition(go_running, "Running", reason="OK: FuelRamp done"),
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


# ============================
# COOLING FSM
# ============================
def build_cooling_fsm(duty: float, cfg: CoolingConfig | None = None) -> CycleFSM:
    cfg = cfg or CoolingConfig()
    duty = max(0.0, min(1.0, float(duty)))

    def cooling_enter(_i: CycleInputs, out: CycleTargets):
        # cooling: starter duty = duty, pump off, valve off
        set_starter_duty(out, duty)
        set_pump_rpm(out, 0.0)
        set_valve(out, 0.0, 0.0, False)

    def stop_enter(_i: CycleInputs, out: CycleTargets):
        stop_all(out)

    states = {
        "Cooling": State(
            "Cooling",
            on_enter=cooling_enter,
            transitions=[
                Transition(lambda i: i.state_t >= cfg.duration_s, "Stop", reason="Cooling done"),
            ],
        ),
        "Stop": State("Stop", on_enter=stop_enter, terminal=True),
    }

    return CycleFSM(states=states, initial="Cooling", stop_state="Stop")
