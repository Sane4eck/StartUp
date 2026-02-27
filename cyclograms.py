# cyclograms.py
from __future__ import annotations

from dataclasses import dataclass

from cycle_fsm import CycleFSM, CycleInputs, CycleTargets, State, Transition, Hold


@dataclass
class StartupCfg:
    # Starter
    starter_mode: str = "rpm"
    starter_target_duty: float = 0.55
    starter_ready_rpm: float = 700.0
    starter_ready_hold_s: float = 0.30
    starter_timeout_s: float = 6.0

    # Ignition
    valve_v_1: float = 15.0
    valve_i_1: float = 20.0
    valve_v_2: float = 5.0
    valve_i_2: float = 20.0
    ignition_pump_rpm: float = 1200.0
    ignition_min_time_s: float = 0.50
    ignition_timeout_s: float = 4.0

    # FuelRamp (automatic pump control as function of starter rpm + time)
    ramp_duration_s: float = 4.0
    pump_rpm_start: float = 1200.0
    pump_rpm_target: float = 5000.0
    pump_rpm_limit_by_starter_ratio: float = 1.2   # pump_cmd <= ratio * starter_rpm
    ramp_timeout_s: float = 8.0

    # Running
    running_pump_rpm: float = 5000.0
    running_starter_rpm: float = 3000.0
    running_hold_s: float = 0.40

    # Fault thresholds
    min_starter_rpm_in_run: float = 1500.0
    drop_hold_s: float = 0.30


@dataclass
class CoolingCfg:
    valve_off: bool = True
    cool_starter_duty: float = 0.055
    cool_duration_s: float = 10.0


def build_startup_fsm(cfg: StartupCfg | None = None) -> CycleFSM:
    cfg = cfg or StartupCfg()

    def stop_outputs(_inp: CycleInputs, out: CycleTargets):
        out.pump = {"mode": "duty", "value": 0.0}
        out.starter = {"mode": "duty", "value": 0.0}
        out.psu = {"v": 0.0, "i": 0.0, "out": False}

    def fault_outputs(_inp: CycleInputs, out: CycleTargets):
        stop_outputs(_inp, out)

    def starter_enter(_inp: CycleInputs, out: CycleTargets):
        out.pump = {"mode": "duty", "value": 0.0}
        out.psu = {"v": 0.0, "i": 0.0, "out": False}
        out.starter = {"mode": cfg.starter_mode, "value": cfg.starter_target_rpm}

    def ignition_enter(_inp: CycleInputs, out: CycleTargets):
        out.psu = {"v": cfg.valve_v, "i": cfg.valve_i, "out": True}
        out.starter = {"mode": cfg.starter_mode, "value": cfg.starter_target_rpm}
        out.pump = {"mode": "rpm", "value": cfg.ignition_pump_rpm}

    def ramp_tick(inp: CycleInputs, out: CycleTargets):
        # time ramp: pump_rpm_start -> pump_rpm_target over ramp_duration
        a = min(1.0, max(0.0, inp.state_t / max(0.1, cfg.ramp_duration_s)))
        cmd = cfg.pump_rpm_start + a * (cfg.pump_rpm_target - cfg.pump_rpm_start)

        # dependency on starter rpm
        cmd_limit = cfg.pump_rpm_limit_by_starter_ratio * max(0.0, inp.starter_rpm)
        cmd = min(cmd, cmd_limit)

        out.psu = {"v": cfg.valve_v, "i": cfg.valve_i, "out": True}
        out.starter = {"mode": cfg.starter_mode, "value": cfg.starter_target_rpm}
        out.pump = {"mode": "rpm", "value": cmd}

    def running_tick(inp: CycleInputs, out: CycleTargets):
        # keep running; optionally keep pump limited by starter rpm
        cmd_limit = cfg.pump_rpm_limit_by_starter_ratio * max(0.0, inp.starter_rpm)
        pump_cmd = min(cfg.running_pump_rpm, cmd_limit)

        out.psu = {"v": cfg.valve_v, "i": cfg.valve_i, "out": True}
        out.starter = {"mode": cfg.starter_mode, "value": cfg.running_starter_rpm}
        out.pump = {"mode": "rpm", "value": pump_cmd}

    starter_ready = Hold(lambda i: i.starter_rpm >= cfg.starter_ready_rpm, cfg.starter_ready_hold_s)
    running_ready = Hold(
        lambda i: (i.starter_rpm >= 0.98 * cfg.running_starter_rpm) and (i.pump_rpm >= 0.98 * cfg.running_pump_rpm),
        cfg.running_hold_s
    )
    drop_fault = Hold(lambda i: i.starter_rpm < cfg.min_starter_rpm_in_run, cfg.drop_hold_s)

    states = {
        "Stop": State("Stop", on_enter=stop_outputs),
        "Fault": State("Fault", on_enter=fault_outputs),

        "Starter": State(
            "Starter",
            on_enter=starter_enter,
            transitions=[Transition(starter_ready, "Ignition")],
            timeout_s=cfg.starter_timeout_s,
            on_timeout="Fault",
        ),

        "Ignition": State(
            "Ignition",
            on_enter=ignition_enter,
            transitions=[Transition(lambda i: i.state_t >= cfg.ignition_min_time_s, "FuelRamp")],
            timeout_s=cfg.ignition_timeout_s,
            on_timeout="Fault",
        ),

        "FuelRamp": State(
            "FuelRamp",
            on_tick=ramp_tick,
            transitions=[Transition(running_ready, "Running")],
            timeout_s=cfg.ramp_timeout_s,
            on_timeout="Fault",
        ),

        "Running": State(
            "Running",
            on_tick=running_tick,
            transitions=[Transition(drop_fault, "Fault")],
        ),
    }

    return CycleFSM(states=states, initial="Starter", stop_state="Stop")


def build_cooling_fsm(cfg: CoolingCfg | None = None) -> CycleFSM:
    cfg = cfg or CoolingCfg()

    def cooling_enter(_inp: CycleInputs, out: CycleTargets):
        out.pump = {"mode": "rpm", "value": 0.0}
        out.starter = {"mode": "duty", "value": cfg.cool_starter_duty}
        out.psu = {"v": 0.0, "i": 0.0, "out": False}

    def stop_outputs(_inp: CycleInputs, out: CycleTargets):
        out.pump = {"mode": "duty", "value": 0.0}
        out.starter = {"mode": "duty", "value": 0.0}
        out.psu = {"v": 0.0, "i": 0.0, "out": False}

    states = {
        "Cooling": State(
            "Cooling",
            on_enter=cooling_enter,
            transitions=[Transition(lambda i: i.state_t >= cfg.cool_duration_s, "Stop")],
        ),
        "Stop": State("Stop", on_enter=stop_outputs, terminal=True),
    }
    return CycleFSM(states=states, initial="Cooling", stop_state="Stop")
