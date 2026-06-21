"""Parallel-discharge / charge-rotation policy for a two-DPU EcoFlow system.

Pure decision logic, no BLE / I/O — so it can be unit-tested offline.

Model
-----
Two DPU units share a parallel box. Each unit is either:
  * ON  = attached to the parallel group, discharging  (PrStateSet set_self=1)
  * OFF = detached from the group, charging             (PrStateSet set_self=0)

Policy (UTC):
  * Default: BOTH ON (no charging, parallel discharge).
  * Pull the LOWER-SoC unit OFF to charge when ANY of:
      1. lower SoC <= FLOOR (40%)                         "floor"
      2. lower SoC <= EARLY (50%) AND both units < 60%    "early"
      3. in night window (04:30-12:00 UTC) AND lower < 75% "window topup"
  * Once charging, always charge to TARGET (80%) then re-evaluate
    (hysteresis: a charging unit keeps charging until it reaches 80%, which
    may then switch charging to the other unit, or return to BOTH ON).
  * At most one unit charges at a time; the other always stays ON
    (make-before-break is enforced when applying transitions).

The night-rate window is fixed in UTC year-round (the EST/EDT pair shifts so
the UTC window is constant - no DST math needed).
"""

from datetime import datetime, time, timezone

# --- Tunable policy constants -------------------------------------------------
CRITICAL_SOC = 15       # emergency: never let the load-bearing unit fall below
FLOOR_SOC = 40          # rule 1: hard floor for the lower unit
EARLY_SOC = 50          # rule 2: lower-unit threshold ...
EARLY_BOTH_BELOW = 60   #         ... when BOTH units are below this
WINDOW_SOC = 75         # rule 3: start a window topup below this
TARGET_SOC = 80         # always charge up to this, then re-evaluate

WINDOW_START = time(4, 30)   # 04:30 UTC
WINDOW_END = time(12, 0)     # 12:00 UTC


def in_window(now=None):
    """True if `now` (UTC) is within the night-rate window [04:30, 12:00)."""
    if now is None:
        now = datetime.now(timezone.utc)
    t = now.astimezone(timezone.utc).time()
    return WINDOW_START <= t < WINDOW_END


def decide_charging_unit(soc, charging=None, now=None):
    """Decide which unit (if any) should be OFF/charging.

    Args:
        soc:      dict {unit_key: soc_percent} for all units (expects 2).
        charging: the unit currently charging (key) or None.
        now:      datetime (UTC) for window evaluation; defaults to utcnow.

    Returns:
        the unit key to charge, or None for BOTH ON.
    """
    # Critical-low override (takes precedence over the charge-to-TARGET
    # hysteresis): while one unit charges, the other carries the whole load. If
    # that load-bearing unit approaches the critical floor, switch charging to
    # it immediately so we never fully deplete and black out the load. Charge
    # rate (~1.5 kW) exceeds typical load, so the unit coming off charge can
    # take over and outpace discharge.
    if charging is not None:
        others = [u for u in soc if u != charging]
        if others:
            other = min(others, key=soc.get)
            if soc[other] <= CRITICAL_SOC:
                return other

    # Hysteresis: a unit that is already charging keeps charging until TARGET.
    if charging is not None and soc.get(charging, 100) < TARGET_SOC:
        return charging

    # Otherwise (nobody charging, or the charger just reached TARGET) evaluate
    # the triggers fresh against the lower-SoC unit.
    lower = min(soc, key=soc.get)
    lower_soc = soc[lower]
    both_below = all(v < EARLY_BOTH_BELOW for v in soc.values())
    win = in_window(now)

    need_charge = (
        lower_soc <= FLOOR_SOC
        or (lower_soc <= EARLY_SOC and both_below)
        or (win and lower_soc < WINDOW_SOC)
    )

    if need_charge and lower_soc < TARGET_SOC:
        return lower
    return None


def plan_transitions(charging_unit, units, actual_on):
    """Plan the ordered ON/OFF actions to reach the desired state.

    Make-before-break: every "turn ON" action is emitted before any "turn OFF",
    so a unit is never detached unless the other is already attached (the load
    never sees zero units in parallel).

    Args:
        charging_unit: unit to keep OFF/charging, or None for BOTH ON.
        units:         iterable of all unit keys.
        actual_on:     dict {unit_key: bool} current (commanded) ON state.

    Returns:
        ordered list of (unit_key, want_on_bool) actions to apply.
    """
    units = list(units)
    desired_on = {u: True for u in units}
    if charging_unit is not None:
        desired_on[charging_unit] = False

    actions = []
    for u in units:  # make (turn ON) first
        if desired_on[u] and not actual_on[u]:
            actions.append((u, True))
    for u in units:  # then break (turn OFF)
        if not desired_on[u] and actual_on[u]:
            actions.append((u, False))
    return actions


# --- offline self-test --------------------------------------------------------
if __name__ == "__main__":
    DAY = datetime(2026, 6, 20, 15, 0, tzinfo=timezone.utc)    # outside window
    NIGHT = datetime(2026, 6, 20, 6, 0, tzinfo=timezone.utc)   # inside window

    cases = [
        # (label, soc, charging, now, expected_charging_unit)
        ("both healthy, day -> both on",      {"A": 70, "B": 65}, None, DAY, None),
        ("floor: lower<=40 -> charge lower",  {"A": 38, "B": 65}, None, DAY, "A"),
        ("early: lower<=50 & both<60",        {"A": 50, "B": 55}, None, DAY, "A"),
        ("early blocked: B>=60",              {"A": 50, "B": 62}, None, DAY, None),
        ("window topup: lower<75 in window",  {"A": 72, "B": 90}, None, NIGHT, "A"),
        ("window: lower<75 but day -> none",  {"A": 72, "B": 90}, None, DAY, None),
        ("hysteresis: keep charging to 80",   {"A": 78, "B": 90}, "A", DAY, "A"),
        ("reached target -> release",         {"A": 80, "B": 90}, "A", DAY, None),
        ("switch: A done(80), B now floor",   {"A": 80, "B": 38}, "A", DAY, "B"),
        # critical override: A charging, B (load-bearing) approaching 15% ->
        # switch to charge B immediately, despite A not yet at 80.
        ("critical: A charging, B<=15",       {"A": 55, "B": 15}, "A", DAY, "B"),
        ("critical: B just above (16) holds A",{"A": 55, "B": 16}, "A", DAY, "A"),
        ("critical at night too",             {"A": 60, "B": 12}, "A", NIGHT, "B"),
    ]
    ok = True
    for label, soc, charging, now, expect in cases:
        got = decide_charging_unit(soc, charging, now)
        status = "ok " if got == expect else "FAIL"
        if got != expect:
            ok = False
        print("[%s] %-34s soc=%s charging=%s -> %s (expected %s)" % (
            status, label, soc, charging, got, expect))

    # make-before-break ordering: switching A->B must turn B ON before A OFF
    acts = plan_transitions("B", ["A", "B"], {"A": False, "B": True})
    print("switch A(charging)->B plan:", acts)
    assert acts == [("A", True), ("B", False)], acts

    print("\nALL POLICY TESTS PASSED" if ok else "\nSOME TESTS FAILED")
