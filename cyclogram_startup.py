# cyclogram_startup.py
from __future__ import annotations

from dataclasses import dataclass

from cycle_fsm import CycleFSM, CycleInputs, CycleTargets, State, Transition, Hold
from pump_profile import PumpProfile, interp_profile


# =========================================================
# ЦИКЛОГРАМА: Starter -> FuelRamp -> Running
# FuelRamp керує:
#   - насос: RPM з _Cyclogram_Pump.xlsx
#   - стартер: DUTY з _Cyclogram_Starter.xlsx
#   - клапан: 15V 2s -> 5V до 12000 rpm -> 0V
# Running:
#   - насос: ручне керування оператором
#   - стартер: тримаємо останнє значення з профілю або константу
#   - клапан: 0V (off)
# =========================================================


@dataclass
class StartupConfig:
    # -------- Starter
    starter_duty_start: float = 0.055
    starter_timeout_s: float = 15.0
    starter_min_rpm: float = 900.0
    starter_min_hold_s: float = 0.2

    # -------- Valve behavior in FuelRamp (твоя вимога)
    valve_i: float = 20.0              # струм PSU для клапана (A)
    valve_v_high: float = 18.0        # перші 1 секунди
    valve_high_time_s: float = 2.0
    valve_v_hold: float = 5.0         # після 1с до порогу rpm
    valve_rpm_threshold: float = 21000.0  # після досягнення -> 0V/off

    # -------- FuelRamp
    fuelramp_timeout_s: float = 120.0  # safety таймаут
    # Перехід FuelRamp->Running: закінчення профілю насоса (див. pump_profile_done)

    # -------- Running (нескінченний)
    running_starter_use_const: bool = True
    running_starter_duty_const: float = 0.00


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


def build_startup_fsm(
    pump_profile: PumpProfile,          # _Cyclogram_Pump.xlsx (value = RPM)
    starter_profile: PumpProfile,       # _Cyclogram_Starter.xlsx (value = DUTY 0..1)
    cfg: StartupConfig | None = None,
) -> CycleFSM:
    cfg = cfg or StartupConfig()

    # ---------------- CONDITIONS
    starter_ready = Hold(lambda i: i.starter_rpm >= cfg.starter_min_rpm, cfg.starter_min_hold_s)

    def pump_profile_done(i: CycleInputs) -> bool:
        # УМОВА переходу FuelRamp -> Running: профіль насоса закінчився
        return i.state_t >= pump_profile.end_time

    # ---------------- STATE BEHAVIOR
    def stop_enter(_i: CycleInputs, out: CycleTargets):
        stop_all(out)

    def fault_enter(_i: CycleInputs, out: CycleTargets):
        stop_all(out)
        out.meta["fault"] = out.meta.get("transition_reason", "Fault")

    # ---------- Starter
    def starter_enter(_i: CycleInputs, out: CycleTargets):
        set_starter_duty(out, cfg.starter_duty_start)
        set_pump_rpm(out, 0.0)
        set_valve(out, 0.0, 0.0, False)

    def starter_tick(_i: CycleInputs, out: CycleTargets):
        set_starter_duty(out, cfg.starter_duty_start)
        set_pump_rpm(out, 0.0)
        set_valve(out, 0.0, 0.0, False)

    # ---------- FuelRamp (єдиний режим для ramp + логіка клапана)
    def fuelramp_enter(_i: CycleInputs, out: CycleTargets):
        # нічого особливого — керування в tick
        pass

    def fuelramp_tick(i: CycleInputs, out: CycleTargets):
        # 1) стартер DUTY з профілю
        duty_cmd = interp_profile(starter_profile, i.state_t)
        set_starter_duty(out, duty_cmd)

        # 2) насос RPM з профілю
        rpm_cmd = interp_profile(pump_profile, i.state_t)
        set_pump_rpm(out, rpm_cmd)

        # 3) клапан (твоя логіка):
        #    - 0..2с: 15V
        #    - після 2с: 5V доки starter_rpm < 12000
        #    - коли starter_rpm >= 12000: 0V (off)
        if i.state_t < cfg.valve_high_time_s:
            set_valve(out, cfg.valve_v_high, cfg.valve_i, True)
        else:
            if i.starter_rpm < cfg.valve_rpm_threshold:
                set_valve(out, cfg.valve_v_hold, cfg.valve_i, True)
            else:
                set_valve(out, 0.0, 0.0, False)

    # ---------- Running (нескінченний)
    def running_enter(_i: CycleInputs, out: CycleTargets):
        # клапан завжди off у Running
        set_valve(out, 0.0, 0.0, False)

        # стартер тримаємо
        if cfg.running_starter_use_const:
            set_starter_duty(out, cfg.running_starter_duty_const)
        else:
            # останнє значення зі стартового профілю
            last = starter_profile.rpm[-1] if starter_profile.rpm else 0.0
            set_starter_duty(out, last)

        # насос НЕ задаємо (оператор керує вручну)
        out.meta["running_manual_pump"] = True

    def running_tick(_i: CycleInputs, out: CycleTargets):
        set_valve(out, 0.0, 0.0, False)
        if cfg.running_starter_use_const:
            set_starter_duty(out, cfg.running_starter_duty_const)
        else:
            last = starter_profile.rpm[-1] if starter_profile.rpm else 0.0
            set_starter_duty(out, last)
        # pump not set

    # ---------------- STATES + TRANSITIONS
    states = {
        "Stop": State("Stop", on_enter=stop_enter, terminal=True),
        "Fault": State("Fault", on_enter=fault_enter, terminal=True),

        "Starter": State(
            "Starter",
            on_enter=starter_enter,
            on_tick=starter_tick,
            transitions=[
                Transition(starter_ready, "FuelRamp", reason="OK: Starter reached rpm"),
            ],
            timeout_s=cfg.starter_timeout_s,
            on_timeout="Fault",
            timeout_reason=f"Stop: Starter did not reach {cfg.starter_min_rpm:.0f} rpm in {cfg.starter_timeout_s:.0f}s",
        ),

        "FuelRamp": State(
            "FuelRamp",
            on_enter=fuelramp_enter,
            on_tick=fuelramp_tick,
            transitions=[
                Transition(pump_profile_done, "Running", reason="OK: Pump profile finished"),
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


def build_cooling_fsm(duty: float, duration_s: float = 500.0) -> CycleFSM:
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
            transitions=[
                Transition(lambda i: i.state_t >= duration_s, "Stop", reason="Cooling done"),
            ],
        ),
        "Stop": State("Stop", on_enter=stop_enter, terminal=True),
    }
    return CycleFSM(states=states, initial="Cooling", stop_state="Stop")
