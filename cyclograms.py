# cyclograms.py
# Поки заглушки. Потім замінимо на твої реальні циклограми.

START_CYCLE = [
    # duration_s, stage, pump_cmd, starter_cmd, psu_cmd
    (2.0, "idle",    {"mode": "duty", "value": 0.0}, {"mode": "duty", "value": 0.0}, {"v": 24.0, "i": 5.0, "out": True}),
    (3.0, "prime",   {"mode": "rpm",  "value": 1500}, {"mode": "duty", "value": 0.05}, {"v": 24.0, "i": 10.0, "out": True}),
    (5.0, "run",     {"mode": "rpm",  "value": 3000}, {"mode": "rpm",  "value": 2000}, {"v": 24.0, "i": 15.0, "out": True}),
    (2.0, "stop",    {"mode": "duty", "value": 0.0}, {"mode": "duty", "value": 0.0}, {"v": 0.0, "i": 0.0, "out": False}),
]

COOLING_CYCLE = [
    (10.0, "cooling", {"mode": "rpm", "value": 800}, {"mode": "duty", "value": 0.0}, {"v": 12.0, "i": 5.0, "out": True}),
    (2.0,  "stop",    {"mode": "duty", "value": 0.0}, {"mode": "duty", "value": 0.0}, {"v": 0.0, "i": 0.0, "out": False}),
]
