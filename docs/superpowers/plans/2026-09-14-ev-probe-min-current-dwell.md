# EV Surplus Probe: Minimum-Current Dwell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the curtailed-surplus probe from chattering at minimum charging current by replacing the instantaneous-drain stop test with a minimum-on floor, an SoC-deficit energy budget, and a restart cooldown.

**Architecture:** `decide_surplus_probe` (pure, in `ev_controller.py`) splits its control law by where the commanded current sits: above minimum it keeps today's two-tier discharge ceilings and one-amp down-steps; at minimum — where the only available move is to stop — a minimum-on floor plus an SoC-deficit budget decide the stop instead, and a cooldown plus a "full again" gate decide the resume. Four tunables move to a new `SurplusProbeParams` dataclass in `models.py`, exposed on the config flow's EV screen. The planner (`planner.py`) owns the clock, passing elapsed-second scalars in so the decision function stays pure.

**Tech Stack:** Python 3.11+, plain dataclasses, pytest. Home Assistant only at the edges (`config_flow.py`, `__init__.py`) which are not unit-tested in this repo by design.

**Spec:** `docs/superpowers/specs/2026-09-14-ev-probe-min-current-dwell-design.md`

---

## Background an engineer needs before starting

**What the probe is.** When the home battery is full and the planner has disabled grid export (bad sell price), surplus solar is *curtailed* — the inverter clips production and no sensor can measure the wasted potential. The EVCS charger detects surplus by watching grid export, so it is blind here. The probe takes over: it pushes EV current up and watches for grid import / battery discharge to discover how much surplus exists. See `docs/superpowers/specs/2026-06-23-ev-curtailed-surplus-probe-design.md`.

**What is broken.** On a three-phase charger `min_charging_current_a = 6 A` is a ~4.1 kW step. Curtailed surplus is routinely smaller, so at 6 A the battery always supplies the difference, always tripping `PROBE_DISCHARGE_CEILING_W = 300`. `_step_down()` from 6 A computes `5 < 6` and commands **0**; the next tick is still armed and hits `if current_a < min_a` → back to 6 A. Result: ~15 min on, one cycle off, forever.

**Why holding is correct.** With the battery full *and* PV clipped, discharging the battery to feed the car opens headroom that the curtailed PV immediately refills. The SoC barely moves. So instantaneous drain cannot distinguish "feeding the car from curtailed PV, via the battery" from "draining the battery into the car" — only the SoC trend can.

**Codebase conventions to follow:**
- `ev_controller.py` holds pure, HA-free decision logic. No `homeassistant` imports, no clock reads, no I/O. Callers pass pre-computed scalars (see the existing `is_session_done(low_power_seconds=...)`).
- `models.py` holds frozen dataclasses with `__post_init__` validation. It has **no** dependency on `ev_controller` — do not add one.
- `planner.py` owns all state and all `now` handling. `EVRuntimeState` is the mutable per-planner EV state.
- `tests/test_ev_controller.py` owns the pure logic. Mid-file `from ... import ...` statements are an established convention in that file — put new imports next to the test block that uses them.
- Run tests with `.venv/bin/python -m pytest`.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `custom_components/pv_optimizer/models.py` | Frozen param dataclasses + validation | **Modify** — add `SurplusProbeParams` |
| `custom_components/pv_optimizer/ev_controller.py` | Pure EV decision logic | **Modify** — new branch structure in `decide_surplus_probe`, `probe` arg on `should_probe_surplus`, new `probe_floor_outspends_budget` |
| `custom_components/pv_optimizer/planner.py` | State, clock, HA reads/writes | **Modify** — `EVConfig.probe`, three `EVRuntimeState` timestamps, `_run_surplus_probe(now, ...)` |
| `custom_components/pv_optimizer/const.py` | Config keys + defaults | **Modify** — four `CONF_*`/`DEFAULT_*` pairs |
| `custom_components/pv_optimizer/config_flow.py` | HA config UI | **Modify** — four fields on `_EV_SCHEMA` |
| `custom_components/pv_optimizer/__init__.py` | HA setup wiring | **Modify** — build `SurplusProbeParams`, `%`→kWh, warning log |
| `tests/test_ev_controller.py` | Pure-logic tests | **Modify** — new cases; update two existing |
| `tests/test_planner.py` | Planner-wiring tests | **Modify** — new cases; update one existing |
| `PRD.md`, `README.md` | User/product docs | **Modify** — document the dwell + new config fields |

No new files. Every change lands in a module that already owns that responsibility.

---

## Task 1: `SurplusProbeParams` dataclass

**Files:**
- Modify: `custom_components/pv_optimizer/models.py` (append after `EVParams`, before `class OptimizerError`)
- Test: `tests/test_ev_controller.py`

- [ ] **Step 1: Write the failing tests**

Append to the end of `tests/test_ev_controller.py`:

```python
# ---------------------------------------------------------------------------
# SurplusProbeParams
# ---------------------------------------------------------------------------

from custom_components.pv_optimizer.models import SurplusProbeParams


def test_probe_params_defaults() -> None:
    p = SurplusProbeParams()
    assert p.min_on_seconds == 600.0
    assert p.restart_cooldown_seconds == 600.0
    assert p.soc_drop_kwh == 1.0
    assert p.import_hard_w == 2000.0


def test_probe_params_disarm_eps_is_derived_above_the_budget() -> None:
    """The disarm margin must stay wider than the hold budget, or the
    arm/disarm boundary re-creates the chatter the hold exists to remove.
    Derived (not configurable) so the invariant cannot be broken from the
    options form.
    """
    assert SurplusProbeParams(soc_drop_kwh=1.0).soc_disarm_eps_kwh == 1.5
    assert SurplusProbeParams(soc_drop_kwh=0.05).soc_disarm_eps_kwh == 0.55


@pytest.mark.parametrize("kwargs", [
    {"min_on_seconds": 0.0},
    {"min_on_seconds": -1.0},
    {"restart_cooldown_seconds": 0.0},
    {"soc_drop_kwh": 0.0},
    {"soc_drop_kwh": -0.5},
    {"import_hard_w": 0.0},
])
def test_probe_params_rejects_non_positive(kwargs) -> None:
    with pytest.raises(ValueError):
        SurplusProbeParams(**kwargs)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_ev_controller.py -k probe_params -v`
Expected: FAIL at collection — `ImportError: cannot import name 'SurplusProbeParams'`

- [ ] **Step 3: Write the implementation**

In `custom_components/pv_optimizer/models.py`, insert immediately after the `EVParams` class (after its `kw_per_amp` property) and before `class OptimizerError`:

```python
@dataclass(frozen=True)
class SurplusProbeParams:
    """Tunables for the curtailed-surplus probe's minimum-current dwell.

    Separate from :class:`EVParams` on purpose: ``soc_drop_kwh`` is a
    *home-battery* energy budget, not a charger characteristic.

    At minimum charging current the probe has no down-step available, so the
    only move is to stop — an expensive move (connector cycling, lost
    throughput). These four knobs govern it in place of the instantaneous
    discharge ceilings, which are the wrong instrument there: with the battery
    full and PV clipped, drain and "surplus arriving via the battery" look
    identical, and only the SoC trend separates them.

    - ``min_on_seconds``: floor on session length once charging starts.
      Unconditional apart from the grid-import escapes.
    - ``restart_cooldown_seconds``: floor on off-time after a stop.
    - ``soc_drop_kwh``: energy budget. Past the min-on floor, a deficit
      (``soc_max_kwh - soc_kwh``) beyond this ends the hold. An energy budget
      rather than a timer is what makes the law self-pacing: mostly-covered
      charging accrues deficit slowly and continues; no sun at all spends the
      budget in minutes and stops.
    - ``import_hard_w``: grid import that stops charging at once, overriding
      ``min_on_seconds``. Import is real money.
    """

    min_on_seconds: float = 600.0
    restart_cooldown_seconds: float = 600.0
    soc_drop_kwh: float = 1.0
    import_hard_w: float = 2000.0

    def __post_init__(self) -> None:
        if self.min_on_seconds <= 0:
            raise ValueError("min_on_seconds must be > 0")
        if self.restart_cooldown_seconds <= 0:
            raise ValueError("restart_cooldown_seconds must be > 0")
        if self.soc_drop_kwh <= 0:
            raise ValueError("soc_drop_kwh must be > 0")
        if self.import_hard_w <= 0:
            raise ValueError("import_hard_w must be > 0")

    @property
    def soc_disarm_eps_kwh(self) -> float:
        """SoC margin below ``soc_max`` within which the probe stays armed.

        Derived rather than configurable: it MUST stay wider than
        ``soc_drop_kwh``, or the arm/disarm boundary trips before the hold
        budget does and the probe chatters via disarm/re-arm instead.
        """
        return self.soc_drop_kwh + 0.5
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_ev_controller.py -k probe_params -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/models.py tests/test_ev_controller.py
git commit -m "$(cat <<'MSG'
feat(ev): add SurplusProbeParams for min-current dwell

Four tunables for the probe's behaviour at minimum charging current,
plus a derived disarm margin that stays wider than the hold budget so
the arm/disarm boundary cannot re-create the chatter.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 2: `should_probe_surplus` takes `probe`, uses the derived disarm margin

**Files:**
- Modify: `custom_components/pv_optimizer/ev_controller.py:99-131` (constants + `should_probe_surplus`)
- Test: `tests/test_ev_controller.py:442-499`

This task removes the `SOC_DISARM_EPS_KWH` module constant. Existing tests import it and one asserts the old 0.5 value — both must be updated here, in the same commit.

- [ ] **Step 1: Update the existing failing test**

In `tests/test_ev_controller.py`, change the top-of-file import block (lines 6-16) — remove `SOC_DISARM_EPS_KWH`:

```python
from custom_components.pv_optimizer.ev_controller import (
    EVStateClass,
    classify_state,
    DEFAULT_STATE_VOCAB,
    should_probe_surplus,
    SOC_FULL_EPS_KWH,
    decide_surplus_probe,
    PROBE_UP_INTERVAL_CYCLES,
    PROBE_OVERSHOOT_SUSTAIN_CYCLES,
)
```

Move the `SurplusProbeParams` import added in Task 1 from the bottom of the file up into the probe section header (line ~442), keeping the file's mid-file-import convention. Delete it from the bottom and add it here:

```python
# ---------------------------------------------------------------------------
# Task 3: should_probe_surplus
# ---------------------------------------------------------------------------

from custom_components.pv_optimizer.models import SurplusProbeParams
```

`EVParams` is already imported at line ~108, which is above this point — leave it where it is.

Add `probe` to the `_arm_kwargs` helper (currently at line ~445):

```python
def _arm_kwargs(**over):
    base = dict(
        currently_armed=False,
        state_class=EVStateClass.CONNECTED_IDLE,
        p_ev_chg_kw=0.0,
        p_sell_kw=0.0,
        soc_kwh=9.0,
        soc_max_kwh=9.0,
        forecast_surplus_kw=2.0,
        battery_power_available=True,
        grid_available=True,
        probe=SurplusProbeParams(),
    )
    base.update(over)
    return base
```

Replace `test_should_probe_disarm_uses_wider_soc_margin` (line ~496) with:

```python
def test_should_probe_disarm_uses_wider_soc_margin() -> None:
    # Default probe: soc_drop 1.0 => disarm margin 1.5. soc 8.6 is a 0.4 kWh
    # deficit: below the arm floor (0.2) but well inside the disarm margin.
    assert should_probe_surplus(**_arm_kwargs(soc_kwh=8.6, currently_armed=False)) is False
    assert should_probe_surplus(**_arm_kwargs(soc_kwh=8.6, currently_armed=True)) is True


def test_should_probe_disarm_margin_tracks_the_hold_budget() -> None:
    """The disarm margin is derived from soc_drop_kwh, so a wider hold budget
    automatically widens the margin that keeps the probe armed. Without this
    the hold would trip the disarm before the budget and chatter via
    disarm/re-arm.
    """
    wide = SurplusProbeParams(soc_drop_kwh=2.0)   # disarm margin 2.5
    # soc 7.0 = a 2.0 kWh deficit: outside the default 1.5 margin, inside 2.5.
    assert should_probe_surplus(
        **_arm_kwargs(soc_kwh=7.0, currently_armed=True)) is False
    assert should_probe_surplus(
        **_arm_kwargs(soc_kwh=7.0, currently_armed=True, probe=wide)) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_ev_controller.py -k should_probe -v`
Expected: FAIL — `TypeError: should_probe_surplus() got an unexpected keyword argument 'probe'`

- [ ] **Step 3: Write the implementation**

In `custom_components/pv_optimizer/ev_controller.py`, delete the `SOC_DISARM_EPS_KWH` constant line and amend the comment above the block:

```python
# --- Curtailed-surplus probe (see specs/2026-06-23-ev-curtailed-surplus-probe
# and specs/2026-09-14-ev-probe-min-current-dwell) ---
# All tunable; initial values to validate empirically. The split is
# configurability, not current level: knobs users are expected to tune live
# on models.SurplusProbeParams and reach the config form, while the values
# here are internal tuning that stays in code.
SOC_FULL_EPS_KWH = 0.2        # how close to soc_max counts as "full" (arm)
PROBE_FORECAST_MARGIN_KW = 0.5  # forecast surplus must exceed this to arm
PROBE_DISCHARGE_CEILING_W = 300.0  # soft band floor: sustained drain steps down
PROBE_DISCHARGE_HARD_W = 1500.0    # hard ceiling: step down immediately
PROBE_IMPORT_CEILING_W = 500.0     # step down above this grid import
PROBE_UP_INTERVAL_CYCLES = 1       # min cycles between speculative up-steps
PROBE_OVERSHOOT_SUSTAIN_CYCLES = 2  # soft-band cycles before stepping down
# Soft import tier's dwell at minimum current, where the only move is to stop.
# Above minimum current the soft tier still steps down immediately.
PROBE_IMPORT_SUSTAIN_SECONDS = 600.0
```

Change the `should_probe_surplus` signature and the SoC-margin line. Add `probe` to the keyword-only args (after `grid_available`) and replace the margin lookup:

```python
def should_probe_surplus(
    *,
    currently_armed: bool,
    state_class: EVStateClass,
    p_ev_chg_kw: float,
    p_sell_kw: float,
    soc_kwh: float,
    soc_max_kwh: float,
    forecast_surplus_kw: float,
    battery_power_available: bool,
    grid_available: bool,
    probe,  # SurplusProbeParams
    eps: float = 1e-6,
) -> bool:
```

and inside, replace:

```python
    soc_eps = SOC_DISARM_EPS_KWH if currently_armed else SOC_FULL_EPS_KWH
```

with:

```python
    soc_eps = probe.soc_disarm_eps_kwh if currently_armed else SOC_FULL_EPS_KWH
```

Also update the docstring's last paragraph:

```python
    """True iff the planner should take over surplus charging from the EVCS.

    Arms only in the curtailment corner: battery full, no LP-planned EV charge,
    not exporting (so the EVCS's export-follower would be blind), forecast says
    surplus exists, car connected, and both signal sources are available.
    Uses ``probe.soc_disarm_eps_kwh`` (derived, wider than the hold budget)
    while already armed, so neither the probe's own overshoot-dip nor a
    deliberate minimum-current hold can disarm it.
    """
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_ev_controller.py -k should_probe -v`
Expected: PASS (11 tests)

`decide_surplus_probe` tests will still pass — it does not reference the removed constant.

Run: `.venv/bin/python -m pytest tests/test_ev_controller.py -q`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/ev_controller.py tests/test_ev_controller.py
git commit -m "$(cat <<'MSG'
refactor(ev): derive probe disarm margin from the hold budget

Replaces the fixed SOC_DISARM_EPS_KWH with probe.soc_disarm_eps_kwh so
the margin always stays wider than soc_drop_kwh. Adds
PROBE_IMPORT_SUSTAIN_SECONDS for the next task.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 3: `decide_surplus_probe` — gated restart from zero

Split into three tasks (3, 4, 5) by branch so each is independently testable. This one covers branch 2 of the spec: leaving zero.

> **Correction applied during execution — expected red suite for Tasks 3-6.**
> The four new required parameters on `decide_surplus_probe` break
> `planner.py`'s call site, exactly as Task 2 broke the `should_probe_surplus`
> call site. Unlike Task 2 this cannot be cheaply forward-pulled: repairing it
> needs the `EVRuntimeState` timestamps, `_elapsed_seconds`, and `now`
> threading — essentially all of Task 7's implementation.
>
> So `tests/test_planner.py` is **expected to fail** from Task 3 until Task 7
> closes the call site. Do NOT give the new parameters defaults to paper over
> this: silently defaulting a clock value would let a wiring bug ship.
>
> **Verification rule for Tasks 3-6 in place of "full suite green":**
> 1. `.venv/bin/python -m pytest tests/test_ev_controller.py -q` MUST be fully
>    green. This is where all the changing logic lives.
> 2. `tests/test_planner.py` failures must be **exactly** the known set — same
>    node IDs, same `TypeError: decide_surplus_probe() missing N required
>    keyword-only arguments` signature. The controller records the baseline set
>    after Task 3 and diffs it after each later task; any new or different
>    failure is a regression, not expected breakage.
>
> This is acceptable because Tasks 3-6 change only pure logic in
> `ev_controller.py`, which `tests/test_ev_controller.py` covers completely.
> The planner tests cover wiring, which does not change until Task 7.

**Files:**
- Modify: `custom_components/pv_optimizer/ev_controller.py` (`decide_surplus_probe`)
- Test: `tests/test_ev_controller.py:503-595`

- [ ] **Step 1: Write the failing tests**

In `tests/test_ev_controller.py`, extend the `_probe` helper (line ~513) with the new arguments:

```python
def _probe(**over):
    base = dict(
        battery_discharge_w=0.0,
        grid_import_w=0.0,
        forecast_surplus_kw=10.0,
        current_a=0,
        cycles_since_up=0,
        cycles_overshooting=0,
        probe_on_seconds=0.0,
        probe_off_seconds=float("inf"),
        import_over_seconds=0.0,
        soc_deficit_kwh=0.0,
        ev=_PROBE_EV,
        probe=SurplusProbeParams(),
    )
    base.update(over)
    return decide_surplus_probe(**base)
```

Then add, after `test_probe_kicks_to_min_when_not_charging`:

```python
def test_probe_kicks_to_min_when_never_stopped() -> None:
    """probe_off_seconds is infinite when there is no recorded stop. A
    never-stopped probe has no cooldown to serve, so the very first kick to
    minimum must not be delayed.
    """
    d = _probe(current_a=0, probe_off_seconds=float("inf"))
    assert d.current_a == 6


def test_probe_restart_blocked_during_cooldown() -> None:
    """The fix for the reported chatter: after a stop the probe must not kick
    straight back to minimum on the next tick.
    """
    d = _probe(current_a=0, probe_off_seconds=60.0)
    assert d.current_a == 0


def test_probe_restart_allowed_after_cooldown() -> None:
    d = _probe(current_a=0, probe_off_seconds=601.0)
    assert d.current_a == 6


def test_probe_restart_blocked_until_battery_full_again() -> None:
    """Cooldown elapsed is not enough. The battery must be genuinely full
    again (the arm epsilon, not the looser hold budget) — otherwise a sunless
    spell would restart, drain, stop, and repeat.
    """
    d = _probe(current_a=0, probe_off_seconds=1200.0, soc_deficit_kwh=0.5)
    assert d.current_a == 0


def test_probe_restart_soc_gate_is_tighter_than_the_hold_budget() -> None:
    """restart_eps = min(SOC_FULL_EPS_KWH, soc_drop_kwh / 2).

    The min() is load-bearing: soc_drop_kwh is user-configurable as a
    percentage of battery capacity, so a small percentage on a small battery
    can land it BELOW the fixed 0.2 kWh arm epsilon. A bare SOC_FULL_EPS_KWH
    restart gate would then be looser than the stop gate — stop at a 0.05 kWh
    deficit, immediately cleared to restart at up to 0.2 — which is the
    original chatter with extra steps.
    """
    tiny = SurplusProbeParams(soc_drop_kwh=0.05)
    # A deficit that already ends a hold (0.06 > 0.05) must not clear restart.
    d = _probe(current_a=0, probe_off_seconds=1200.0,
               soc_deficit_kwh=0.06, probe=tiny)
    assert d.current_a == 0
    # Inside restart_eps (0.025) it may restart.
    d = _probe(current_a=0, probe_off_seconds=1200.0,
               soc_deficit_kwh=0.02, probe=tiny)
    assert d.current_a == 6
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_ev_controller.py -k "probe_restart or probe_kicks" -v`
Expected: FAIL — `TypeError: decide_surplus_probe() got an unexpected keyword argument 'probe_on_seconds'`

- [ ] **Step 3: Write the implementation**

Replace the whole `decide_surplus_probe` signature and its docstring, and the "kick to min" branch. The full new function body arrives across Tasks 3-5; this step writes the signature plus branch 2 and leaves the rest as-is.

New signature and docstring:

```python
def decide_surplus_probe(
    *,
    battery_discharge_w: float,
    grid_import_w: float,
    forecast_surplus_kw: float,
    current_a: int,
    cycles_since_up: int,
    cycles_overshooting: int,
    probe_on_seconds: float,
    probe_off_seconds: float,
    import_over_seconds: float,
    soc_deficit_kwh: float,
    ev,  # EVParams
    probe,  # SurplusProbeParams
) -> SurplusProbeDecision:
    """Zero-import regulator step, split by where the current sits.

    **Above minimum current**, a down-step gives back one amp: cheap,
    reversible, correct immediately. Past ``PROBE_DISCHARGE_HARD_W`` of battery
    discharge, or *any* grid import past ``PROBE_IMPORT_CEILING_W``, step down
    at once. Inside the soft band (``PROBE_DISCHARGE_CEILING_W`` .. hard) the
    drain must persist ``PROBE_OVERSHOOT_SUSTAIN_CYCLES`` consecutive cycles
    first, so a transient — a kettle, a passing cloud, the settling tick right
    after an up-step — doesn't ratchet the current down.

    **At minimum current** there is no down-step, so the only move is to stop:
    expensive (it cycles the car's connector and costs throughput) and
    governed differently. The discharge ceilings are the wrong instrument
    here — with the battery full and PV clipped, "feeding the car from
    curtailed PV via the battery" and "draining the battery into the car" look
    identical, and only the SoC trend separates them. So instead:
    ``probe.min_on_seconds`` is a floor on session length, and past it a
    deficit beyond ``probe.soc_drop_kwh`` ends the hold. There is deliberately
    no maximum-on cap: while the surplus covers the car the SoC stays near
    full and charging runs uninterrupted.

    **Leaving zero** needs both ``probe.restart_cooldown_seconds`` to have
    elapsed and the battery to be genuinely full again — a floor on off-time
    plus proof that surplus actually returned.

    Up (speculative): at most one amp every PROBE_UP_INTERVAL_CYCLES, only
    while not overshooting, below max, and the next amp still fits the
    forecast surplus headroom. Otherwise hold and advance the up-counter.

    All dwells are seconds, computed by the caller (same idiom as
    ``is_session_done(low_power_seconds=...)``) so this stays clock-free.
    Cycle counting would be the wrong clock: ``charger_state_entity`` is a
    re-plan trigger, so every start/stop write fires an extra off-cadence
    tick, precisely around the transitions the dwells exist to damp.
    """
```

Then replace the existing kick-to-min branch:

```python
    if current_a < min_a:
        # Not charging yet — kick to min as the first probe.
        return SurplusProbeDecision(current_a=min_a, cycles_since_up=0)
```

with:

```python
    if current_a < min_a:
        # Not charging. Two gates before kicking to min.
        if probe_off_seconds < probe.restart_cooldown_seconds:
            return SurplusProbeDecision(current_a=0, cycles_since_up=0)
        # Battery genuinely full again, not merely inside the hold budget.
        # The min() keeps this strictly tighter than the stop gate for every
        # configurable budget: soc_drop_kwh is a percentage of battery
        # capacity and can land below the fixed arm epsilon, which would make
        # the restart gate looser than the stop gate and re-create the chatter.
        restart_eps = min(SOC_FULL_EPS_KWH, probe.soc_drop_kwh / 2.0)
        if soc_deficit_kwh > restart_eps:
            return SurplusProbeDecision(current_a=0, cycles_since_up=0)
        return SurplusProbeDecision(current_a=min_a, cycles_since_up=0)
```

Note this branch must move **above** the discharge-ceiling checks — see Task 4, which reorders them. For now leave the ceiling checks where they are; the new tests all pass `battery_discharge_w=0.0` / `grid_import_w=0.0` so they reach this branch either way.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_ev_controller.py -q`
Expected: PASS (all — the existing `decide_surplus_probe` tests keep working because `_probe` supplies defaults for the new arguments)

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/ev_controller.py tests/test_ev_controller.py
git commit -m "$(cat <<'MSG'
feat(ev): gate probe restart on cooldown and battery-full-again

Stops the probe kicking straight back to min current on the tick after
a stop. Restart needs restart_cooldown_seconds elapsed AND the deficit
back inside min(SOC_FULL_EPS_KWH, soc_drop_kwh/2) -- derived so the
restart gate stays strictly tighter than the stop gate for every
configurable budget.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 4: `decide_surplus_probe` — minimum-current hold

Branch 4 of the spec. This is the core fix for the reported bug, and it changes the meaning of one existing test.

**Files:**
- Modify: `custom_components/pv_optimizer/ev_controller.py` (`decide_surplus_probe` body)
- Test: `tests/test_ev_controller.py`

- [ ] **Step 1: Replace the existing test that asserts the old behaviour**

In `tests/test_ev_controller.py`, delete `test_probe_below_min_goes_to_zero` (line ~571):

```python
def test_probe_below_min_goes_to_zero() -> None:
    d = _probe(current_a=6, battery_discharge_w=2000.0)
    assert d.current_a == 0
```

and replace it with:

```python
def test_probe_holds_at_min_inside_the_min_on_floor() -> None:
    """REGRESSION for the reported chatter.

    At min current there is no down-step, so the old law stopped. On a
    three-phase charger 6 A is a ~4.1 kW quantum, larger than typical
    curtailed surplus, so the drain test was permanently unsatisfiable and
    the probe stopped every time it reached min.
    """
    d = _probe(current_a=6, battery_discharge_w=500.0, probe_on_seconds=120.0)
    assert d.current_a == 6


def test_probe_holds_at_min_through_hard_drain_inside_the_floor() -> None:
    """Deliberate change: PROBE_DISCHARGE_HARD_W no longer stops charging
    instantly at min current. Its worst case is bounded by the min-on floor
    and then the SoC budget instead.
    """
    d = _probe(current_a=6, battery_discharge_w=2000.0, probe_on_seconds=120.0)
    assert d.current_a == 6


def test_probe_holds_at_min_indefinitely_while_soc_stays_full() -> None:
    """No maximum-on cap. With the battery full and PV clipped, discharging to
    feed the car opens headroom the curtailed PV immediately refills, so the
    deficit stays near zero and charging should never be interrupted.
    """
    for on_seconds in (700.0, 3600.0, 86400.0):
        d = _probe(current_a=6, battery_discharge_w=2000.0,
                   probe_on_seconds=on_seconds, soc_deficit_kwh=0.1)
        assert d.current_a == 6


def test_probe_stops_at_min_once_the_soc_budget_is_spent() -> None:
    d = _probe(current_a=6, battery_discharge_w=2000.0,
               probe_on_seconds=700.0, soc_deficit_kwh=1.2)
    assert d.current_a == 0


def test_probe_soc_budget_does_not_override_the_min_on_floor() -> None:
    """Ordering: the floor is checked first. Only the import escapes break it.
    """
    d = _probe(current_a=6, battery_discharge_w=2000.0,
               probe_on_seconds=300.0, soc_deficit_kwh=1.2)
    assert d.current_a == 6


def test_probe_at_min_emits_zero_overshoot_count() -> None:
    """cycles_overshooting governs one-amp steps above min only. A stale count
    must not leak into a later above-min state.
    """
    d = _probe(current_a=6, battery_discharge_w=500.0, probe_on_seconds=120.0,
               cycles_overshooting=1)
    assert d.cycles_overshooting == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_ev_controller.py -k "holds_at_min or stops_at_min or soc_budget or at_min_emits" -v`
Expected: FAIL — `test_probe_holds_at_min_inside_the_min_on_floor` asserts `6` but gets `0` (the old `_step_down()` path), and similarly for the others.

- [ ] **Step 3: Write the implementation**

Replace the entire body of `decide_surplus_probe` below the docstring (keep the signature and docstring from Task 3) with:

```python
    min_a = int(round(ev.min_charging_current_a))
    max_a = int(round(ev.max_charging_current_a))

    def _step_down() -> SurplusProbeDecision:
        new_a = current_a - 1
        if new_a < min_a:
            return SurplusProbeDecision(current_a=0, cycles_since_up=0)
        return SurplusProbeDecision(current_a=new_a, cycles_since_up=0)

    def _stop() -> SurplusProbeDecision:
        return SurplusProbeDecision(current_a=0, cycles_since_up=0)

    def _hold_at_min() -> SurplusProbeDecision:
        return SurplusProbeDecision(current_a=min_a, cycles_since_up=0)

    at_min = current_a == min_a

    # --- Import escapes. Only meaningful while actually charging; they
    # override the min-on floor because import is real money.
    if current_a >= min_a:
        if grid_import_w > probe.import_hard_w:
            return _stop() if at_min else _step_down()
        if grid_import_w > PROBE_IMPORT_CEILING_W:
            if not at_min:
                return _step_down()
            if import_over_seconds >= PROBE_IMPORT_SUSTAIN_SECONDS:
                return _stop()
            # Sustain not met: fall through to the drain handling below, which
            # holds at min. Never step up while importing.

    if current_a < min_a:
        # Not charging. Two gates before kicking to min.
        if probe_off_seconds < probe.restart_cooldown_seconds:
            return _stop()
        # Battery genuinely full again, not merely inside the hold budget.
        # The min() keeps this strictly tighter than the stop gate for every
        # configurable budget: soc_drop_kwh is a percentage of battery
        # capacity and can land below the fixed arm epsilon, which would make
        # the restart gate looser than the stop gate and re-create the chatter.
        restart_eps = min(SOC_FULL_EPS_KWH, probe.soc_drop_kwh / 2.0)
        if soc_deficit_kwh > restart_eps:
            return _stop()
        return SurplusProbeDecision(current_a=min_a, cycles_since_up=0)

    over_soft = battery_discharge_w > PROBE_DISCHARGE_CEILING_W
    over_hard = battery_discharge_w > PROBE_DISCHARGE_HARD_W
    importing = grid_import_w > PROBE_IMPORT_CEILING_W

    if at_min:
        # No down-step exists here, so the only move is off. The discharge
        # ceilings cannot tell "surplus arriving via the battery" from "the
        # battery draining into the car" — only the SoC trend can.
        if over_soft or importing:
            if probe_on_seconds < probe.min_on_seconds:
                return _hold_at_min()
            if soc_deficit_kwh > probe.soc_drop_kwh:
                return _stop()
            return _hold_at_min()
    else:
        if over_hard:
            return _step_down()
        if over_soft:
            sustained = cycles_overshooting + 1
            if sustained >= PROBE_OVERSHOOT_SUSTAIN_CYCLES:
                return _step_down()
            return SurplusProbeDecision(current_a=current_a, cycles_since_up=0,
                                        cycles_overshooting=sustained)

    can_step_up = (
        current_a < max_a
        and cycles_since_up + 1 >= PROBE_UP_INTERVAL_CYCLES
        and (current_a + 1) * ev.kw_per_amp <= forecast_surplus_kw
    )
    if can_step_up:
        return SurplusProbeDecision(current_a=current_a + 1, cycles_since_up=0)
    return SurplusProbeDecision(current_a=current_a,
                                cycles_since_up=cycles_since_up + 1)
```

Two things to note while reading this:
- `_hold_at_min()` and `_stop()` both leave `cycles_overshooting` at its default `0`, which is what the at-min branch requires.
- The at-min branch falls through to the up-step when *not* over any ceiling, so a min-current probe with real headroom still ramps up normally.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_ev_controller.py -q`
Expected: PASS (all). In particular the pre-existing above-min tests — `test_probe_holds_on_first_soft_battery_discharge`, `test_probe_steps_down_after_sustained_soft_battery_discharge`, `test_probe_steps_down_immediately_on_hard_battery_discharge`, `test_probe_steps_down_on_grid_import`, `test_probe_steps_up_every_cycle`, `test_probe_up_gated_by_forecast_headroom`, `test_probe_does_not_exceed_max` — must pass **untouched**. That is the guard that this change is scoped to minimum current. If any of them needed editing, the branch split is wrong.

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/ev_controller.py tests/test_ev_controller.py
git commit -m "$(cat <<'MSG'
fix(ev): hold at min current instead of stopping on drain

At min current a down-step has nowhere to go, so it became a stop, and
the next tick kicked straight back -- ~15 min on, one cycle off,
forever. Root cause is quantization: 6 A three-phase is a 4.1 kW step,
larger than typical curtailed surplus, so the drain test was
permanently unsatisfiable there.

Replace the ceilings at min current with a min-on floor plus an
SoC-deficit budget. With the battery full and PV clipped, the drain is
refilled by the curtailed PV, so the deficit stays near zero and
charging runs uninterrupted -- no maximum-on cap.

Above min current the two-tier ceilings are unchanged.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 5: `decide_surplus_probe` — import escape tiers

Branch 1 of the spec. The code landed in Task 4; this task adds the tests that pin it down.

**Files:**
- Test: `tests/test_ev_controller.py`

- [ ] **Step 1: Write the failing tests**

Append after the at-min tests from Task 4:

```python
def test_probe_hard_import_stops_at_min_overriding_the_floor() -> None:
    """Import is real money, so it is allowed to break the connector-
    protection floor. The restart cooldown bounds the resulting cycling.
    """
    d = _probe(current_a=6, grid_import_w=2500.0, probe_on_seconds=10.0)
    assert d.current_a == 0


def test_probe_hard_import_steps_down_above_min() -> None:
    d = _probe(current_a=10, grid_import_w=2500.0)
    assert d.current_a == 9


def test_probe_soft_import_at_min_needs_sustain() -> None:
    """500 W for one cycle is ~0.04 kWh -- about a cent. Stopping instantly
    over that would let one kettle end a charging session.
    """
    d = _probe(current_a=6, grid_import_w=800.0, probe_on_seconds=1200.0,
               import_over_seconds=60.0)
    assert d.current_a == 6


def test_probe_soft_import_at_min_stops_once_sustained() -> None:
    d = _probe(current_a=6, grid_import_w=800.0, probe_on_seconds=1200.0,
               import_over_seconds=601.0)
    assert d.current_a == 0


def test_probe_soft_import_at_min_never_steps_up() -> None:
    """While importing we already suspect we are over: hold, don't probe up.
    """
    d = _probe(current_a=6, grid_import_w=800.0, probe_on_seconds=1200.0,
               import_over_seconds=60.0, forecast_surplus_kw=10.0)
    assert d.current_a == 6


def test_probe_import_escape_inactive_at_zero() -> None:
    """Nothing to escape from when already stopped; the restart gates own
    this state. Import here is the house, not the car.
    """
    d = _probe(current_a=0, grid_import_w=2500.0, probe_off_seconds=1200.0)
    assert d.current_a == 6
```

- [ ] **Step 2: Run tests to verify they pass immediately**

Run: `.venv/bin/python -m pytest tests/test_ev_controller.py -k import -v`
Expected: PASS. These tests characterise code written in Task 4 rather than driving new code — if any fails, Task 4's branch ordering is wrong and must be fixed before continuing.

- [ ] **Step 3: Commit**

```bash
git add tests/test_ev_controller.py
git commit -m "$(cat <<'MSG'
test(ev): pin down probe import-escape tiers

Hard tier stops at min / steps down above min, overriding the min-on
floor. Soft tier needs PROBE_IMPORT_SUSTAIN_SECONDS at min current so a
household transient cannot end a session, and never steps up while
importing. Neither applies at zero current.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 6: `probe_floor_outspends_budget` predicate

**Files:**
- Modify: `custom_components/pv_optimizer/ev_controller.py` (append after `decide_surplus_probe`)
- Test: `tests/test_ev_controller.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ev_controller.py`:

```python
# ---------------------------------------------------------------------------
# probe_floor_outspends_budget
# ---------------------------------------------------------------------------

from custom_components.pv_optimizer.ev_controller import (
    probe_floor_outspends_budget,
)

# Three-phase: 22 kW / 32 A => 0.6875 kW/A, so 6 A is ~4.1 kW.
_EV_3P = EVParams(
    max_charging_power_kw=22.0, max_charging_current_a=32.0,
    min_charging_current_a=6.0, car_battery_kwh=60.0,
)


def test_floor_outspends_budget_when_budget_is_small() -> None:
    # 10 min at 4.125 kW = 0.69 kWh, which a 0.5 kWh budget cannot cover:
    # the floor would decide every stop and the budget nothing.
    assert probe_floor_outspends_budget(
        ev=_EV_3P, probe=SurplusProbeParams(soc_drop_kwh=0.5)) is True


def test_floor_within_budget_at_defaults_three_phase() -> None:
    assert probe_floor_outspends_budget(
        ev=_EV_3P, probe=SurplusProbeParams(soc_drop_kwh=1.0)) is False


def test_floor_within_budget_single_phase() -> None:
    # 7.2 kW / 32 A => 6 A is ~1.35 kW; 10 min is only 0.22 kWh.
    assert probe_floor_outspends_budget(
        ev=_PROBE_EV, probe=SurplusProbeParams(soc_drop_kwh=0.5)) is False


def test_floor_outspends_budget_scales_with_the_floor() -> None:
    assert probe_floor_outspends_budget(
        ev=_EV_3P, probe=SurplusProbeParams(
            min_on_seconds=1800.0, soc_drop_kwh=1.0)) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_ev_controller.py -k outspends -v`
Expected: FAIL at collection — `ImportError: cannot import name 'probe_floor_outspends_budget'`

- [ ] **Step 3: Write the implementation**

Append to `custom_components/pv_optimizer/ev_controller.py`, immediately after `decide_surplus_probe`:

```python
def probe_floor_outspends_budget(*, ev, probe) -> bool:
    """True when the min-on floor can spend more than the whole SoC budget.

    The floor and the budget are sequential gates: the floor holds charging
    unconditionally, then the budget decides. If a worst-case floor (PV
    vanishing the instant charging starts) drains more than ``soc_drop_kwh``,
    the budget never gets to decide anything and the floor silently becomes
    the entire law.

    Both values are user-configurable, so this is surfaced as a setup warning
    rather than an error: the combination is legal and merely means the floor
    dominates. Refusing to start over a tuning choice would be
    disproportionate, and silently clamping would hide it.
    """
    worst_case_kwh = (probe.min_on_seconds
                      * ev.min_charging_current_a
                      * ev.kw_per_amp) / 3600.0
    return worst_case_kwh > probe.soc_drop_kwh
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_ev_controller.py -q`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/ev_controller.py tests/test_ev_controller.py
git commit -m "$(cat <<'MSG'
feat(ev): warn when the min-on floor outspends the SoC budget

Both knobs are user-configurable and are sequential gates, so a floor
whose worst case exceeds the whole budget makes the budget dead code.
Pure predicate here; __init__.py logs the warning.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 7: Planner state and wiring

**Files:**
- Modify: `custom_components/pv_optimizer/planner.py:50-79` (`EVRuntimeState`), `:112-144` (`EVConfig`), `:913-965` (`_run_surplus_probe`), `:905` (its call site)
- Test: `tests/test_planner.py:2179-2320`

> **Correction applied during execution.** The plan assumed the full suite would
> stay green through Tasks 2-6. It does not: making `probe` a required argument
> of `should_probe_surplus` in Task 2 breaks `planner.py`'s call site and turned
> 14 `test_planner.py` tests red. Leaving that outstanding until Task 7 would
> have left Task 4 — the highest-risk change in the plan — with no clean
> full-suite signal, so three items were **forward-pulled into Task 2**:
>
> - the `SurplusProbeParams` entry in planner's `from .models import (...)` block
>   (part of step 3a),
> - the whole of step 3b (`EVConfig.probe` with its `default_factory`),
> - the `probe=cfg.probe,` argument on the `should_probe_surplus(...)` call
>   inside `_run_surplus_probe` (part of step 3f).
>
> When executing this task, treat those three as already done and verify rather
> than re-apply them. Everything else below is untouched.

- [ ] **Step 1: Write the failing tests**

In `tests/test_planner.py`, add `probe` to `_probe_ev_cfg` (line ~2183):

```python
def _probe_ev_cfg(**over):
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams, SurplusProbeParams
    kwargs = dict(
        params=EVParams(
            max_charging_power_kw=7.2, max_charging_current_a=32.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0,
            current_tolerance_a=1.0, buy_price_threshold=0.0),
        probe=SurplusProbeParams(),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        start_switch_entity="switch.ev_start",
        charger_mode_entity="select.ev_mode",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
        planned_start_entity="datetime.pv_optimizer_ev_planned_start",
    )
    kwargs.update(over)
    return EVConfig(**kwargs)
```

Replace `test_probe_steps_to_zero_and_stops_on_overshoot` (line ~2284) — it asserts the removed behaviour — with:

```python
def test_probe_holds_at_min_through_drain_inside_the_floor() -> None:
    # Past the hard ceiling, but inside the min-on floor: hold, don't stop.
    states = _probe_states()
    states["sensor.batt_w"] = StateView(state="-2000")
    p = Planner(_config(ev=_probe_ev_cfg(), battery_power_entity="sensor.batt_w"),
                FakeReader(states), FakeCaller())
    p.ev_state.probe_armed = True
    p.ev_state.probe_current_a = 6
    p.ev_state.probe_started_at = NOW - timedelta(seconds=60)
    p.step(NOW)
    assert p.ev_state.probe_current_a == 6
    starts = [c for c in p.caller.calls if c[2].get("entity_id") == "switch.ev_start"]
    assert starts and starts[-1][1] == "turn_on"


def test_probe_stops_at_min_past_floor_with_budget_spent() -> None:
    # Test config: capacity 10.0 kWh, soc_max 9.0 kWh. 78 % => 7.8 kWh, a
    # 1.2 kWh deficit: past the default 1.0 kWh budget, but still inside the
    # 1.5 kWh disarm margin so the probe stays armed rather than handing back.
    states = _probe_states()
    states["sensor.batt_w"] = StateView(state="-2000")
    states["sensor.soc_pct"] = StateView(state="78", attributes={})
    p = Planner(_config(ev=_probe_ev_cfg(), battery_power_entity="sensor.batt_w"),
                FakeReader(states), FakeCaller())
    p.ev_state.probe_armed = True
    p.ev_state.probe_current_a = 6
    p.ev_state.probe_started_at = NOW - timedelta(seconds=1200)
    p.step(NOW)
    assert p.ev_state.probe_current_a == 0
    assert p.ev_state.probe_armed is True   # still armed, just not charging
    assert p.ev_state.probe_stopped_at == NOW
    assert p.ev_state.probe_started_at is None
    current_writes = [c for c in p.caller.calls
                      if c[2].get("entity_id") == "number.ev_max_current"]
    starts = [c for c in p.caller.calls if c[2].get("entity_id") == "switch.ev_start"]
    assert current_writes and current_writes[-1][2]["value"] == 0
    assert starts and starts[-1][1] == "turn_off"


def test_probe_records_started_at_on_kick_to_min() -> None:
    states = _probe_states()
    p = Planner(_config(ev=_probe_ev_cfg(), battery_power_entity="sensor.batt_w"),
                FakeReader(states), FakeCaller())
    p.step(NOW)
    assert p.ev_state.probe_current_a == 6
    assert p.ev_state.probe_started_at == NOW


def test_probe_cooldown_survives_a_disarm_round_trip() -> None:
    """The disarm block resets probe state; probe_stopped_at must be excluded
    or a disarm/re-arm round trip launders the cooldown and the chatter simply
    relocates to the arm boundary.
    """
    states = _probe_states()
    p = Planner(_config(ev=_probe_ev_cfg(), battery_power_entity="sensor.batt_w"),
                FakeReader(states), FakeCaller())
    p.ev_state.probe_armed = True
    p.ev_state.probe_current_a = 0
    p.ev_state.probe_stopped_at = NOW - timedelta(seconds=60)
    # Export re-enabled -> disarm.
    states["sensor.sell"] = StateView(state="0.30", attributes={"today": [0.30] * 24})
    p.step(NOW)
    assert p.ev_state.probe_armed is False
    assert p.ev_state.probe_stopped_at == NOW - timedelta(seconds=60)
    # Export disabled again -> re-arm, but still inside the cooldown.
    states["sensor.sell"] = StateView(state="-0.01", attributes={"today": [-0.01] * 24})
    p.step(NOW + timedelta(seconds=120))
    assert p.ev_state.probe_armed is True
    assert p.ev_state.probe_current_a == 0   # cooldown still running


def test_probe_timestamps_cleared_on_disconnect() -> None:
    """A freshly plugged car should start promptly, not inherit a stale
    cooldown from the previous session.
    """
    states = _probe_states()
    states["sensor.ev_state"] = StateView(state="Disconnected")
    p = Planner(_config(ev=_probe_ev_cfg(), battery_power_entity="sensor.batt_w"),
                FakeReader(states), FakeCaller())
    p.ev_state.probe_started_at = NOW - timedelta(seconds=60)
    p.ev_state.probe_stopped_at = NOW - timedelta(seconds=60)
    p.ev_state.probe_import_over_since = NOW - timedelta(seconds=60)
    p.step(NOW)
    assert p.ev_state.probe_started_at is None
    assert p.ev_state.probe_stopped_at is None
    assert p.ev_state.probe_import_over_since is None


def test_probe_import_over_since_accumulates_and_resets() -> None:
    states = _probe_states()
    states["sensor.grid_w"] = StateView(state="800")   # above the soft ceiling
    p = Planner(_config(ev=_probe_ev_cfg(), battery_power_entity="sensor.batt_w"),
                FakeReader(states), FakeCaller())
    p.ev_state.probe_armed = True
    p.ev_state.probe_current_a = 6
    p.ev_state.probe_started_at = NOW - timedelta(seconds=1200)
    p.step(NOW)
    assert p.ev_state.probe_import_over_since == NOW
    assert p.ev_state.probe_current_a == 6   # sustain not met yet
    states["sensor.grid_w"] = StateView(state="0")
    p.step(NOW + timedelta(minutes=5))
    assert p.ev_state.probe_import_over_since is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_planner.py -k probe -v`
Expected: FAIL — `TypeError: EVConfig.__init__() got an unexpected keyword argument 'probe'`

- [ ] **Step 3: Write the implementation**

**3a.** Update the imports at the top of `planner.py`. `math` is not currently imported; add it to the stdlib block (line 9-13):

```python
import logging
import math
import time as _time
```

Add `PROBE_IMPORT_CEILING_W` to the `ev_controller` import block (line 17-26):

```python
from .ev_controller import (
    DEFAULT_STATE_VOCAB,
    EVStateClass,
    PROBE_IMPORT_CEILING_W,
    classify_state,
    decide_reactive,
    is_session_done,
    translate_lp_slot0,
    should_probe_surplus,
    decide_surplus_probe,
)
```

Add `SurplusProbeParams` to the `models` import block (line 29-37):

```python
from .models import (
    BatteryParams,
    EVParams,
    OptimizerError,
    OptimizerInputs,
    OptimizerResult,
    SlotPlan,
    SurplusProbeParams,
    TariffSlot,
)
```

**3b.** In `EVConfig`, add the field **after `max_current_entity`** — the last field without a default — and before `session_energy_entity`:

```python
    max_current_entity: str     # number entity (A) — output
    # Minimum-current dwell tunables for the curtailed-surplus probe. Placed
    # after the no-default entity fields above (dataclass field-ordering
    # requires defaulted fields to come after non-defaulted ones).
    probe: SurplusProbeParams = field(default_factory=SurplusProbeParams)
    session_energy_entity: str | None = None
```

It cannot go next to `params: EVParams`, which is where it logically belongs: `charger_state_entity`, `charging_power_entity` and `max_current_entity` follow `params` and have no defaults, so a defaulted `probe` placed before them fails at class-definition time with `TypeError: non-default argument 'charger_state_entity' follows default argument 'probe'`.

`field` is already imported in `planner.py` (line 11: `from dataclasses import dataclass, field, replace`). A default is supplied so every existing `EVConfig(...)` construction in tests keeps working — and every construction site uses keyword arguments, so the field's position is not observable by callers.

**3c.** In `EVRuntimeState` (line ~75), replace the probe-state block:

```python
    # Curtailed-surplus probe state.
    probe_armed: bool = False
    probe_current_a: int = 0
    probe_cycles_since_up: int = 0
    probe_cycles_overshooting: int = 0
    # Minimum-current dwell clocks. Seconds-based rather than cycle-based
    # because charger_state_entity is a re-plan trigger, so every start/stop
    # write fires an extra off-cadence tick.
    probe_started_at: datetime | None = None   # 0 -> >=min transition
    # NOT reset on disarm: a disarm/re-arm round trip must not launder the
    # restart cooldown. Cleared only on disconnect.
    probe_stopped_at: datetime | None = None
    probe_import_over_since: datetime | None = None
```

**3d.** In `_apply_ev`, clear the timestamps on disconnect. Find the existing block:

```python
        if mode != "car" or state_class == EVStateClass.DISCONNECTED:
            es.car_session_charging_seen = False
```

and add immediately after it:

```python
        if state_class == EVStateClass.DISCONNECTED:
            # New session on the next plug-in: don't inherit a stale cooldown.
            es.probe_started_at = None
            es.probe_stopped_at = None
            es.probe_import_over_since = None
```

**3e.** Change the `_run_surplus_probe` call site (line ~905) from:

```python
        if self._run_surplus_probe(plan_first):
```

to:

```python
        if self._run_surplus_probe(now, plan_first):
```

**3f.** Replace `_run_surplus_probe` entirely:

```python
    def _run_surplus_probe(self, now: datetime, plan_first) -> bool:
        """If conditions warrant, take over surplus charging from the EVCS and
        return True (writes active charger mode if a charger_mode_entity is
        configured, regulated current, and start). Otherwise reset any probe
        state and return False so the caller's reactive path runs.

        Owns the clock for the minimum-current dwell: converts the runtime
        timestamps into elapsed-second scalars so ``decide_surplus_probe``
        stays pure.
        """
        cfg = self.config.ev
        es = self.ev_state
        if cfg is None or es is None:
            return False
        batt = self._read_float_or_none(self.config.battery_power_entity)
        grid = self._read_float_or_none(self.config.grid_power_entity)
        soc_pct = self._read_float_or_none(self.config.battery_soc_entity)
        state_class = classify_state(self._read_text(cfg.charger_state_entity))
        soc_kwh = (soc_pct / 100.0 * self.config.battery.capacity_kwh
                   if soc_pct is not None else 0.0)
        forecast_surplus = self._cached_first_pv_kw - self._cached_first_load_kw
        armed = should_probe_surplus(
            currently_armed=es.probe_armed,
            state_class=state_class,
            p_ev_chg_kw=plan_first.p_ev_chg_kw,
            p_sell_kw=plan_first.p_sell_kw,
            soc_kwh=soc_kwh,
            soc_max_kwh=self.config.battery.soc_max_kwh,
            forecast_surplus_kw=forecast_surplus,
            battery_power_available=batt is not None and soc_pct is not None,
            grid_available=grid is not None,
            probe=cfg.probe,
        )
        if not armed:
            if es.probe_armed:
                es.probe_armed = False
                es.probe_current_a = 0
                es.probe_cycles_since_up = 0
                es.probe_cycles_overshooting = 0
                es.probe_started_at = None
                es.probe_import_over_since = None
                # probe_stopped_at deliberately survives: a disarm/re-arm
                # round trip must not launder the restart cooldown.
            return False
        grid_import_w = max(0.0, grid)
        # Soft-import dwell: start the clock on the first tick over the
        # ceiling, stop it the moment import falls back.
        if grid_import_w > PROBE_IMPORT_CEILING_W:
            if es.probe_import_over_since is None:
                es.probe_import_over_since = now
        else:
            es.probe_import_over_since = None
        decision = decide_surplus_probe(
            battery_discharge_w=max(0.0, -batt),
            grid_import_w=grid_import_w,
            forecast_surplus_kw=forecast_surplus,
            current_a=es.probe_current_a,
            cycles_since_up=es.probe_cycles_since_up,
            cycles_overshooting=es.probe_cycles_overshooting,
            probe_on_seconds=_elapsed_seconds(es.probe_started_at, now, 0.0),
            # No recorded stop => no cooldown to serve. Zero would block the
            # very first kick to min for a full cooldown window.
            probe_off_seconds=_elapsed_seconds(
                es.probe_stopped_at, now, math.inf),
            import_over_seconds=_elapsed_seconds(
                es.probe_import_over_since, now, 0.0),
            soc_deficit_kwh=max(
                0.0, self.config.battery.soc_max_kwh - soc_kwh),
            ev=cfg.params,
            probe=cfg.probe,
        )
        # Mode first so any active/passive transition cache invalidation lands
        # before the (forced) current write.
        self._write_ev_charger_mode_active()
        self._write_ev_current(decision.current_a, force=True)
        self._write_ev_start(decision.current_a > 0)
        # Dwell bookkeeping on the transitions, before committing the current.
        if decision.current_a > 0 and es.probe_current_a == 0:
            es.probe_started_at = now
        elif decision.current_a == 0 and es.probe_current_a > 0:
            es.probe_stopped_at = now
            es.probe_started_at = None
        es.probe_armed = True
        es.probe_current_a = decision.current_a
        es.probe_cycles_since_up = decision.cycles_since_up
        es.probe_cycles_overshooting = decision.cycles_overshooting
        return True
```

**3g.** Add the helper near the other module-level helpers at the bottom of `planner.py` (next to `_floor_to_slot`):

```python
def _elapsed_seconds(since: datetime | None, now: datetime,
                     default: float) -> float:
    """Seconds from ``since`` to ``now``, or ``default`` when unset.

    Clamped at zero so a clock step backwards can never read as a negative
    dwell (which would satisfy every "dwell elapsed" comparison at once).
    """
    if since is None:
        return default
    return max(0.0, (now - since).total_seconds())
```

**3h.** Verify the imports from step 3a are all in place — `math`, `PROBE_IMPORT_CEILING_W` and `SurplusProbeParams` are all used by the code above.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_planner.py -k probe -v`
Expected: PASS (all probe tests, including the pre-existing `test_probe_arms_and_drives_manual_in_reactive_branch`, `test_probe_disabled_without_battery_power_entity`, `test_probe_disarms_back_to_auto_when_exporting`, `test_probe_holds_then_steps_down_on_sustained_drain`, `test_probe_does_not_arm_inside_planned_start_gate`, `test_probe_arms_despite_clipped_live_pv`)

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (whole suite)

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/planner.py tests/test_planner.py
git commit -m "$(cat <<'MSG'
feat(planner): wire the probe's minimum-current dwell clocks

Three timestamps on EVRuntimeState, converted to elapsed-second
scalars so decide_surplus_probe stays pure. probe_stopped_at is
excluded from the disarm reset -- otherwise a disarm/re-arm round trip
launders the restart cooldown and the chatter relocates to the arm
boundary. All three clear on disconnect so a freshly plugged car starts
promptly.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 8: Config keys, schema, and setup wiring

These three files are HA-only and not unit-tested in this repo by design (see the `config_flow.py` module docstring). Verification is an import check plus a manual reload.

**Files:**
- Modify: `custom_components/pv_optimizer/const.py` (EV section, after `CONF_EV_SESSION_DONE_SECONDS` / `DEFAULT_EV_SESSION_DONE_SECONDS`)
- Modify: `custom_components/pv_optimizer/config_flow.py:136-174` (`_EV_SCHEMA`)
- Modify: `custom_components/pv_optimizer/__init__.py:58-98`

> **Correction applied during execution — the 600 s floor default makes the 6%
> budget default dead code on small batteries.** Checking
> `probe_floor_outspends_budget` against real configurations:
>
> | Budget | kWh | Floor outspends it? |
> |---|---|---|
> | 0.5% of 15 kWh | 0.07 | yes |
> | 4% of 15 kWh | 0.60 | yes |
> | 6% of 15 kWh | 0.90 | no |
> | **6% of 10 kWh** | **0.60** | **yes** |
>
> The three-phase worst case is `600 s x 6 A x 0.6875 kW/A / 3600 = 0.6875 kWh`,
> so a 6% budget only clears it above ~11.5 kWh of capacity. Stock defaults
> would therefore fire the warning for a large class of users, which destroys
> its value as a signal — if defaults warn, either the default or the warning is
> wrong.
>
> **So this task must also lower the min-on floor default from 600 s to 300 s**
> (worst case 0.34 kWh, self-consistent above ~5.7 kWh of capacity). Raising the
> budget instead was rejected: it would enlarge the battery bite per drain event,
> whereas lowering the floor costs nothing in practice — the floor only binds in
> the pathological "PV vanished the instant charging started" case, while the SoC
> budget is what produces the measured 55-minute runs.
>
> Three places change together:
> 1. `DEFAULT_EV_PROBE_MIN_ON_SECONDS = 300.0` in `const.py` (below).
> 2. `SurplusProbeParams.min_on_seconds` default 600.0 -> 300.0 in `models.py`,
>    so the dataclass and the form agree.
> 3. `test_probe_params_defaults` in `tests/test_ev_controller.py` asserts
>    `min_on_seconds == 600.0` — update it to 300.0.
>
> At a 300 s cadence a 300 s floor still guarantees a full cycle of charging
> before any stop, which is all that is needed to prevent per-tick chatter. The
> chatter test's `min(on_runs) >= probe.min_on_seconds` assertion still holds
> (runs are 55 min), and its 1800 s bar is unaffected.

- [ ] **Step 1: Add the config keys**

In `const.py`, add to the EV configuration-keys block, after `CONF_EV_SESSION_DONE_SECONDS`:

```python
# Curtailed-surplus probe: minimum-current dwell. At min charging current the
# probe has no down-step available, so the only move is to stop -- expensive
# (it cycles the car's connector and costs throughput). These govern it in
# place of the instantaneous discharge ceilings, which cannot tell "surplus
# arriving via a full battery" from "the battery draining into the car".
CONF_EV_PROBE_MIN_ON_SECONDS = "ev_probe_min_on_seconds"
CONF_EV_PROBE_RESTART_COOLDOWN_SECONDS = "ev_probe_restart_cooldown_seconds"
CONF_EV_PROBE_SOC_DROP_PCT = "ev_probe_soc_drop_pct"
CONF_EV_PROBE_IMPORT_HARD_W = "ev_probe_import_hard_w"
```

and to the EV defaults block, after `DEFAULT_EV_SESSION_DONE_SECONDS`:

```python
DEFAULT_EV_PROBE_MIN_ON_SECONDS = 600.0
DEFAULT_EV_PROBE_RESTART_COOLDOWN_SECONDS = 600.0
# Percent of battery capacity, converted to kWh in __init__.py -- matches the
# other SoC fields on the form. 6 % of a 10-20 kWh battery is ~0.6-1.2 kWh,
# putting the stop threshold around 94 % of soc_max and the restart at ~98 %.
# Both are offsets below the configured soc_max, not absolute SoC.
DEFAULT_EV_PROBE_SOC_DROP_PCT = 6.0
DEFAULT_EV_PROBE_IMPORT_HARD_W = 2000.0
```

- [ ] **Step 2: Add the schema fields**

In `config_flow.py`, add to `_EV_SCHEMA`, after the `CONF_EV_SESSION_DONE_SECONDS` entry and before the closing `})`:

```python
    vol.Optional(C.CONF_EV_PROBE_MIN_ON_SECONDS,
                 default=C.DEFAULT_EV_PROBE_MIN_ON_SECONDS): _num(30.0, 3600.0, 30.0, "s"),
    vol.Optional(C.CONF_EV_PROBE_RESTART_COOLDOWN_SECONDS,
                 default=C.DEFAULT_EV_PROBE_RESTART_COOLDOWN_SECONDS): _num(30.0, 3600.0, 30.0, "s"),
    vol.Optional(C.CONF_EV_PROBE_SOC_DROP_PCT,
                 default=C.DEFAULT_EV_PROBE_SOC_DROP_PCT): _num(0.5, 50.0, 0.5, "%"),
    vol.Optional(C.CONF_EV_PROBE_IMPORT_HARD_W,
                 default=C.DEFAULT_EV_PROBE_IMPORT_HARD_W): _num(100.0, 20000.0, 100.0, "W"),
```

No options-flow change is needed: `_OPTIONS_SCHEMA` extends `_EV_SCHEMA`, so the fields appear there automatically and saving reloads the entry via the existing update listener.

- [ ] **Step 3: Build the params at setup**

In `__init__.py`, extend the deferred import line:

```python
    from .models import BatteryParams, EVParams, SurplusProbeParams
```

Add a module-level logger just below the existing `from .const import DOMAIN, PLATFORMS` import at the top of the file:

```python
import logging

_LOGGER = logging.getLogger(__name__)
```

Then inside the `if (ev_state_entity and ...)` block, after `ev_params = EVParams(...)` closes and before `ev_cfg = EVConfig(`:

```python
        ev_probe = SurplusProbeParams(
            min_on_seconds=float(data.get(
                C.CONF_EV_PROBE_MIN_ON_SECONDS,
                C.DEFAULT_EV_PROBE_MIN_ON_SECONDS)),
            restart_cooldown_seconds=float(data.get(
                C.CONF_EV_PROBE_RESTART_COOLDOWN_SECONDS,
                C.DEFAULT_EV_PROBE_RESTART_COOLDOWN_SECONDS)),
            # Percent of capacity -> kWh, matching the other SoC fields.
            soc_drop_kwh=capacity * float(data.get(
                C.CONF_EV_PROBE_SOC_DROP_PCT,
                C.DEFAULT_EV_PROBE_SOC_DROP_PCT)) / 100.0,
            import_hard_w=float(data.get(
                C.CONF_EV_PROBE_IMPORT_HARD_W,
                C.DEFAULT_EV_PROBE_IMPORT_HARD_W)),
        )
        if probe_floor_outspends_budget(ev=ev_params, probe=ev_probe):
            _LOGGER.warning(
                "EV surplus probe: the minimum-on floor (%.0f s at %.1f A) can "
                "drain up to %.2f kWh, more than the %.2f kWh SoC budget, so "
                "the budget will never decide a stop. Lower "
                "%s or raise %s.",
                ev_probe.min_on_seconds, ev_params.min_charging_current_a,
                ev_probe.min_on_seconds * ev_params.min_charging_current_a
                * ev_params.kw_per_amp / 3600.0,
                ev_probe.soc_drop_kwh,
                C.CONF_EV_PROBE_MIN_ON_SECONDS,
                C.CONF_EV_PROBE_SOC_DROP_PCT,
            )
```

Add `probe_floor_outspends_budget` to the deferred imports at the top of `async_setup_entry`:

```python
    from .ev_controller import probe_floor_outspends_budget
```

And pass the params into `EVConfig`, immediately after `params=ev_params,`:

```python
        ev_cfg = EVConfig(
            params=ev_params,
            probe=ev_probe,
            charger_state_entity=ev_state_entity,
```

- [ ] **Step 4: Verify**

The HA-side modules cannot be imported without `homeassistant` installed, so check them by compilation and check `const.py` by import:

```bash
.venv/bin/python -c "
from custom_components.pv_optimizer import const as C
print(C.CONF_EV_PROBE_MIN_ON_SECONDS, C.DEFAULT_EV_PROBE_MIN_ON_SECONDS)
print(C.CONF_EV_PROBE_RESTART_COOLDOWN_SECONDS, C.DEFAULT_EV_PROBE_RESTART_COOLDOWN_SECONDS)
print(C.CONF_EV_PROBE_SOC_DROP_PCT, C.DEFAULT_EV_PROBE_SOC_DROP_PCT)
print(C.CONF_EV_PROBE_IMPORT_HARD_W, C.DEFAULT_EV_PROBE_IMPORT_HARD_W)
"
.venv/bin/python -m py_compile custom_components/pv_optimizer/config_flow.py custom_components/pv_optimizer/__init__.py && echo "compile OK"
.venv/bin/python -m pytest -q
```

Expected:
```
ev_probe_min_on_seconds 600.0
ev_probe_restart_cooldown_seconds 600.0
ev_probe_soc_drop_pct 6.0
ev_probe_import_hard_w 2000.0
compile OK
```
followed by a passing test suite.

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_optimizer/const.py custom_components/pv_optimizer/config_flow.py custom_components/pv_optimizer/__init__.py
git commit -m "$(cat <<'MSG'
feat(ev): expose probe dwell knobs on the EV config screen

Min-on seconds, restart cooldown, SoC drop percent and the hard import
stop. The SoC band is percent-of-capacity to match the other SoC fields
on the form, converted to kWh at setup. Warns when the min-on floor can
outspend the SoC budget.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 9: Documentation

**Files:**
- Modify: `README.md` (EV charging section — find it with the grep below)
- Modify: `PRD.md` (EV charging section)

- [ ] **Step 1: Locate the sections to update**

```bash
grep -n "surplus probe\|curtailed\|PROBE_\|Curtailed" README.md PRD.md
grep -n "session_done_seconds\|current_tolerance_a" README.md PRD.md
```

The second grep finds the EV config-field tables — the new fields belong in the same tables.

> **Correction applied during execution — do not oversell the hold.** A
> controller-side simulation of the real `decide_surplus_probe` against the
> reported scenario (three-phase, 22 kW/32 A so 6 A = 4.1 kW, 15 kWh battery,
> 6% budget = 0.9 kWh, 300 s cycle) gives:
>
> | Surplus | Behaviour | Connector cycles |
> |---|---|---|
> | 3 kW (reported case) | ~55 min on, ~25 min off, repeating | 1 per ~80 min |
> | 0 kW (sun gone) | one bounded drain event, then stays off | 1, then none |
> | ≥ 4.1 kW | no drain at all — probe steps *up*, never sits at min | none |
>
> (Earlier revisions of this note said 50/20 and then 45/15. Both came from a
> simulation whose deficit accounting credited battery recovery on the tick the
> car was still drawing — it gated on the post-decision current rather than the
> one actually in force during the tick. Fixed in `12957c1`; 55/25 is the
> verified figure. The perturbation response is close to linear: doubling
> `soc_drop_kwh` roughly doubles the charging run, halving it halves it.)
>
> The spec's claim that a full battery's discharge is "immediately refilled" by
> curtailed PV holds only while surplus **exceeds** the EV draw. At 6 A drawing
> 4.1 kW against 3 kW of surplus the battery drains net at ~1.1 kW, the deficit
> accrues, and the budget correctly binds after ~48 min. Charging *is*
> uninterrupted for hours when surplus covers the draw — but in that regime the
> probe is above minimum current anyway.
>
> **So when surplus sits below the minimum-current quantum, cycling is
> unavoidable** — it is inherent to 6 A being a 4.1 kW step on three-phase. This
> change makes each cycle roughly 3.5x longer and bounds its battery cost; it
> does not eliminate cycling. Say that plainly in both README and PRD. Do not
> write "charges continuously for hours" without the "while surplus covers the
> car" qualifier. The honest headline is: fewer, longer sessions with a bounded
> worst case, not zero interruptions.

- [ ] **Step 2: Update `README.md`**

In the surplus-probe subsection, replace the description of the down-step / below-min behaviour with the new law. Add this paragraph:

```markdown
**Minimum-current dwell.** At `min_charging_current_a` the probe has no
down-step available, so the only move is to stop — and on a three-phase
charger that minimum is a large quantum (6 A ≈ 4.1 kW), routinely bigger than
the curtailed surplus. Letting the instantaneous drain decide there made the
probe stop every time it reached minimum and restart on the next tick. It no
longer does: with the home battery full and PV clipped, discharging to feed
the car opens headroom the curtailed PV immediately refills, so drain alone
cannot tell "absorbing surplus" from "draining the battery" — only the SoC
trend can.

So at minimum current:

- charging is held for at least `ev_probe_min_on_seconds`;
- past that, it continues while the SoC deficit below `soc_max` stays within
  `ev_probe_soc_drop_pct` of battery capacity — with **no** maximum-on cap, so
  a steady curtailed surplus charges the car for hours uninterrupted;
- it stops once that budget is spent, and will not restart until
  `ev_probe_restart_cooldown_seconds` has elapsed *and* the battery is
  genuinely full again. A sunless spell therefore costs one bounded drain
  event, not a repeating one.

Grid import overrides the minimum-on floor: above `ev_probe_import_hard_w`
immediately, and above the internal 500 W soft ceiling after ~10 minutes.
Above minimum current nothing changed — the two-tier discharge ceilings still
give back one amp at a time, immediately.
```

Add to the EV config-field table:

```markdown
| `ev_probe_min_on_seconds` | 600 | Minimum charging time at min current before a stop is considered |
| `ev_probe_restart_cooldown_seconds` | 600 | Minimum off-time after the probe stops |
| `ev_probe_soc_drop_pct` | 6 | SoC deficit below `soc_max` (% of capacity) that ends a min-current hold |
| `ev_probe_import_hard_w` | 2000 | Grid import that stops charging at once, overriding the min-on floor |
```

- [ ] **Step 3: Update `PRD.md`**

Mirror the behavioural description in the PRD's EV section, at the abstraction level the surrounding PRD text uses, and add the four fields to its EV configuration table.

- [ ] **Step 4: Verify**

```bash
grep -n "ev_probe_min_on_seconds\|ev_probe_soc_drop_pct" README.md PRD.md
```

Expected: hits in both files.

- [ ] **Step 5: Commit**

```bash
git add README.md PRD.md
git commit -m "$(cat <<'MSG'
docs(ev): document the probe's minimum-current dwell

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
MSG
)"
```

---

## Task 10: Full-suite verification

- [ ] **Step 1: Run everything**

```bash
.venv/bin/python -m pytest -q
```

Expected: all tests pass, no errors.

- [ ] **Step 2: Confirm the above-min tests were not weakened**

Tasks 1-9 produce nine commits, so compare against `HEAD~9`:

```bash
git log --oneline -9
git diff HEAD~9 -- tests/test_ev_controller.py | grep -E "^-" | grep -v "^---"
```

Expected: the only *removed* test lines are `test_probe_below_min_goes_to_zero` (Task 4), the old `test_should_probe_disarm_uses_wider_soc_margin` body (Task 2), the `SOC_DISARM_EPS_KWH` import (Task 2), and the relocated `SurplusProbeParams` import (Task 2). If any *other* pre-existing probe test was edited or deleted, the branch split leaked outside minimum current — investigate before finishing.

```bash
git diff HEAD~9 -- tests/test_planner.py | grep -E "^-" | grep -v "^---"
```

Expected: only `test_probe_steps_to_zero_and_stops_on_overshoot` and the `_probe_ev_cfg` signature.

- [ ] **Step 3: Confirm the removed constant is gone everywhere**

```bash
grep -rn "SOC_DISARM_EPS_KWH" custom_components tests
```

Expected: no hits. Only code and tests are scanned — `docs/` still references the constant historically (both probe specs and this plan), which is correct.

---

## Self-review notes

**Spec coverage.** Branch 1 (import escapes) → Tasks 4+5. Branch 2 (gated restart) → Task 3. Branch 3 (above min, unchanged) → asserted untouched in Tasks 4 and 10. Branch 4 (min-current hold) → Task 4. Branch 5 (up-step, unchanged) → preserved verbatim in Task 4, asserted in Task 10. `SurplusProbeParams` + derived disarm margin → Task 1. `restart_eps` derivation → Task 3. `should_probe_surplus` change → Task 2. `PROBE_IMPORT_SUSTAIN_SECONDS` → Task 2 (constant) + Task 5 (tests). `probe_floor_outspends_budget` → Task 6. Planner timestamps, disarm-survival, disconnect-clearing, `_elapsed_seconds` fallbacks → Task 7. Config plumbing + `%`→kWh + warning → Task 8. Docs → Task 9.

**Type consistency.** `SurplusProbeParams` field names are identical in Tasks 1, 6, 7, 8. `decide_surplus_probe`'s four new keyword arguments (`probe_on_seconds`, `probe_off_seconds`, `import_over_seconds`, `soc_deficit_kwh`) plus `probe` match between the Task 3 signature, the Task 4 body, the `_probe` test helper, and the Task 7 call site. `EVRuntimeState` field names (`probe_started_at`, `probe_stopped_at`, `probe_import_over_since`) match between Task 7's dataclass, its `_run_surplus_probe`, and the tests. `probe_floor_outspends_budget(ev=..., probe=...)` is keyword-only in both Task 6 and Task 8.

**Known behaviour changes to existing tests** (all intentional, all handled in-plan): `test_probe_below_min_goes_to_zero` and `test_probe_steps_to_zero_and_stops_on_overshoot` assert the exact behaviour being fixed and are replaced; `test_should_probe_disarm_uses_wider_soc_margin` hard-codes the old 0.5 kWh margin and is rewritten.
