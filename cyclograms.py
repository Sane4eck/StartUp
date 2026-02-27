# cyclograms.py

# ----- STARTER -> Ignition 조건
STARTER_DUTY = 0.05
STARTER_WAIT_S = 10.0
STARTER_MIN_RPM = 500.0

# ----- IGNITION
STARTER_IGNITION_DUTY_PROFILE = [
    # (time_s, duty)
    (0.0, 0.05),
    (2.0, 0.055),
    (4.0, 0.060),
    (6.0, 0.065),
    (8.0, 0.070),
    (10.0, 0.075),
]
IGNITION_TARGET_STARTER_RPM = 20000.0

VALVE_V_1 = 18.0
VALVE_V_2 = 24.0
VALVE_I = 5.0
VALVE_SWITCH_S = 1.0  # after this time: V1->V2

PUMP_PROFILE_XLSX = "_Cyclogramm.xlsx"
PUMP_PROFILE_SHEET = None  # or sheet name string

# safety timeout for ignition total
IGNITION_TIMEOUT_S = 30.0

# ----- RUNNING
RUNNING_STARTER_DUTY = 0.075  # keep starter here (can change)
# In Running: pump is manual (your Set rpm/duty)

# ----- COOLING
COOLING_DURATION_S = 8.0
COOLING_DEFAULT_DUTY = 0.05
