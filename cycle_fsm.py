# cycle_fsm.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Any


@dataclass
class CycleInputs:
    now: float
    t: float           # time since session start
    state_t: float     # time since current state entered

    pump_rpm: float
    starter_rpm: float
    pump_current: float
    starter_current: float

    psu_v_out: float
    psu_i_out: float
    psu_output: bool


@dataclass
class CycleTargets:
    # IMPORTANT: pump = RPM only, starter = DUTY only
    pump: Dict[str, Any] = field(default_factory=lambda: {"mode": "rpm", "value": 0.0})
    starter: Dict[str, Any] = field(default_factory=lambda: {"mode": "duty", "value": 0.0})
    psu: Dict[str, Any] = field(default_factory=lambda: {"v": 0.0, "i": 0.0, "out": False})

    # for messages/fault reasons etc.
    meta: Dict[str, Any] = field(default_factory=dict)


class Hold:
    """Predicate must be True continuously for hold_s seconds."""
    def __init__(self, predicate: Callable[[CycleInputs], bool], hold_s: float):
        self.predicate = predicate
        self.hold_s = float(hold_s)
        self._t0: Optional[float] = None

    def reset(self):
        self._t0 = None

    def __call__(self, inp: CycleInputs) -> bool:
        if self.predicate(inp):
            if self._t0 is None:
                self._t0 = inp.now
            return (inp.now - self._t0) >= self.hold_s
        self._t0 = None
        return False


@dataclass
class Transition:
    cond: Callable[[CycleInputs], bool]
    next_state: str
    reason: Optional[str] = None


@dataclass
class State:
    name: str
    on_enter: Optional[Callable[[CycleInputs, CycleTargets], None]] = None
    on_tick: Optional[Callable[[CycleInputs, CycleTargets], None]] = None
    transitions: List[Transition] = field(default_factory=list)

    timeout_s: Optional[float] = None
    on_timeout: Optional[str] = None
    timeout_reason: Optional[str] = None

    terminal: bool = False


class CycleFSM:
    def __init__(self, states: Dict[str, State], initial: str, stop_state: str = "Stop"):
        self.states = states
        self.initial = initial
        self.stop_state = stop_state

        self.running: bool = False
        self.current: str = initial
        self._state_enter_time: float = 0.0

        self.targets = CycleTargets()

        self.last_state: Optional[str] = None
        self.last_transition_reason: Optional[str] = None

    @property
    def state(self) -> str:
        return self.current

    def start(self, inp: CycleInputs):
        self.running = True
        self._switch(inp, self.initial, reason=None)

    def stop(self, inp: CycleInputs, reason: str | None = None):
        self.running = False
        self._switch(inp, self.stop_state, reason=reason)

    def tick(self, inp: CycleInputs) -> CycleTargets:
        # clear per-tick meta
        self.targets.meta.clear()
        self.last_transition_reason = None

        st = self.states[self.current]

        if st.on_tick:
            st.on_tick(inp, self.targets)

        # timeout
        if st.timeout_s is not None and st.on_timeout:
            if inp.state_t >= float(st.timeout_s):
                self._switch(inp, st.on_timeout, reason=st.timeout_reason)
                st = self.states[self.current]

        # transitions
        for tr in st.transitions:
            if tr.cond(inp):
                self._switch(inp, tr.next_state, reason=tr.reason)
                st = self.states[self.current]
                break

        if st.terminal:
            self.running = False

        return self.targets

    def _switch(self, inp: CycleInputs, next_state: str, reason: Optional[str]):
        self.last_state = self.current
        self.current = next_state
        self._state_enter_time = inp.now
        self.last_transition_reason = reason

        # reset Hold conditions for new state's transitions
        st = self.states[self.current]
        for tr in st.transitions:
            if hasattr(tr.cond, "reset"):
                try:
                    tr.cond.reset()
                except Exception:
                    pass

        if reason:
            self.targets.meta["transition_reason"] = reason

        if st.on_enter:
            st.on_enter(inp, self.targets)

    def state_time(self, now: float) -> float:
        return now - self._state_enter_time
