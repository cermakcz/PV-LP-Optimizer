# EV export set-point correction

Status: approved 2026-09-17.

## 1. Problem

The grid set-point commands the **grid meter**, not the battery:

```python
# planner.py:1292 (_attach_physical_soc)
setpoint_w = (sp.p_buy_kw - sp.p_sell_kw) * 1000.0 if active else 0.0
```

The Multiplus defends that number whatever happens on the AC side, so
the battery is the only free variable left. The LP derives `p_sell`
from `load_kw[0]`, which comes from the median load forecaster
(`planner.py:755`) — a forecaster *designed* to reject EV spikes
(PRD §4.2: "one-off spikes — e.g. a single EV-charging session … do
not bias the forecast"). So an EV session is invisible to the number
the inverter is told to defend.

Concretely, with force-PV-export active: PV 6 kW, house load 1 kW, LP
sells 5 kW with the battery idle → set-point `-5000`. Plug in a car
that draws 4 kW and the balance becomes

```
PV 6 = house 1 + EV 4 + export 5 + battery(-4)
```

The battery discharges 4 kW to defend an export target that was sized
against a 1 kW house. The same failure hits force-discharge: LP plans
`p_dis = 3` and `p_sell = 5`, EV adds 4 kW, and the battery must find
7 kW.

Slot-0 PV already gets an asymmetric live clamp for exactly this class
of error (`planner.py:409-415`, PRD §8.2: `pv_kw[0] = min(forecast,
live)`). Load has no equivalent, and deliberately so — see §6.

### 1.1 Secondary effect: a reactive runaway

`decide_reactive` sizes EV current from site grid power, back-adding
what the EV already draws so the loop converges
(`ev_controller.py:345`):

```python
surplus_kw = max(0.0, (-grid_power_w + ev_charging_power_w) / 1000.0)
```

That back-add assumes grid power *responds* to EV draw. Under an
active export set-point it does not — the grid stays pinned at
`-5000` regardless — so the term compounds instead of converging:

| cycle | `ev_charging_power_w` | `grid_power_w` | `surplus_kw` → next EV |
|---|---|---|---|
| 0 | 0 | −5000 | 5 kW |
| 1 | 5000 | −5000 (pinned) | 10 kW |
| 2 | 10000 | −5000 (pinned) | 15 kW → max current |

The battery supplies all of it. Reachable today with force-PV-export
on, no EV target set, and no `charger_mode_entity`. The correction in
§2 removes this by restoring the responsiveness the back-add assumes.

## 2. Algorithm

In `Planner.step()`, between reading `first.setpoint_w` and the
existing `setpoint_tolerance_w` dead-band check (`planner.py:313-320`):

```
if setpoint_w < 0:                                      # exporting only
    ev_excess_w = max(0, ev_measured_w - p_ev_chg_kw[0] * 1000)
    setpoint_w  = min(0.0, setpoint_w + ev_excess_w)
```

`ev_measured_w` is `_read_charging_power_w(cfg.ev.charging_power_entity)`
(`planner.py:1068`), which already normalises `kW` vs `W` by unit
attribute. No explicit clamp against `p_grid_exp_max_kw` is needed:
`ev_excess_w` is non-negative, so the correction only ever moves the
set-point toward zero from a base the LP already kept within bounds.

Three properties, each load-bearing:

**Excess over plan, not raw draw.** `_build_inputs:436-493` models
`p_ev` in the LP whenever target + deadline + connected are set —
**regardless of mode**, car mode included. `p_sell` therefore already
accounts for planned EV charging, and subtracting the full measured
draw would double-count it. When no target or deadline is set,
`ev_params = None`, `p_ev_chg_kw[0] = 0`, and the correction is the
whole draw — correct, because the LP knew nothing about the car.

**One-sided.** An EV drawing *less* than planned leaves the set-point
alone. The surplus then charges the battery instead of being exported,
which is benign (no drain), and the asymmetry mirrors PRD §8.2's
`min(forecast, live)`: only ever cut the plan when reality disagrees,
never speculate the other way.

**Saturates at 0, never crosses into import.** When the car eats more
than the whole export target, the set-point lands on `0`, which is
PRD §8.1 *passive* — control handed back to the Multiplus, which then
runs PV → load → EV → battery natively. Clean degradation to the
inverter's own logic rather than a clamp at an arbitrary floor.

### 2.1 Why export only

Applying the same correction to a positive (import) set-point is
**rejected on breaker-safety grounds**. Under force-charge the
uncorrected behaviour is the safe one:

- Uncorrected: grid stays at the planned import, the battery absorbs
  the EV draw. Total grid draw = `p_buy`.
- Corrected: the battery keeps charging at plan *and* the EV draws.
  Total grid draw = `p_buy + ev_power`, which can exceed the main
  fuse whenever `p_grid_imp_max_kw` is configured optimistically.

The import side has a real and larger version of this bug (§7), but
its fix is current-side, not set-point-side.

### 2.2 Why `step()` and not `_attach_physical_soc`

`_attach_physical_soc` computes `setpoint_w` for **every** slot in the
horizon, and PRD §8.1 makes it the shared source of truth for the
control path and the `sensor.pv_optimizer_plan` series. A live EV
correction is only meaningful for slot 0; applying it there would
smear slot-0's measured EV draw across all 24 planned slots. Keeping
it in `step()` leaves the plan series as pure LP intent and corrects
only the value actually written.

Consequence: for the current slot, the plan attribute's `setpoint_w`
and `sensor.pv_optimizer_planned_grid_setpoint` now deliberately
diverge. `applied_setpoint_w` feeds the sensor (`sensor.py:71`), so
the sensor reports what was written and the plan reports what the LP
intended. README:538-544 currently claims they match and must be
amended (§8).

## 3. Interactions

**Feed-in switch** — unchanged. `feedin = first.p_sell_kw > _FORCE_EPS`
(`planner.py:314`) reads the LP plan, not the corrected set-point. A
reduced export target is still an export, so feed-in stays on. When
the correction saturates to `0`, feed-in remaining on is exactly right:
passive mode needs it to spill genuine surplus.

**Physical SoC projection (PRD §8.3)** — becomes *more* accurate. The
projection follows the LP for active slots; with the correction the
battery actually does what the LP planned, so projection and reality
converge rather than diverge.

**Dead-band** — the existing `setpoint_tolerance_w` check runs
*after* the correction, so a ramping EV cannot spam `number.set_value`
below 50 W of movement. No new rate-limiting needed.

**Surplus probe** — mutually exclusive by construction, not merely
unlikely. `should_probe_surplus` refuses to arm unless
`p_sell_kw <= eps` (`ev_controller.py:123`; it arms only in the
curtailment corner, where the EVCS's export-follower is blind). Every
§8.1 branch that yields a *negative* set-point requires
`p_sell_kw > eps`. So a set-point passing the `< 0` gate guarantees
the probe is disarmed, and an armed probe guarantees the gate fails.
No ordering or interleaving concern.

**Read ordering** — `_apply_ev` already reads EV power at
`planner.py:815`, but runs *after* the set-point write ("EV control.
Always last", `planner.py:325`). `step()` therefore takes its own
read before the set-point block. One extra `reader.get()`, a dict
lookup; `_apply_ev` is left untouched.

## 4. Configuration

No new knobs. The correction is a regression no-op by construction —
`ev_excess_w` is `0` whenever the EV is unconfigured, idle, or drawing
no more than planned — so there is nothing for a default-off flag to
protect. This fixes a defect rather than adding a behaviour, and a
knob defaulting to off would leave the defect in place for everyone
who never finds it.

## 5. Edge cases

1. `cfg.ev is None` → correction skipped entirely, byte-identical
   behaviour.
2. `charging_power_entity` unavailable / unparseable →
   `_read_charging_power_w` returns `0.0` (`planner.py:1068-1079`),
   correction is `0`. Fails toward current behaviour.
3. Passive slot (`setpoint_w == 0.0`) → fails `< 0`, untouched. This
   is the case that would actively break if the gate were `!= 0`: the
   correction would turn `0 → +4000` and force a 4 kW import, wrecking
   PRD §8.1's deliberate hand-off to the inverter.
4. Force-charge / force-hold-import (`setpoint_w > 0`) → untouched
   per §2.1.
5. EV draw exceeds the whole export target → saturates at `0`.
6. Meter lag putting `ev_measured_w` above the true draw → over-
   correction, i.e. less export. Safe direction.

## 6. Why not clamp slot-0 load instead

The symmetric-looking fix, `load_kw[0] = max(forecast, live)`, was
rejected. An LP slot is an hour and instantaneous load is a poor
estimate of an hour's mean: a kettle, an oven cycle or a heat-pump
compressor start would each drag a whole hour's plan. PRD §4.2's
median exists precisely to avoid that, and it pays off on every cycle,
whereas the EV problem occurs only while a car is plugged in.

EV power is different in kind from household load — separately
metered, and a *commanded*, steady draw rather than a spike. That is
what makes the narrow correction safe where the general one is not.

## 7. Out of scope

- **Import-side correction.** Rejected on breaker grounds, §2.1.
- **The negative-price collision.** At default
  `buy_price_threshold = 0.0` (`models.py:186`), `decide_reactive:342`
  commands max current whenever `price_buy <= 0` — with no surplus,
  grid or headroom check — which is exactly when the LP force-charges
  the battery hardest. With no EV target set, `p_ev_chg_kw[0] = 0`,
  and `planner.py:748` has already stripped EV out of the load
  forecast (`subtract_ev = mode == "auto"`), so the EV load is
  invisible to the set-point twice over. PV 0, house 1 kW, battery
  charging 5 kW → `p_buy = 6` → set-point `+6000`; the planner then
  commands 11 kW of EV draw, and the battery discharges 6 kW during
  the negative-price hour while the plan believes it is charging at
  5 kW. This is a distinct defect with a current-side fix (cap the
  cheap-grid current to `p_buy * 1000 - house_load_w`, or suppress the
  reactive cheap-grid path while a force-charge set-point is active).
  Its own spec.
- **A fast set-point path.** The correction runs at planner cadence
  (`update_seconds`, default 300), so up to one cycle of drain is
  accepted per plug-in — ~0.33 kWh at 4 kW. A listener on
  `charging_power_entity` re-applying the set-point from a cached plan
  without an LP solve would cut this to seconds; deferred until the
  cheap version is shown insufficient in real data. Note
  `ev_replan_trigger_entities` (`planner.py:161-184`) excludes that
  entity on purpose, so the fast path must not re-solve.
- **Correcting for non-EV unplanned load** (oven, heat pump). §6.

## 8. Documentation updates

**PRD** gains §8.7 "EV export set-point correction" after §8.6,
stating the rule from §2, the export-only restriction with its
breaker rationale, and the plan-vs-sensor divergence. §7 of this spec
is recorded there as a known, deliberate gap so the negative-price
collision is discoverable.

**README** gains a note in *Active vs passive control* after the
set-point table, and README:538-544 is corrected: the plan attribute's
`setpoint_w` for the current slot is LP intent and may read a larger
export than `sensor.pv_optimizer_planned_grid_setpoint` while an EV
draws more than planned.

## 9. Tests

All in `tests/test_planner.py`, matching the existing
`cycle.applied_setpoint_w` assertion style:

1. **Export corrected** — force-export set-point `-5000`,
   `p_ev_chg_kw[0] = 0`, EV drawing 4000 W → `-1000`.
2. **Excess over plan** — base `-5000`, `p_ev_chg_kw[0] = 3.0`, EV
   drawing 4000 W → `-4000`, not `-1000`.
3. **One-sided** — EV drawing 2000 W against `p_ev_chg_kw[0] = 3.0`
   → `-5000`, untouched.
4. **Passive stays passive** — `setpoint_w == 0.0` with EV drawing
   4000 W → `0.0`. The §5 case-3 trap.
5. **Positive untouched** — force-charge `+3000` with EV drawing
   4000 W → `+3000`.
6. **Saturates at zero** — base `-2000`, EV drawing 6000 W → `0.0`,
   never positive.
7. **Force-discharge treated like force-export** — base `-5000` with
   `p_dis_kw = 3.0` and `p_sell_kw = 5.0`, EV drawing 4000 W →
   `-1000`. Pins that the correction keys off the set-point sign
   alone and does not special-case which §8.1 branch produced it.
8. **No EV configured** — `cfg.ev is None` → regression no-op.
9. **Unavailable power entity** → correction `0`, set-point unchanged.
10. **Dead-band still applies** — a correction smaller than
    `setpoint_tolerance_w` triggers no `number.set_value` call.
