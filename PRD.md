# PV LP Optimizer — Product Requirements Document

## 1. Purpose
A Home Assistant custom integration (`pv_optimizer`) that minimizes the total
electricity cost of a household equipped with a photovoltaic plant and a battery
(controlled via a Victron Cerbo GX that is already integrated in HA). It produces
a rolling plan over a configurable horizon (default: end of the last known
tariff hour, typically 24–48 h) using **linear programming** and writes the
control set-points for the *current* slot back to HA entities.

The optimizer trades off:
- buying energy from the grid at hourly buy prices,
- selling surplus energy at hourly sell prices,
- (dis)charging the battery,
- amortized battery wear cost per kWh delivered out of the battery.

## 2. Scope and Non-Goals
**In scope**
- Reading sensor entities from HA for the inputs listed in §4.
- Solving an LP over a discrete time horizon split into uniform slots.
- Writing the optimal current-slot grid set-point and feed-in switch state to
  user-configured HA entities (typically Victron-backed `number`/`switch`).
- Exposing diagnostic sensors with the latest plan, expected cost and savings.
- Configurable via the HA UI (config flow + options flow).
- Unit-tested business logic (optimizer + coordinator with mocked `hass`).
- Optional, brand-agnostic **EV charging control** (§9): the wall-box is
  either co-optimized inside the same LP (when the user sets a charge
  target + deadline) or driven by a reactive controller from PV surplus /
  cheap grid. Entirely opt-in — leaving the EV entities unset makes every
  EV code path a regression no-op.

**Out of scope (initially)**
- Direct MQTT/dbus communication with the Cerbo (we go through HA entities).
- Demand-charge tariffs, tiered/block tariffs, capacity tariffs.
- Multi-vector optimization *beyond* EV charging (heat pumps, hot water,
  HVAC) — left as future work.
- Stochastic / robust optimization — we use deterministic forecasts.

## 3. High-Level Architecture
```
HA sensor entities ──► Coordinator ──► Optimizer (pure) ──► OptimizerResult
                          │                                       │
                          ├──► number.set_value (grid setpoint) ◄─┘
                          ├──► switch.turn_on/off (feed-in)
                          ├──► EV charger writes (current / start / mode)
                          └──► diagnostic sensors (plan, cost)
```
- `optimizer.py` is **pure Python**, no HA imports — fully unit-testable.
- `ev_controller.py` is pure as well: charger-state classification plus the
  reactive and surplus-probe decision functions (§9).
- `load_forecaster.py` is pure apart from the recorder query it is handed.
- `coordinator.py` (subclass of `DataUpdateCoordinator`) reads states, builds
  an `OptimizerInputs`, runs the optimizer, and applies the first slot.
- `config_flow.py` collects all entity IDs and parameters via the UI.
- `select.py` / `number.py` / `datetime.py` / `switch.py` publish the
  integration-created EV control entities (§9.1); they exist only when the
  EV feature is configured.

## 4. Inputs (sensor entities, configurable)
| Input | Required | Notes |
|---|---|---|
| Current household load (W) | yes | instantaneous; used as fallback if no forecast |
| Battery state of charge (%) | yes | converted to kWh via configured capacity |
| PV current power (W) | yes | informational + plan execution feedback |
| Grid power (W, signed) | yes | informational |
| Buy price today, hourly (`your_currency`/kWh) | yes | `list[24]` (Nordpool-style) **or** `dict[iso_timestamp, float]` (timestamp-keyed; preferred — see §4.1) |
| Buy price tomorrow, hourly | optional | same shape; if absent, the horizon is truncated to today's last known hour |
| Sell price today, hourly | yes | may equal buy with FiT subtractor |
| Sell price tomorrow, hourly | optional | as above |
| PV power forecast, hourly (kW) | yes | e.g. `forecast.solar` |
| Load forecast, hourly (kW) | optional | escape hatch; if unset the built-in forecaster (§4.2) is used |
| Feed-in allowed override | optional | external switch forcing feed-in off |
| Force PV export toggle | optional | external `switch`/`input_boolean`/`binary_sensor`; when on, opts into the active-export branch in §8.1 |
| Battery power (W, signed) | optional | negative = discharging. Read as `discharge_w = max(0, −value)`; **required** by the §9.6 curtailed-surplus probe, which stays disarmed without it |
| EV charger state | optional | string state classified per §9.2. Together with EV charging power, EV max-current and the three static EV params below, this is what enables the whole EV feature |
| EV charging power (W or kW) | optional | instantaneous; drives surplus tracking, session-done detection, the session integrator, and the §4.2 forecaster correction. Unit taken from `unit_of_measurement` (`kW` scaled, anything else treated as W) |
| EV session energy (kWh) | optional | energy delivered this session. When absent the planner integrates EV charging power itself (§9.3) |

### 4.1 Price-attribute shapes & staleness contract
The tariff attribute (configurable name, default `today` / `tomorrow`) may be
any of four shapes — the planner auto-detects which is in use, in this order:

- **`list[float]` of length 24** under the configured attribute name —
  legacy Nordpool/OTE shape. Indexed by hour of the local day; assumed to
  always represent "today's" 24 hours.
- **`dict[str, float]`** under the configured attribute name, keyed by ISO 8601
  timestamps with timezone offset (e.g. `"2026-05-01T06:00:00+02:00"`).
- **Dict under any other attribute name** — if the configured attribute is
  absent, the planner scans every other dict-valued attribute and picks the
  largest one whose keys parse as ISO timestamps. This lets user-built
  template sensors publish under whatever wrapper name feels natural
  (`prices`, `raw_today`, …) without changing planner config.
- **Top-level ISO-keyed attributes** — the entity's *entire* `attributes`
  dict carries hour-timestamp keys directly, with no wrapping attribute
  (this is what `spot_hodinovy_tarif` and similar Czech/CEE plugins do).
  Unrelated metadata keys (`unit_of_measurement`, `friendly_name`, …) are
  ignored.

The dict shapes are preferred because they carry an explicit "as-of" anchor.

When the dict shape is used:
- Today and tomorrow may be merged into a single entity attribute, or split
  across the today/tomorrow entities — both work.
- If the **current** slot's hour key is absent, the planner refuses to run
  and surfaces a `last_error` ("price data for current slot … is missing —
  stale tariff sensor?"). This protects against a frozen/last-good price
  source where a `list[24]` would silently look fresh.
- If a **future** slot's hour key is absent, the horizon is truncated at the
  first gap. The optimizer simply plans over the shorter window.

### 4.2 Built-in load forecaster
When the optional `load_forecast_entity` is unset, the integration uses an
internal forecaster (`load_forecaster.py`) that derives a per-slot expected
household load from the recorder history of the configured load-power entity.

Algorithm: **median over the last *N* days at the same hour-of-day**, with
optional cap and weekday awareness.

```
forecast[H] = clip(median(load[d, H] for d in last_N_days), 0, cap_kw)
```

Median (rather than mean) is used so that one-off spikes — e.g. a single
EV-charging session, an unusual guest day, an HVAC outlier — do not bias the
forecast. No spike *detection* is required.

Configuration knobs (with defaults):
| Knob | Default | Notes |
|---|---|---|
| `lookback_days` | `7` | Window used per hour-bucket. |
| `cap_kw` | `None` | Optional hard ceiling (kW). |
| `weekday_aware` | `false` | If true, only days with the same weekday as the target slot contribute; needs ~4 weeks of history to be useful. |

The forecaster is consumed directly by the planner (no roundtrip through HA
state). A diagnostic sensor `sensor.pv_optimizer_load_forecast` is also
published with `attributes.kw_per_slot` and `attributes.lookback_days_used`
for observability — nothing reads it back. Setting `load_forecast_entity` to
any user-supplied entity disables the built-in forecaster (escape hatch).

If no usable history exists (fresh install, or no samples at the target hour
for any of the lookback days), the planner falls back to the current
load-power reading for that slot — same behavior as if no forecaster were
present at all.

**EV correction.** When an EV is configured (§9), the
forecaster also reads the EV charging-power entity's history and subtracts
the per-bucket EV average from the per-bucket load average before taking
the median (bucket-level clamp at 0). The subtraction is gated on the
*current* EV mode being `auto` — the only mode in which the LP also
budgets `p_ev_chg` separately. In `car`/`off` the EV is treated as
opaque household load and historical draws are included.

## 5. Outputs (controlled HA entities, configurable)
| Output | Type | Semantics |
|---|---|---|
| Grid set-point (W) | `number` | Victron AcPowerSetPoint. Sign convention:
  positive = target import (W) at the grid meter, negative = target export. |
| Feed-in allowed | `switch` | When off, optimizer must set `p_sell[t] = 0`. |
| (optional) Max charge power | `number` | If exposed, plan respects it. |
| (optional) Max discharge power | `number` | As above. |
| (EV) Charger max current (A) | `number` | Required for the EV feature. Written as an integer, subject to the `ev_current_tolerance_a` dead-band (§9.7). |
| (EV, optional) Charger start switch | `switch` | Written `on` iff the commanded current > 0. Written **every** tick, no dead-band (§9.7). |
| (EV, optional) Charger native mode | `select`-like | The charger's own mode entity. The planner writes the configured *active* option when it wants to own the current, the *passive* option when it hands surplus decisions back to the charger (§9.4). |

## 6. Configuration (config flow)
Single flow, five steps:
1. **Entities** — pick all sensor & control entity IDs from §4 and §5,
   including the configurable price-attribute names (default `today` /
   `tomorrow`) and the optional feed-in override and force-PV-export
   toggles.
2. **Battery** — usable capacity (kWh), SoC min/max (%), max charge/discharge
   power (kW), round-trip efficiencies (η_chg, η_dis), **cycle cost
   (`your_currency`/kWh delivered out of the battery — LCOS convention)**
   ≈ `battery_price / (cycles · usable_kWh · η_rt)`, **soft SoC health
   floor (%; default = SoC min, i.e. disabled)** and **low-SoC dwell
   penalty (`your_currency`/(kWh·h); default `0`)** — together they
   implement the §8.5 soft floor.
3. **Solver** — slot length (default 60 min), horizon (default 24 h, max 48 h),
   update interval (default 5 min), max grid import/export (kW), set-point
   write tolerance (W), **minimum sell price (`your_currency`/kWh; default
   `0`)** — slots whose all-in sell price falls below this floor are gated
   off (see §8.4).
4. **Load forecast** — lookback days (default 7), optional cap (kW; 0 = off),
   weekday-aware mode (default off). Skipped when an external
   `load_forecast_entity` was selected in step 1.
5. **EV charging** — all fields optional; leaving them blank disables the
   feature (§9). Charger entity IDs (state, charging power, optional session
   energy, max-current `number`, optional start `switch`, optional native-mode
   entity plus its *active* / *passive* option strings), and the static
   parameters: max charging power (kW), max charging current (A), min charging
   current (A, default 6), cheap-grid buy-price threshold (default 0), car
   battery capacity (kWh), current write tolerance (A, default 1),
   session-done power (W, default 100) and session-done duration (s,
   default 60).

The EV feature activates only when **all six** required pieces are present:
charger-state entity, charging-power entity, max-current entity, max charging
power, max charging current, and car battery capacity. Any missing one leaves
`ev = None`, in which case no EV entities are created, no EV variables enter
the LP, and no EV writes ever fire.

Currency is left unspecified on purpose: the planner is currency-agnostic and
all monetary fields use the placeholder unit `your_currency`. As long as buy
price, sell price and cycle cost are expressed in the same currency, the
diagnostic cost / savings sensors come out in that same currency.

The options flow re-exposes **every** step — Entities, Battery, Solver,
Load-forecast and EV — as one combined form, pre-filled with the currently
active values, so every entity binding and parameter can be re-pointed after
install without removing the integration (this is also how the EV feature is
enabled on an existing install). Tariff
surcharges (distribution, taxes) are expected to live in the user's price
template (e.g. a Jinja `template.sensor` that adds them on top of the spot
price), not in the integration itself.

## 7. LP Formulation
Discrete slots `t = 0..T-1` of length `Δt` hours. All variables ≥ 0 unless
noted; `pv[t]`, `load[t]`, `price_buy[t]`, `price_sell[t]`, `feedin_ok[t]`
are parameters.

**Decision variables**
- `p_buy[t]`            — grid import power (kW)
- `p_sell[t]`           — grid export power (kW), upper-bounded by 0 when `!feedin_ok[t]`
- `p_chg[t]`, `p_dis[t]`— battery charge / discharge power (kW)
- `p_curt[t]`           — curtailed (spilled) PV power (kW). Free slack that
  lets the balance close when PV exceeds everything the system can absorb —
  battery full, export gated off. Without it those slots would be infeasible.
- `soc[t]`              — energy stored at *start* of slot (kWh), `soc_min ≤ soc[t] ≤ soc_max`
- `p_ev[t]`             — EV charging power (kW). **Only created when the EV
  path is engaged** (§9.3); otherwise a constant `0` and the LP is bit-for-bit
  the pre-EV problem.
- `ev_deficit`          — scalar slack for unmet EV energy (§9.3), created with
  `p_ev`.

**Constraints (per t)**
- Power balance:
  `pv[t] + p_dis[t] + p_buy[t] = load[t] + p_ev[t] + p_chg[t] + p_sell[t] + p_curt[t]`
- SoC update:    `soc[t+1] = soc[t] + Δt·(η_c·p_chg[t] − p_dis[t]/η_d)`
- Bounds: `0 ≤ p_chg[t] ≤ P_chg_max`, `0 ≤ p_dis[t] ≤ P_dis_max`,
  `0 ≤ p_buy[t] ≤ P_imp_max`, `0 ≤ p_sell[t] ≤ P_exp_max·feedin_ok[t]`
- EV window: `0 ≤ p_ev[t] ≤ EV_max_kw` for `t ∈ [ev_start, ev_deadline)`,
  else `p_ev[t] = 0` (§9.3)
- EV energy (soft): `ev_deficit ≥ ev_target_kwh − Σ_t Δt·p_ev[t]`,
  `0 ≤ ev_deficit ≤ ev_target_kwh`
- Terminal SoC ≥ `soc_target` (default = initial SoC, prevents end-of-horizon dump)

**Objective (minimize)**
```
Σ_t Δt · [ price_buy[t]·p_buy[t] − price_sell[t]·p_sell[t]
           + c_cycle·p_dis[t]
           + c_low_soc·deficit[t] ]
  + Σ_t eps_ev_early · t · p_ev[t]
  + c_ev_deficit · ev_deficit
```
`c_cycle` is amortised on the discharge leg only — interpret the input as
"currency per kWh delivered out of the battery" (LCOS convention). The
matching charge-side wear is implicit in the input number, which the
config recipe derives as `battery_price / (cycles · usable_kWh · η_rt)`.
Round-trip efficiency (`η_rt < 1`) plus a tiny `eps_cycle · (p_chg+p_dis)`
regulariser keep the LP from co-charging/discharging in the same slot. A
matching `eps_curt · p_curt` term makes curtailment the last resort rather
than a free dump. The optional `deficit[t] ≥ max(0, soc_health − soc[t])`
slack and its rate `c_low_soc` implement the §8.5 soft health floor; both
default off (`c_low_soc = 0`, `soc_health = soc_min`) so the term vanishes
for upgrading users.

One **binary** per slot (`export_on[t]`) gates import against export via
big-M, since a single net-metered connection cannot physically do both at
once; without it the LP fabricates an import→export round-trip whenever
`price_sell ≥ price_buy`. The big-Ms are the variables' own upper bounds,
so the gate adds no slack beyond existing ones. Strictly this makes the
problem a MILP, but with one binary per slot and an otherwise-LP
relaxation it solves in the same order of time.

The two EV objective terms:
- `eps_ev_early = 1e-6` tilts equal-cost EV schedules toward the earliest
  in-window slot. Since the battery can shuttle energy between in-window
  slots, the slot choice is otherwise degenerate; the tilt makes the plan
  deterministic across solvers and hedges against forecast error and a
  tightening deadline. Far too small to override any real price difference.
- `c_ev_deficit = 100 · max(max_t price_buy[t], 0.01)` prices the unmet-energy
  slack above the worst realistic buy price, so the LP prefers importing at a
  peak price over missing the target — while keeping the problem **feasible**
  when the deadline is genuinely unreachable (plugged in too late, deadline
  too soon). Graceful degradation instead of `OptimizerError`; the residual
  is surfaced as `sensor.pv_optimizer_ev_deficit_kwh` (§10).

Solver: **PuLP** with HiGHS preferred (`highspy`, in-process, no subprocess
exec, ships pre-built wheels) and the bundled CBC binary as fallback. The
chosen backend is cached for the lifetime of the process.

## 8. Plan Execution
Each coordinator tick:
1. Build `OptimizerInputs` from the current HA state and forecasts.
2. Refine slot-0 PV against live measurements (§8.2).
3. Solve the LP for `T` slots from "now".
4. For **every** slot, decide **active** vs **passive** (§8.1), derive the
   set-point it implies, and project a *physical* SoC track (§8.3). The
   control path and the dashboard projection therefore share one source of
   truth for the predicates — slot 0's derived set-point is exactly what
   step 5 writes.
5. Write slot 0's set-point in **W** with sign per §5 via `number.set_value`
   on the configured entity, subject to the dead-band.
6. Toggle the feed-in switch: ON iff `p_sell[0] > ε` and global feed-in is allowed.
7. Apply EV control (§9) from the LP's slot 0 — always last, so it reads the
   latest first-slot price and what the rest of the plan already decided
   (notably whether this slot exports, which determines who owns surplus
   charging).
8. Publish diagnostic sensors with the full plan.

Failures (missing sensor, infeasible LP, write error) leave the previous
set-point in place, log a warning, and surface a `last_error` attribute.

### 8.1 Active vs passive control
The Multiplus's native EMS already runs self-consumption (PV → load →
battery → grid surplus, gated by the feed-in switch) when the set-point is
0. Forcing a non-zero set-point speculatively is brittle to forecast
error: a PV undershoot would make the inverter discharge the battery to
defend a target it was never asked to defend. The planner therefore only
overrides the EMS when the LP actively wants to move energy between the
battery and the grid:

| Branch | Predicate (slot 0) | Set-point |
|---|---|---|
| force-discharge | `p_dis > ε ∧ p_sell > ε` | `(p_buy − p_sell) · 1000` (negative) |
| force-charge    | `p_buy > ε ∧ p_chg > ε` | `(p_buy − p_sell) · 1000` (positive) |
| force-export *(opt-in §8.2)* | `toggle_on ∧ p_sell > ε ∧ p_chg < ε ∧ p_dis < ε` | `(p_buy − p_sell) · 1000` (negative) |
| force-hold-import *(§8.6)* | `p_buy > ε ∧ p_chg < ε ∧ p_dis < ε` | `(p_buy − p_sell) · 1000` (positive) |
| passive | none of the above | `0` |

`ε = 1e-3 kW`, shared between control and the §8.3 projection so they
agree on which slots are active.

### 8.2 Force-PV-export & live-PV refinement
Pure-export slots (LP wants to sell PV with the battery idle) are passive
by default. In some scenarios the user wants the surplus pushed to the
grid instead of self-consumed into the battery — typically when a high
morning sell price will be followed by cheap noon recharge anyway. The
optional `force_pv_export_entity` toggle activates the third branch above.

To make force-export safe against forecast error the planner refines slot 0
before solving:

```
pv_kw[0] := min(forecast[0], max(0, live_avg))
```

`live_avg` is a time-weighted average of the configured `pv_power_entity`
over the last `update_seconds` (the coordinator's update cadence, mirrored
into planner config). Recorder-backed; if no samples cover the window
(e.g. fresh install) the forecast is used unchanged. The clamp is
one-sided on purpose — a transient PV burst above the forecast must never
make the LP commit to a sell that the next minute's cloud will retract.

### 8.3 Physical SoC projection
The LP's `soc_start_kwh` is bookkeeping: in passive PV-surplus slots it
stays flat (the LP curtails what it doesn't actively transfer). To show
the user where the battery will *physically* end up, the planner attaches
a second SoC track `soc_physical_kwh` to every slot:

- For active, force-export, and force-hold-import slots, the projection
  follows the LP exactly (`Δsoc = Δt · (η_c · p_chg − p_dis / η_d)`; this
  is `0` for force-export and force-hold-import by construction).
- For passive slots, the projection re-runs the inverter's
  self-consumption rule: surplus PV charges the battery up to `soc_max`,
  deficit discharges down to `soc_min`, the rest spills to grid /
  curtailment.

Plotting both tracks reveals where bookkeeping and reality diverge.

### 8.4 Minimum sell price floor
A user-configurable floor on the slot sell price (default `0`). For every
slot the planner computes:

```
feedin_allowed[t] := feedin_global ∧ (price_sell[t] ≥ min_sell_price)
```

`feedin_allowed=False` is enforced by the optimizer as `p_sell[t] = 0`
(via the per-slot `sell_ub` in §7), so a sub-threshold slot can neither
export PV nor discharge the battery to the grid. The LP then plans
around the lost revenue — typically by storing PV in the battery for a
later, higher-priced slot, or by curtailing if the battery is full.

When the gate fires for slot 0 the §8.1 set-point logic naturally falls
into the passive branch (since both `p_sell` and `p_dis` are 0) and the
feed-in switch goes off (predicate `p_sell[0] > ε` is false), which also
prevents the inverter's native EMS from exporting at the floor price.
Useful when the marginal sell revenue (e.g. 0.10 CZK/kWh) doesn't justify
running the inverter at full export power. Default `0` disables the floor
entirely (any non-negative sell price clears the gate).

### 8.5 Soft SoC health floor
The hard `soc_min` bound prevents the LP from going below the inverter /
chemistry safety floor, but says nothing about *dwelling* there. Without
further input the LP happily drains to `soc_min` mid-afternoon and parks
the battery at the bottom for 18 hours if the next day's prices don't
beat today's; this is fine for revenue but bad for calendar aging,
particularly on NMC and to a lesser extent LFP.

Two opt-in battery parameters add a soft floor *above* `soc_min`:

| Parameter | Default | Unit | Meaning |
|---|---|---|---|
| `soc_health_pct` | `soc_min_pct` | % | SoC the LP is encouraged to stay above |
| `low_soc_penalty_per_kwh_h` | `0` | currency / (kWh·h) | dwell-cost rate below the floor |

Mechanics: per slot the LP gains a slack `deficit[t] ≥ max(0,
soc_health − soc[t])` (and an equivalent `deficit_end` on `soc_end`),
priced into the objective at `Δt · c_low_soc · deficit[t]`. The LP can
still dip below the floor — that's the *soft* part — but only when the
slot's marginal economic gain (sell revenue, displaced buy, etc.) beats
the cumulative penalty of the resulting low-SoC dwell over the rest of
the horizon. A typical configuration (`soc_health = 40 %`,
`c_low_soc = 0.5 CZK/(kWh·h)`) penalises a 1 kWh shortfall at ~0.5 CZK/h,
enough to nudge the LP to recharge during cheap hours, weak enough to
yield to a real evening peak.

Defaults are deliberately a regression no-op: with `soc_health = soc_min`
or `c_low_soc = 0` the slack variables and penalty term aren't created
at all (`health_active = False` short-circuits the LP construction).

### 8.6 Force-hold-import
The §8.1 baseline rule "passive when the LP isn't moving energy between
the battery and the grid" implicitly assumes that the inverter's native
EMS, given set-point `0`, does the same thing the LP planned. That holds
for PV-surplus slots (both prefer self-consumption) but breaks down in
two cases where the LP plans pure grid coverage of the load with the
battery idle:

1. The §8.5 health floor (or any near-floor condition) makes further
   discharge expensive enough that the LP would rather buy from the grid
   — but the inverter's EMS doesn't know about that penalty and drains
   the battery anyway.
2. The configured `soc_min` sits above the inverter's BMS floor (e.g.
   reserve set to 20 % when the BMS allows 10 %); the LP respects
   `soc_min`, the EMS doesn't.

In both cases passive set-point `0` causes the inverter to silently
violate the plan. The fourth branch in the §8.1 table fixes this:

```
force_hold_import := p_buy > ε ∧ p_chg < ε ∧ p_dis < ε
                  → set-point := (p_buy − p_sell) · 1000
```

The set-point pins the grid draw to the LP's planned import (positive,
typically tracking load), which forces the inverter to cover the load
from the grid and leaves the battery idle. Always-on, no toggle: when
the predicate doesn't fire (e.g. battery already at hard floor with no
arbitrage to defend) the forced positive set-point produces the same
physical behaviour as set-point `0`. The §8.3 projection mirrors the
predicate so the displayed physical SoC stays flat.

## 9. EV Charging
Optional, brand-agnostic wall-box control. Everything below is inert unless
the six required config pieces from §6 are present. The design goal is that
each controller runs only where it is competent: the LP owns the charger when
the user has committed to a target, the charger's own firmware owns surplus
tracking when surplus is visible to it, and the planner steps in only where
neither can see the truth (§9.6).

EV control is applied at the end of every coordinator tick, after the
set-point and feed-in writes, from `plan_first` (the LP's slot 0).

### 9.1 Mode surface and integration-created entities
A single `select.pv_optimizer_ev_mode` governs top-level behaviour.

| Mode | Behaviour |
|---|---|
| `auto` (default) | LP-planned charging when a target and a future deadline exist (§9.3); otherwise the reactive path (§9.4), which may be pre-empted by the surplus probe (§9.6). Deliberately does **not** react to the car's "requesting" signal. |
| `car` | "Just charge, now." Writes active charger mode + `max_charging_current_a` + start ON every tick. **Sticky** — stays until the user switches away, unless auto-return is enabled (§9.5). |
| `off` | The planner writes nothing to any EV entity. Diagnostic sensors keep reading inputs, so the user still observes charger state while the integration is a passive observer. |

The integration creates six entities, all `RestoreEntity` (they survive a
restart) with pinned entity IDs so the planner's reads line up regardless of
the slug HA would derive:

| Entity | Purpose |
|---|---|
| `select.pv_optimizer_ev_mode` | `auto` / `car` / `off`. A restored legacy `manual` migrates to `car` before the options check, so pre-rename installs survive. |
| `number.pv_optimizer_ev_target_kwh` | Session charge target in kWh. |
| `number.pv_optimizer_ev_target_pct` | Same target as % of `ev_car_battery_kwh`. The planner takes the **larger** of the two implied kWh values, so neither field has to be cleared when the other is set. |
| `datetime.pv_optimizer_ev_deadline` | Deadline for LP-planned charging. |
| `datetime.pv_optimizer_ev_planned_start` | Optional "I'll be home by" time (§9.3). |
| `switch.pv_optimizer_ev_car_auto_return` | Opt-in auto-return out of `car` mode (§9.5). Default OFF. |

The user's target is **not** auto-reset between sessions — it persists until
they change it.

### 9.2 Charger-state classification
`ev_charger_state_entity` is a free-form vendor string, classified by
case-insensitive **substring** match into three classes, in precedence order
`DISCONNECTED` → `CONNECTED_REQUESTING` → `CONNECTED_IDLE`:

| Class | Default needles |
|---|---|
| `DISCONNECTED` | `disconnect`, `unplug` |
| `CONNECTED_REQUESTING` | `charging`, `waiting_for_sun`, `waiting_for_start`, `waiting_for_rfid`, `waiting_for_time`, `wait sun`, `wait_sun`, `wait time`, `wait start`, `wait rfid` |
| `CONNECTED_IDLE` | `charged`, `connect`, `low_soc` |

An unrecognised state falls through to `CONNECTED_IDLE`, the conservative
answer (plugged in, not asking for power). `None` / `""` / `unknown` /
`unavailable` classify as `DISCONNECTED`, so a stale or failed input parks
the feature rather than driving the charger blind.

Two needle choices are deliberate and load-bearing:
- A bare **`idle`** is *not* a `DISCONNECTED` needle: several firmwares
  (Wallbox, SMA) spell connected-but-not-charging as `charging_idle` /
  `connected_idle`, and substring precedence would mis-route those to
  "no car". Bare `idle` therefore lands on the conservative fallback.
- **`low_soc`** — the charger-side pause when the *home* battery is under
  the user's floor — classifies as `CONNECTED_IDLE`, so the planner respects
  the charger's own home-battery protection unless the LP plan or `car` mode
  explicitly overrides it.

### 9.3 LP-planned charging
Engaged when `mode == auto` **and** all of: the car counts as connected, the
remaining target is > 0, a deadline exists and is in the future, and the
resulting slot window is non-empty. Then `p_ev[t]` enters the LP (§7) and EV
charging is co-optimized against PV, battery, prices and the wear cost.

**Remaining energy.** `remaining = max(0, target_kwh − session_energy)`,
where session energy is the configured `ev_session_energy_entity` when
present, otherwise a planner-side trapezoidal integration of EV charging
power between ticks. The integrator resets on the `DISCONNECTED → connected`
transition. `remaining` (not the raw target) is what the LP must deliver, so
every tick re-plans against what is actually left.

**Charging window.** `[ev_start_index, ev_deadline_index)` over the horizon:
- `ev_deadline_index` is the first slot at or after the deadline floored to
  slot resolution — inclusive of that slot, since charging during the
  deadline hour is allowed. A deadline past the horizon clamps to the end.
- `ev_start_index` is 0 unless a **planned start** in the future is set, in
  which case it is the slot containing that time.

**Planned start** means "assume the car will be plugged in by then": the
planner treats the car as connected even while it is physically absent, and
reserves the LP window from that slot on. This lets the user pre-schedule a
charging block before arriving home. `_apply_ev` keeps a parallel gate — before
the planned start it drives **no** charging at all, not even from free PV
surplus, because a user who scheduled a later start may have done so
deliberately (cheaper or negative prices later); soaking solar early could be
the wrong trade. During the gate the charger is handed to its passive mode
with start OFF, which also self-corrects a stale ON left by a prior session.

**Slot-0 translation.** `current_a = round(clamp(p_ev[0] / kw_per_amp,
min_a, max_a))`, with two special cases: disconnected or `p_ev[0] == 0` writes
`0` (the LP plan outranks the car's request signal), and a converted current
*below* `min_a` clamps **upward** rather than skipping. That is the opposite
of the reactive path's skip-below-min (§9.4) and is intentional: the user has
committed to a target, so a minor slot-0 overshoot is acceptable and the next
tick re-plans against a reduced `remaining`.

`kw_per_amp = max_charging_power_kw / max_charging_current_a` is the *only*
voltage/phase abstraction in the system — declared by the user as a
(power, current) pair at the charger's maximum, then applied as a linear
factor everywhere. No phase or voltage math anywhere in the codebase.

**Degradation.** When the deadline passes with energy still owed, the window
empties, `ev_deficit` absorbs the gap (§7), the EV plan becomes the zero plan,
and the reactive path takes over any remaining opportunistic charging. No EV
`cycle_cost` term is added: the home battery's `p_dis` already carries its own
wear cost, so the LP charges the car from the battery only when
`cycle_cost < price_buy` — which is exactly the right test.

### 9.4 Reactive charging
Runs in `auto` when the LP path is not engaged (no target, no deadline,
deadline past, or target already met). Two variants, chosen by whether the
user wired the charger's native mode entity:

**With a mode entity (preferred).** The planner evaluates one per-tick
predicate, `cheap_grid_active = price_buy[now] ≤ ev_buy_price_threshold`
(no trigger/release asymmetry), and:
- cheap → write the *active* option + `max_charging_current_a` + start ON;
- not cheap → write the *passive* option + `max_charging_current_a` + start ON,
  handing the surplus decision to the charger's own solar logic.

Start is written ON in **both** branches: the charger can only surplus-charge
with its start switch on, so leaving it untouched would strand a stale OFF
(e.g. one left by the planned-start gate) and silently block surplus charging.

**Without a mode entity.** The charger is assumed to sit in active-control
mode permanently (a one-time user setup) and the planner owns everything:

```
if disconnected:                    current = 0
elif price_buy ≤ threshold:         current = max_a          # cheap grid
else:
    surplus_kw = max(0, (−grid_w + ev_charging_w) / 1000)
    target_a   = surplus_kw / kw_per_amp
    current    = 0 if target_a < min_a else trunc(clamp(target_a, min_a, max_a))
```

Two details carry the design:
- The `+ ev_charging_w` back-add makes the loop **converge**. Without it the
  controller reads its own consumption as "no surplus" and ramps itself to
  zero. The home battery takes surplus first via passive self-consumption, so
  `grid_w < 0` genuinely means the battery is full and there is leftover.
- The result is **truncated**, not rounded: with 7.5 A of headroom, rounding
  to 8 A would pull the last half-amp from the grid. Truncating to 7 A keeps
  the site on the export side of zero.

### 9.5 Car mode and session-done
`car` writes active mode → max current → start ON every tick, in that order
(some chargers only honour a current after their mode is set). It is sticky
by default; the user leaves it explicitly.

With `switch.pv_optimizer_ev_car_auto_return` ON, the planner returns the mode
select to `auto` when the session is done, defined as either:
- the charger classifies to `DISCONNECTED`, or
- it classifies to `CONNECTED_IDLE` **and** EV power has stayed below
  `session_done_power_w` for at least `session_done_seconds`, **and** the car
  drew at least `session_done_power_w` at some point this session.

That last conjunct — the `car_session_charging_seen` guard — exists because
some chargers report a connected-idle state while gating power themselves;
without it a car that never started would instantly "finish". The flag clears
whenever the mode leaves `car`, the car disconnects, or auto-return is
switched off, so a later re-enable starts from a clean baseline.

With auto-return OFF and the car already full, the planner keeps writing max
current indefinitely and the charger just reports `Charged`. That is the
accepted cost of stickiness; the user switches mode to exit.

### 9.6 Curtailed-surplus probe
**The problem.** A charger's native solar mode detects surplus by watching
*grid export*. But the planner disables feed-in whenever the slot's sell price
is under the §8.4 floor. So: sun is strong, the home battery is full and
cannot absorb, export is disabled — the inverter therefore **clips PV** to
match house load and grid power sits at ~0. The charger sees no export,
concludes there is no surplus, and idles. Free solar is thrown away.

Worse, the planner's own `pv_power_entity` is blind here too: a clipping
inverter reports *clipped* production, not *potential*, so `pv − load ≈ 0`.
The surplus is potential energy that **no sensor measures**. It cannot be
measured — only discovered, by pushing load and watching what the grid does.

**The asymmetric split.** The probe deliberately does *not* take over surplus
charging in general. The dividing question is "would this solar otherwise be
wasted?"

| Battery | Export this slot | Who charges the EV |
|---|---|---|
| Full | disabled (bad price) | **Planner probe** — surplus is curtailed and the charger is blind |
| Full | enabled (ok price) | **The charger's own mode** — surplus is exported and it follows it competently |
| Not full | either | Battery self-consumption absorbs it |

Export disabled means the energy is genuinely wasted, so grabbing it is pure
gain with no opportunity cost, and a blind regulator is appropriate. Export
enabled means the energy is *sellable*, so diverting it has an opportunity
cost — a value decision for the LP or the charger, not for a grid-following
regulator. The planner is uniquely able to make this call because it *set*
the export disable and knows the SoC. The two controllers never run at once.

**Arming.** All of the following must hold (a wider SoC margin applies while
already armed, so the probe's own brief overshoot-dip cannot self-disarm):

- `mode == auto`, and not inside the future-planned-start gate;
- `p_ev_chg_kw ≤ ε` — the LP is not already charging (if it is, it owns the
  charger);
- `p_sell_kw ≤ ε` — not exporting this slot, i.e. exactly the boundary above;
- battery effectively full: `soc ≥ soc_max − SOC_FULL_EPS_KWH`
  (`SOC_DISARM_EPS_KWH` while armed);
- `pv_forecast[0] − load[0] > PROBE_FORECAST_MARGIN_KW`, so the probe never
  kicks the charger awake on a dark clamp. This uses the **raw** slot-0
  forecast captured *before* the §8.2 `min(forecast, live_avg)` clamp — under
  curtailment the live PV reading is itself clipped to ≈ load, so the clamped
  value would zero the headroom and blind the probe exactly when it is needed.
  An over-optimistic forecast is harmless: the probe kicks to minimum current
  and the discharge check immediately backs it off;
- car not `DISCONNECTED`;
- both `battery_power_entity` and `grid_power_entity` readable.

**Why battery power is required.** A full battery still *discharges*. Command
more current than PV supports and the inverter covers the gap from the battery
first — grid import stays ~0 until the battery is drained. Grid import is
therefore a *late* indicator; battery discharge is the *leading* one. Watching
import alone would let the probe silently drain the home battery into the car,
a lossy round-trip we never want. Without the entity the probe stays disarmed
and the planner falls back to passive handback. The reading is normalised to a
discharge-positive magnitude (`max(0, −value)`) so a sign or unit mismatch
cannot silently read as "not discharging".

**Control law** (stateful, unlike the stateless reactive decision). Per cycle:

| Direction | Rule |
|---|---|
| **Down, immediate** | Battery discharge > `PROBE_DISCHARGE_HARD_W` *or* grid import > `PROBE_IMPORT_CEILING_W` → step down 1 A now. Below `min_a` → command 0 and stay armed, waiting for more sun. |
| **Down, sustained** | Battery discharge in the *soft band* (`PROBE_DISCHARGE_CEILING_W` … hard) → hold and count. Step down only once the drain has persisted `PROBE_OVERSHOOT_SUSTAIN_CYCLES` consecutive cycles. Any clean cycle resets the counter. |
| **Up** (speculative) | 1 A every `PROBE_UP_INTERVAL_CYCLES`, only while not overshooting, below `max_a`, and the next amp still fits the forecast headroom. |
| **Rest** | Otherwise hold and advance the up-counter. |

The feedback is one-sided — import is ~0 for *every* current at or below the
available surplus and only lifts once we exceed it — so overshoot gives a
clean signal while extra headroom can only be found speculatively. The bias is
to **undershoot**: resting one amp below the true surplus leaves <1 A of free
solar curtailed, which is the cheap error; importing is not.

**Why the down side is two-tier.** A single ceiling forces one number to serve
two jobs: rejecting noise and capping the loss. Set it low and the probe backs
off on any transient — a kettle, a passing cloud, or simply the settling tick
right after an up-step, before the inverter has unclipped PV to match. Set it
high and the probe will happily rest at a continuous drain, trickling the
house battery into the car at round-trip loss for as long as the sun holds.
Splitting them lets each threshold do one job: the soft band tolerates
transients *without* raising what we're willing to lose at rest, while the hard
ceiling still catches a genuine collapse (cloud kills PV, car keeps pulling kW)
on the very next tick. Worst case before correction is bounded by
`PROBE_DISCHARGE_HARD_W × PROBE_OVERSHOOT_SUSTAIN_CYCLES × cadence` ≈ 250 Wh at
the defaults. Note that grid import is **never** sustain-gated: unlike a
battery drain, it is money leaving immediately.

Limit-cycling on a quantized (integer-amp) actuator is held off by three
levers: the soft-band sustain requirement absorbs sensor noise and transients
before any down-step fires; while counting, the probe holds instead of stepping
up, so it never ratchets further into an overshoot it already suspects; and the
forecast-headroom gate suppresses pointless probing near the forecast
potential. With `PROBE_UP_INTERVAL_CYCLES = 1` the up-rate limiter is
effectively off — the ramp is deliberately aggressive, since the sustain
counter now absorbs the noise the rate limit used to. Note the phase wrinkle:
at ~0.69 kW/A three-phase the effective rest band is wider than single-phase's
~0.23 kW/A, so more free solar is left on the table.

**Disarming** happens as soon as any arm condition fails — export re-enabled,
battery no longer full, car gone — whereupon the planner writes the passive
option to hand back to the charger and resets probe state. No stickiness is
applied to the export-re-enable transition: prices are hourly, so a crossing
of the floor flips the charger at most about once an hour.

Tunables (initial values, to be validated empirically; they may later be
derived from `kw_per_amp`):

| Constant | Default |
|---|---|
| `SOC_FULL_EPS_KWH` | `0.2` kWh |
| `SOC_DISARM_EPS_KWH` | `0.5` kWh |
| `PROBE_FORECAST_MARGIN_KW` | `0.5` kW |
| `PROBE_DISCHARGE_CEILING_W` | `300` W (soft-band floor) |
| `PROBE_DISCHARGE_HARD_W` | `1500` W (immediate step-down) |
| `PROBE_IMPORT_CEILING_W` | `500` W (immediate step-down) |
| `PROBE_UP_INTERVAL_CYCLES` | `1` (step up every cycle) |
| `PROBE_OVERSHOOT_SUSTAIN_CYCLES` | `2` (~10 min at the default 300 s cadence) |

### 9.7 Write discipline and on-demand re-plan
Throughout §9, writes to the two *optional* outputs — the start switch and the
charger's native mode entity — are silently skipped when the user hasn't wired
them. Only the max-current `number` is guaranteed present, so each branch above
can be read as "write all three" with the unconfigured ones dropping out.

**Write order** per tick is always mode → current → start. Mode goes first so
that any mode-transition cache invalidation on the charger side lands before
the current write; otherwise the current would be written, immediately
invalidated, and re-fire needlessly next tick.

**Dead-bands differ per output, on purpose:**
- *Current* is suppressed when the new integer value is within
  `ev_current_tolerance_a` of the last written one — except the surplus probe,
  which forces the write, since its whole job is 1 A steps that would
  otherwise be swallowed by the tolerance.
- *Start switch* is written **every** tick with no suppression, so the planner
  self-corrects against firmware resets (some builds clear the charging switch
  on a passive→active transition) and against external user toggles. A switch
  service call is cheap; the logbook noise is accepted.

**On-demand re-plan.** The coordinator's fixed `update_interval` would mean
waiting up to a full cycle (default 5 min) after plugging in before anything
happens. So `__init__.py` registers a state-change listener on the EV inputs
that matter — charger state, target kWh, target %, deadline, planned start,
mode — and requests an immediate coordinator refresh. Charging engages within
a second or two of plug-in.

Two entities are excluded deliberately: **charging power**, because normal
wattage fluctuation would trigger a CPU-bound LP solve on every wobble, and
the **car auto-return switch**, which only changes what happens at session
end. `async_request_refresh()` is already debounced by HA, so a burst of
changes coalesces into one solve, and the listener is registered via
`entry.async_on_unload` so it detaches cleanly on reload.

## 10. Diagnostic Sensors
- `sensor.pv_optimizer_planned_grid_setpoint` (W, current slot)
- `sensor.pv_optimizer_planned_feed_in` (`on`/`off`)
- `sensor.pv_optimizer_expected_cost_horizon` (`your_currency`)
- `sensor.pv_optimizer_savings_vs_passive` (`your_currency`; cost of doing nothing − optimal)
- `sensor.pv_optimizer_plan` — state = next-slot set-point in kW.
  Attributes:
  - `slots`: per-slot list with ISO-tagged `start`, `duration_h`,
    `p_buy_kw`, `p_sell_kw`, `p_chg_kw`, `p_dis_kw`, `p_ev_chg_kw`,
    `soc_start_kwh`,
    `soc_physical_kwh`, `setpoint_w` (the §8.1 set-point the planner
    *would* write for that slot — slot 0 matches the realised
    `planned_grid_setpoint`; the rest is what the planner would write
    next if the LP plan holds, suitable as a chart series without
    re-implementing the predicates).
  - `capacity_kwh`, `soc_min_kwh`, `soc_max_kwh`, `soc_health_kwh`:
    battery params, exposed so frontend cards can compute SoC % and draw
    reserve / ceiling / health-floor lines without hardcoding.
  - `low_soc_penalty_per_kwh_h`: active dwell-penalty rate from §8.5
    (`your_currency`/(kWh·h); `0` = disabled).
  - `force_pv_export_enabled`: last-read state of the §8.2 toggle.
  - `min_sell_price_per_kwh`: active sell-price floor from §8.4
    (`your_currency`/kWh; `0` = disabled).
  - `horizon_slots`, `status`, `solve_time_s`, `error`: solver diagnostics.
- `sensor.pv_optimizer_load_forecast` (next-slot kW; full per-slot series in
  attributes). Only published when the built-in forecaster is active.
  Attributes include `ev_subtracted: bool` indicating whether the most
  recent cycle had the EV-history correction (§4.2) applied.

Five further sensors are published **only when the EV feature is configured**
(§9):
- `sensor.pv_optimizer_ev_status` — one of `disconnected`, `off`, `car_mode`,
  `charging_lp_planned`, `charging_cheap_grid`, `charging_surplus`, `idle`,
  evaluated in that precedence order. The mode-based checks come *before* the
  activity-based ones so a stale last-written current from a previous session
  cannot bleed through after a mode switch.
- `sensor.pv_optimizer_ev_session_energy` (kWh) — energy delivered since the
  last plug-in. Reads through the same accessor the LP uses (configured
  session-energy entity when present, internal integrator otherwise), so the
  sensor can never disagree with what the plan is working from. Forced to `0`
  while the car is disconnected: many charger firmwares only reset their
  session counter on plug-in, so between sessions the entity still reports the
  *previous* session's total — without the guard the planner would conclude a
  freshly scheduled target was already met and skip the session.
- `sensor.pv_optimizer_ev_remaining_kwh` — `max(0, target − session energy)`,
  i.e. what the LP is still trying to deliver.
- `sensor.pv_optimizer_ev_planned_current` (A) — the last value written to the
  charger's max-current entity.
- `sensor.pv_optimizer_ev_deficit_kwh` — the LP slack from §7. **Non-zero
  means the deadline is not achievable** with the configured charger power in
  the remaining window; this is the user-facing signal for that, since the LP
  degrades gracefully rather than failing.

## 11. Testing Strategy
- `optimizer.py` covered by deterministic unit tests with hand-designed
  scenarios (free PV, pure arbitrage, feed-in disabled, amortization, limits).
- `planner.py` covered end-to-end via `tests/test_planner.py` against a fake
  state reader / service caller — exercises every supported price shape
  (legacy list, wrapped dict, alternate wrapper auto-discovery, top-level
  ISO-keyed) plus the stale-data hard-fail and set-point dead-band; the
  active vs passive split (§8.1), force-PV-export branch (§8.2) including
  toggle-off / toggle-on / toggle-on-but-no-surplus and live-PV clamp
  below / above / no-history cases; the physical SoC projection
  (§8.3) for passive surplus, force-charge, and force-export slots; and
  the minimum-sell-price gate (§8.4) — default zero is a regression
  no-op, floor above all prices disables export horizon-wide, partial
  floor gates only the cheap slots within the horizon; the soft SoC
  health floor (§8.5) — default penalty zero is a regression no-op,
  cheap charge slot pulls SoC up to the floor, strong sell opportunity
  still allowed to dip below it.
- `load_forecaster.py` covered by `tests/test_load_forecaster.py` —
  median-based spike rejection, partial-history fallback, time-weighted
  bucket averaging, weekday filtering, result caching.
- `ev_controller.py` covered by `tests/test_ev_controller.py` — state
  classification (default vocabulary, the bare-`idle` and `low_soc` corner
  cases, precedence, custom overrides, unavailable inputs); the reactive
  decision (surplus math and the back-add convergence term, cheap-grid
  threshold, skip-below-min, clamp-at-max); session-done; slot-0 translation
  including the clamp-**up**-below-min asymmetry; the surplus-probe arm matrix
  (each condition gating independently, plus the wider disarm margin) and the
  regulator (kick to min, down-step on battery discharge *and* on grid import,
  below-min → 0, rest inside the deadband, up-step rate limiting, forecast
  headroom gating, max clamp).
- EV coverage in the shared suites: `tests/test_optimizer.py` asserts the
  LP schedules EV charge into cheap hours, respects the deadline cut-off,
  falls back to the soft slack when the deadline is unreachable, that the
  deficit penalty dominates a peak buy price, and that a zero target creates
  **no** EV variables (regression no-op). `tests/test_planner.py` covers the
  mode surface (`off` writes nothing, `car` writes max every tick, sticky vs
  auto-return incl. the charging-seen guard and the `low_soc` pause), the
  planned-start gate (and that `car` mode overrides it), the re-plan trigger
  entity set, the forced-write bypass of the current tolerance, and the probe
  wiring end-to-end — arms in the curtailment corner, arms despite clipped
  live PV, disarms back to passive on export, steps to zero on overshoot, and
  stays out of the planned-start gate.
- HA-side files (`coordinator.py`, `config_flow.py`, `sensor.py`) are thin
  shims over the pure layer and are exercised in a live HA instance, not in
  this repository's CI.
- No live HA instance required for the pure-layer suite; CI runs `pytest`.
