# PV LP Optimizer

> :warning: **Disclaimer:** The entire application is "vibe-coded" by "AI". Some
> parts may not be fully tested.

Home Assistant custom integration that minimises electricity cost for a
PV + battery + grid system using **linear programming**. Designed for
installations where the inverter/charger is controlled via a Victron Cerbo GX
already integrated with Home Assistant.

> Detailed design: see [PRD.md](PRD.md).

<p>
  <img src="/dashboards/pv_optimizer_plan.png" width="49%" alt="PV Optimizer plan" />
  <img src="/dashboards/pv_optimizer_prices_setpoint.png" width="49%" alt="PV Optimizer Prices & Setpoint" />
</p>


## What it does
At every update tick the integration:

1. Reads household load, PV power, battery SoC, hourly buy/sell tariffs, and
   PV/load forecasts from user-configured HA sensor entities.
2. Builds an LP over a configurable horizon (default 24 h, hourly slots).
3. Solves it with PuLP, preferring **HiGHS** (in-process via `highspy`) and
   falling back to the bundled CBC binary, minimising
   `Σ (price_buy·p_buy − price_sell·p_sell + cycle_cost·p_dis)·Δt`
   subject to power balance, battery dynamics, SoC bounds, charge/discharge
   limits, and grid import/export limits. Battery wear is included via a
   per-kWh **cycle (amortization) cost** booked on the discharge leg
   (LCOS convention: cost per kWh delivered out of the battery).
4. Writes the optimal current-slot grid set-point (W) and feed-in switch
   state to the configured Victron-backed `number`/`switch` entities.
   The set-point is only forced non-zero when the LP actively wants to move
   energy between battery and grid; in passive slots it stays at 0 and the
   inverter handles self-consumption itself (see *Active vs passive control*
   below).
5. Optionally drives an **EV wall-box** — either scheduled inside the same LP
   against a charge target and deadline, or reactively from PV surplus and
   cheap grid hours (see *EV charging* below). Entirely opt-in.

The optimizer is a pure Python module (`optimizer.py`) with no Home Assistant
imports, fully covered by unit tests.

## Repository layout
```
custom_components/pv_optimizer/
  models.py            # Pure dataclasses (BatteryParams, OptimizerInputs, ...)
  optimizer.py         # LP formulation + solve()
  planner.py           # Pure read→solve→apply pipeline (testable, no HA)
  load_forecaster.py   # Median-over-N-days load forecaster (testable, no HA)
  ev_controller.py     # Pure EV decision logic: state classification,
                       #   reactive charging, curtailed-surplus probe
  coordinator.py       # HA DataUpdateCoordinator shim around Planner
  config_flow.py       # Multi-step UI configuration
  sensor.py            # Diagnostic sensors
  select.py            # EV mode select        (auto / car / off)
  number.py            # EV target kWh / %
  datetime.py          # EV deadline / planned start
  switch.py            # EV car-mode auto-return
  const.py             # Configuration keys + defaults
  manifest.json        # HA manifest
tests/
  test_optimizer.py
  test_planner.py
  test_load_forecaster.py
  test_ev_controller.py
PRD.md
```

The four EV control platforms (`select`/`number`/`datetime`/`switch`) only
publish entities when the EV feature is configured.

## Installation
1. Copy `custom_components/pv_optimizer/` into your Home Assistant
   `config/custom_components/` directory (HACS-compatible repo layout).
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → "PV LP Optimizer"**.

## Charts
Useful charts are in the [dashboards](/dashboards) directory.

## Configuration
Five steps in the UI:

| Step | What you provide |
|---|---|
| Entities | Sensor IDs for load, PV, grid, SoC, buy/sell prices (today + optional tomorrow), PV forecast, optional load forecast, optional **battery power** (signed; needed by the EV surplus probe), optional feed-in override, optional **force-PV-export** toggle; plus the `number` for the Victron grid set-point and the `switch` controlling feed-in. |
| Battery  | Usable capacity (kWh), SoC min/max %, max charge/discharge power (kW), round-trip efficiencies, **cycle cost in your currency / kWh delivered out of the battery** — LCOS convention, booked on the discharge leg (≈ `battery_price / (cycles × usable_kWh × η_rt)`), optional **soft SoC health floor (%)** and **low-SoC dwell penalty (currency / kWh / h)** — see *Soft SoC health floor* below. |
| Solver   | Slot length (default 60 min), horizon (default 24 h), update interval (default 5 min), max grid import/export (kW), set-point write tolerance (W), **minimum sell price (currency/kWh; default 0)** — see *Minimum sell price* below. |
| Load forecast | Lookback days (default 7), optional cap (kW; 0 = no cap), weekday-aware mode (default off). Skipped at runtime when an external `load_forecast_entity` was set in the Entities step. |
| EV charging | All optional — leave blank to disable the feature entirely. Charger entity IDs (state, charging power, optional session energy, max-current `number`, optional start `switch`, optional native-mode entity + its *active* / *passive* option strings) and the static parameters (max charging power kW, max charging current A, min current A, cheap-grid price threshold, car battery kWh, current write tolerance, session-done power/duration). See *EV charging* below. |

Options flow re-exposes **every** knob — entities, battery, solver,
load-forecast and EV — in a single combined screen (pre-filled with current
values), so cycle cost, efficiencies, horizon, etc. can be tuned after install
without re-adding the integration. It is also how you switch the EV feature on
for an existing install.

> Currency note: the planner is currency-agnostic. All price-related fields
> (cycle cost in config, the cost / savings diagnostic sensors) carry the
> placeholder unit label `your_currency` — supply tariff sensors in whatever
> currency you use (EUR, USD, CZK, …) and the math comes out in that same
> unit. Just keep buy price, sell price, and cycle cost in **the same**
> currency.

### Plug-and-play integrations
Point the relevant entity fields at the sensors below — no template-sensor
glue required.

**Spot-price tariffs** — buy/sell price entities:

| Integration | Sensor to use | Notes |
|---|---|---|
| [`cz_energy_spot_prices`](https://github.com/rnovacek/homeassistant_cz_energy_spot_prices) (rnovacek, Czech OTE) | `sensor.current_buy_electricity_price` / `sensor.current_sell_electricity_price` (or the bare `current_spot_*` if no buy/sell template is configured) | 60-minute interval only. Today + tomorrow live on one sensor; leave the tomorrow fields blank. The 15-minute variant is **not** supported (the planner hour-truncates keys, collapsing the four sub-hour entries into one). |
| [`spot_hodinovy_tarif`](https://github.com/cermakcz/spot_hodinovy_tarif-ha) (Czech) | the integration's price sensor | Top-level ISO-keyed attributes; today + tomorrow on one sensor. |
| [Nordpool](https://github.com/custom-components/nordpool) | the price sensor (any country/area) | Legacy `today` / `tomorrow` list-of-24 shape — matches the planner's default `price_today_attr` / `price_tomorrow_attr` of `today` / `tomorrow`. Use the tomorrow field as well. |

**PV forecast** — multi-select PV forecast entity field:

| Integration | Sensor(s) to use | Notes |
|---|---|---|
| [Solcast PV Forecast (HACS)](https://github.com/BJReplay/ha-solcast-solar) | `sensor.solcast_pv_forecast_forecast_today` **and** `sensor.solcast_pv_forecast_forecast_tomorrow` | Read from `detailedHourly`; both sensors must be selected to cover the full ~48 h horizon. The planner accepts `period_start` as either a tz-aware `datetime` (Solcast's native shape) or an ISO string. |
| [Forecast.Solar](https://www.home-assistant.io/integrations/forecast_solar/) (HA core) | the integration's energy-forecast sensor (`sensor.energy_production_today` or similar) | Read from the `wh_hours` attribute (`{iso_hour: Wh}`). Today + tomorrow live on one sensor. |

Other integrations that publish one of the auto-detected shapes (see
*Expected attribute shapes* below) will also work — these are just the
ones explicitly covered by tests.

### Expected attribute shapes
- **Hourly tariff sensors** — five shapes are auto-detected, tried in order:
  - `list[float]` of length 24 under the configured attribute name (default
    `today` / `tomorrow`; Nordpool/OTE legacy shape);
  - `dict[str, float]` under the configured attribute name, keyed by ISO 8601
    timestamps with timezone offset, e.g. `{"2026-05-01T06:00:00+02:00": 0.12}`;
  - **dict under any other attribute name** — if the configured attribute
    isn't found, the planner scans every other dict-valued attribute and
    picks the largest one whose keys parse as ISO timestamps. This means
    user-built template sensors can publish under whatever attribute name
    feels natural (`prices`, `raw_today`, …) without any planner config;
  - **top-level ISO-keyed attributes** — the entity's `attributes` dict itself
    is the price map, with each hour timestamp as its own attribute key (this
    is what `spot_hodinovy_tarif` and `cz_energy_spot_prices` do — both are
    plug-and-play, point at `Current Buy/Sell Electricity Price` and leave
    the tomorrow entity blank). Unrelated metadata keys are ignored;
  - **scalar state** — the entity's `state` is a plain number (e.g.
    `0.15`). Every slot in the horizon is filled with that constant value.
    Useful for fixed-rate tariffs where the price never changes.

  With any dict shape today's and tomorrow's hours may live on a single
  entity (e.g. one 48-entry `prices` dict, or `cz_energy_spot_prices`'
  flat 48-key attribute set) or be split across today/tomorrow entities —
  both work.

  **Staleness contract (dict shapes only):** if the *current* hour's key is
  missing, the planner refuses to run and records `last_error` rather than
  silently re-using yesterday's prices. Missing *future* hours simply
  truncate the planning horizon.
- **PV forecast** accepts either a single sensor or a list of sensors (the UI
  exposes a multi-select; lists are merged per-hour, later entries winning).
  Each sensor must expose one of:
  - a `wh_hours` dict (`forecast.solar` style: `{"<iso-hour>": Wh}`),
  - a `forecast` list of `{"datetime": "<iso>", "power_kw": <kW>}` entries, or
  - a `detailedHourly` list of `{"period_start": "<iso>"|datetime,
    "pv_estimate": <kW>}` entries (Solcast HACS integration — point at both
    the today and tomorrow sensors to cover the full horizon natively).
- **Load forecast** is optional. When unset, a built-in forecaster derives a
  per-slot expected load from the recorder history of the configured load-power
  entity using the **median over the last `lookback_days` days at the same
  hour-of-day** (default `7`). Median naturally rejects one-off spikes (e.g.
  an EV charging session) without explicit detection. Optional knobs: `cap_kw`
  (hard ceiling) and `weekday_aware` (only days with the same weekday
  contribute; needs ~4 weeks of history to be useful). Setting an external
  `load_forecast_entity` disables the built-in forecaster (escape hatch).

## Active vs passive control
The planner only overrides the inverter when the LP wants to actively move
energy between the battery and the grid. Otherwise the set-point stays at
**0 W** and the Multiplus runs its own self-consumption logic (PV → load →
battery → grid surplus, gated by the feed-in switch).

| LP situation in slot 0 | Set-point written | Feed-in switch |
|---|---|---|
| Force-discharge (`p_dis > 0 ∧ p_sell > 0`) | `(p_buy − p_sell) · 1000` (negative) | `on` |
| Force-charge (`p_chg > 0 ∧ p_buy > 0`)     | `(p_buy − p_sell) · 1000` (positive) | `off` |
| Pure PV export, force-export toggle **on** (battery idle) | `(p_buy − p_sell) · 1000` (negative) | `on` |
| Force-hold-import (`p_buy > 0` with battery idle) | `(p_buy − p_sell) · 1000` (positive) | `off` |
| Anything else (PV deficit covered by battery, idle, pure export with toggle **off**) | `0` | `on` iff LP wants to sell |

Forcing a non-zero set-point speculatively is brittle: a PV undershoot would
make the inverter discharge the battery to defend a target it was never
asked to defend. Set-point = 0 hands those degrees of freedom back to the
inverter — but only when the LP and the inverter actually agree on what
"do nothing" means (see *Force-hold-import* below).

### Force-PV-export toggle
The default passive behaviour means morning PV ends up charging the battery
even when the LP would rather sell it (e.g. a high-price morning followed
by cheap noon recharge). Wire any `switch` / `input_boolean` / `binary_sensor`
to **Force PV export** in the options flow to opt into the third row above.

When the toggle is on the planner protects against forecast error by
clamping slot-0 PV to `min(forecast, trailing_avg)`, where the trailing
average is computed from the recorder over the configured update interval.
That way the LP can never speculate above measured production, and a cloud
that drops live PV below the forecast immediately reduces the export target
on the next planner cycle.

### Minimum sell price
A floor on the sell price (currency/kWh, default `0`) configurable in the
Solver options. Slots whose all-in sell price falls **strictly below** the
floor are treated as feed-in-disallowed for the purposes of the LP: the
optimizer pins `p_sell[t] = 0`, so it plans to keep that PV in the battery
for a better-priced slot (or curtails when the battery is full). When the
gate fires for slot 0 the feed-in switch is also turned off, which stops
the inverter's native logic from exporting at the floor price too. Useful
when the marginal sell revenue (e.g. 0.10 CZK/kWh) doesn't justify running
the inverter at full export power. Leave at `0` to disable the floor.

### Soft SoC health floor
The hard `SoC min` bound stops the LP at the inverter / chemistry safety
floor but says nothing about *dwelling* there. Without further input the
LP cheerfully drains to `SoC min` mid-afternoon and parks the battery at
the bottom for 18 hours when the next day's prices don't beat today's —
fine for revenue, bad for calendar aging (more so on NMC, mildly on LFP).

Two opt-in Battery knobs add a *soft* floor above `SoC min`:

| Knob | Default | Unit | Meaning |
|---|---|---|---|
| Soft SoC health floor | = `SoC min` (off) | % | SoC the LP is encouraged to stay above |
| Low-SoC dwell penalty | `0` (off) | currency / (kWh·h) | per-hour cost of each kWh below the floor |

The LP can still dip below the floor — that's the *soft* part — but only
when the slot's marginal economic gain (sell revenue, displaced buy)
beats the cumulative penalty over the rest of the horizon. A typical
configuration (`floor = 40 %`, `penalty = 0.5 CZK/(kWh·h)`) costs ~0.5
CZK/h per kWh of shortfall: enough to nudge the LP into recharging during
cheap hours, weak enough to yield to a real evening peak. Defaults make
the feature a regression no-op (no slack vars, no objective term).

### Force-hold-import
"Set-point = 0 means do nothing" only works when the LP and the inverter
agree on what "do nothing" is. They disagree in two situations where the
LP wants to cover the load purely from the grid with the battery idle:

- The soft health floor (above) makes further discharge expensive enough
  that grid import wins, but the inverter's native EMS doesn't know
  about that penalty and would drain the battery anyway.
- `SoC min` is set above the inverter's BMS floor (e.g. 20 % reserve on
  a battery the BMS allows down to 10 %); the LP respects `SoC min`,
  the EMS doesn't.

Both are silent plan violations: passive set-point `0` looks fine but the
battery quietly empties. The fourth row in the table above fixes it: when
the LP plans `p_buy > 0` with the battery idle, the planner pins the grid
set-point to that planned import (positive, typically tracking load).
Always-on, no toggle — when the predicate doesn't fire (e.g. battery
already at the hard floor with no arbitrage to defend) the forced
positive set-point produces the same physical behaviour as `0`.

## EV charging
Optional and brand-agnostic — the integration talks to your wall-box through
whatever HA entities it already exposes, with no charger-specific code. Fill in
the **EV charging** config step to enable it; leave it blank and nothing below
exists (no entities, no LP variables, no writes).

The feature turns on only when all six of these are set: charger-state entity,
charging-power entity, max-current `number`, max charging power (kW), max
charging current (A), and car battery capacity (kWh). The power/current pair is
the only place phases and voltage enter the picture — everything downstream
uses the derived ratio `kw_per_amp = max_power_kw / max_current_a`.

### Entities you get
| Entity | What it's for |
|---|---|
| `select.pv_optimizer_ev_mode` | `auto` / `car` / `off`. Default `auto`. |
| `number.pv_optimizer_ev_target_kwh` | How much to put in the car this session. |
| `number.pv_optimizer_ev_target_pct` | Same target as a % of the car's battery. Set either one — the planner uses whichever implies more kWh, so you never have to clear the other. |
| `datetime.pv_optimizer_ev_deadline` | "Have it done by." Required for LP-planned charging. |
| `datetime.pv_optimizer_ev_planned_start` | Optional "I'll be plugged in by." |
| `switch.pv_optimizer_ev_car_auto_return` | Opt-in auto-exit from `car` mode. Default off. |

All of them survive a restart. The target is **not** reset between sessions —
it stays until you change it.

### The three modes
**`auto`** — the useful one. If you've set a target **and** a future deadline
and the car is connected, EV charging becomes a variable in the LP and gets
scheduled into the cheapest hours before the deadline, co-optimized against
the house battery, PV and prices. Without a target (or once it's met, or once
the deadline has passed) it falls back to reactive charging: max current
whenever the buy price is at or below your cheap-grid threshold, otherwise
surplus-follow. Notably `auto` ignores the car's own "I want power" signal —
that request is what `car` mode is for.

**`car`** — "just charge it, now." Writes max current + start every tick and
ignores the plan. **Sticky**: it stays there until you switch back, which is
usually what you want at 11pm on a Sunday. Turn on the auto-return switch if
you'd rather have it drop back to `auto` when the session finishes.

**`off`** — the planner writes nothing to the charger at all. Sensors keep
updating, so you can watch without the integration touching anything.

### Planned start
Set a future **planned start** and the planner pretends the car will be
plugged in by then: it reserves the LP window from that time onwards, so you
can schedule a charging block before you're home. Until that time it drives no
charging at all — *not even from free PV surplus*. That's deliberate: if you
scheduled a later start you probably had a reason (cheaper or negative prices
tonight), and topping up from solar now could be the wrong trade. `car` mode
overrides the gate — explicit intent beats a schedule.

### Deadline you can't hit
The LP degrades gracefully rather than failing: if the charger physically
cannot deliver the target before the deadline, it charges as much as it can
and reports the shortfall in `sensor.pv_optimizer_ev_deficit_kwh`. Watch that
sensor — a non-zero value is the integration telling you the deadline was
never achievable.

### Charger state vocabulary
The charger's state string is matched case-insensitively against substrings,
in the precedence order disconnected → requesting → idle:

| Class | Matches |
|---|---|
| Disconnected | `disconnect`, `unplug` — plus `unknown` / `unavailable` / empty |
| Requesting | `charging`, `waiting_for_sun`, `waiting_for_start`, `waiting_for_rfid`, `waiting_for_time`, `wait sun`, `wait_sun`, `wait time`, `wait start`, `wait rfid` |
| Idle | `charged`, `connect`, `low_soc` |

Anything unrecognised is treated as *connected but idle*, the conservative
answer. Two deliberate choices: a bare `idle` is **not** treated as
disconnected (several firmwares spell connected-not-charging as
`charging_idle` / `connected_idle`), and `low_soc` — the charger pausing
because your *home* battery is low — counts as idle, so the charger's own
home-battery protection is respected.

### Native charger mode (recommended)
If you point **charger mode entity** at your wall-box's own mode `select`
(and set the option strings — defaults are EVCS's `Manual` / `Auto`), the
integration hands surplus tracking back to the charger whenever the charger
can see the surplus itself, and only takes the wheel when it needs to. That's
the better split: your charger's solar logic is usually good, and it runs far
more often than the 5-minute planner tick.

Without a mode entity the planner assumes the charger is permanently in
manual/active mode and does the surplus math itself:

```
surplus_kw = max(0, (−grid_w + ev_charging_w) / 1000)
current_a  = trunc(surplus_kw / kw_per_amp)     # 0 if below min current
```

The `+ ev_charging_w` term back-adds what the car is already drawing, without
which the loop would read its own consumption as "no surplus" and ramp itself
to zero. The result is truncated rather than rounded so the last fraction of
an amp never comes from the grid.

### Charging from curtailed solar
There's a corner where *nobody* can see the free energy. Sun is strong, the
house battery is full, and the sell price is under your **minimum sell price**
floor — so the planner has disabled export. The inverter therefore clips
production to match house load, grid power sits at zero, and your charger,
which detects surplus by watching grid *export*, concludes there's nothing
there and idles. Free solar gets thrown away.

The planner's own PV sensor is blind here too: a clipping inverter reports the
clipped figure, not the potential. So the surplus can't be measured — it has
to be **discovered**, by pushing load and watching what the grid does.

When the planner detects exactly that corner (battery full, export disabled,
car connected, forecast says surplus exists, LP not already charging) it takes
over the charger and runs a zero-import regulator: nudge the current up an amp
every tick, and give an amp back when it has clearly gone too far. It rests one
amp *below* the true surplus on purpose — leaving a fraction of an amp of free
solar unused is the cheap mistake; paying for grid is not.

Backing off is deliberately two-speed, because "the battery is discharging a
bit" and "the battery is emptying into the car" want different reactions:

| Signal | Reaction |
|---|---|
| Grid importing (> 500 W) | Step down immediately — that's money leaving now. |
| Battery draining hard (> 1500 W) | Step down immediately — PV has collapsed, probably a cloud. |
| Battery draining a little (300–1500 W) | Hold, and only step down if it's still happening two ticks later. |

The middle row is what keeps the probe from getting spooked: a kettle switching
on, a brief cloud, or simply the tick right after a step-up (before the inverter
has unclipped production to match) all look like a small drain for a moment.
Waiting one tick rides those out, while a genuine collapse still gets caught on
the very next tick by the hard threshold. Worst case the house battery gives up
roughly 250 Wh before the regulator corrects.

This needs the optional **battery power** entity (signed, negative =
discharging) wired up in the Entities step. Without it the probe stays off
entirely, because a full battery still discharges: watching grid import alone
would let the planner quietly drain your house battery into the car at
round-trip loss. Battery discharge is the early warning; grid import is the
late one.

As soon as the sell price recovers and export re-enables, the planner hands
the charger back — your charger can see the export again, so it's the better
controller from that point on.

### Response time
The planner normally runs on its fixed update interval (default 5 min), but
waiting that long after plugging in would be annoying. So it also watches the
charger state, target kWh/%, deadline, planned start and mode, and re-plans
within a second or two of any of them changing. Charging power is deliberately
**not** watched — normal wattage wobble would trigger an LP solve every few
seconds.

## Diagnostic sensors
Up to six sensors are created so the plan is visible in HA dashboards:

| Entity | Meaning |
|---|---|
| `sensor.pv_optimizer_planned_grid_setpoint` | Current-slot set-point in W (sign = Victron convention). |
| `sensor.pv_optimizer_planned_feed_in`       | `on`/`off` for the upcoming slot. |
| `sensor.pv_optimizer_expected_cost_horizon` | Total expected cost over the horizon (`your_currency`). |
| `sensor.pv_optimizer_savings_vs_passive`    | Savings vs. doing nothing (battery idle), in `your_currency`. |
| `sensor.pv_optimizer_plan`                  | State = next-slot set-point in kW; per-slot plan + battery params in attributes (see below). |
| `sensor.pv_optimizer_load_forecast`         | Built-in load forecaster's next-slot kW; full `kw_per_slot` and `days_used_per_slot` in attributes. Only created when the built-in forecaster is active (no `load_forecast_entity` configured). |

Five more appear when the EV feature is configured:

| Entity | Meaning |
|---|---|
| `sensor.pv_optimizer_ev_status` | `disconnected`, `off`, `car_mode`, `charging_lp_planned`, `charging_cheap_grid`, `charging_surplus`, or `idle` — what the integration thinks it's doing right now. |
| `sensor.pv_optimizer_ev_session_energy` | kWh delivered since plug-in — your session-energy entity if you wired one, otherwise the planner's own integration of charging power. Reads from the same place the LP does, so the two can't disagree. Reports `0` while the car is unplugged, since many chargers hold the last session's total until the next plug-in. |
| `sensor.pv_optimizer_ev_remaining_kwh` | Target minus what's been delivered — what the LP is still trying to schedule. |
| `sensor.pv_optimizer_ev_planned_current` | Last max-current value written to the charger (A). |
| `sensor.pv_optimizer_ev_deficit_kwh` | Energy the LP couldn't fit before the deadline. **Non-zero means the deadline isn't achievable** — the plan degrades instead of erroring, so this is where you find out. |

`sensor.pv_optimizer_plan` attributes:

| Attribute | Meaning |
|---|---|
| `slots` | Per-slot list with ISO-tagged `start`, `duration_h`, `p_buy_kw`, `p_sell_kw`, `p_chg_kw`, `p_dis_kw`, `p_ev_chg_kw`, `soc_start_kwh`, `soc_physical_kwh`, `setpoint_w`. `p_ev_chg_kw` is the LP's planned EV charging power (always `0` when the EV feature is off), so EV power charts as part of the same plan series. |
| `capacity_kwh`, `soc_min_kwh`, `soc_max_kwh`, `soc_health_kwh` | Battery params, exposed so dashboards can compute SoC % and draw reserve / ceiling / health-floor lines without hardcoding. |
| `low_soc_penalty_per_kwh_h` | Active dwell-penalty rate (currency/(kWh·h); `0` = disabled). |
| `force_pv_export_enabled` | Mirrors the toggle's last-read value (`null` if unset, `true`/`false` otherwise). |
| `min_sell_price_per_kwh` | Active sell-price floor (currency/kWh; `0` = disabled). |
| `horizon_slots`, `status`, `solve_time_s`, `error` | Solver diagnostics. |

Each slot carries two SoC tracks: `soc_start_kwh` is the LP's bookkeeping
(stays flat across passive PV-surplus slots because the LP curtails what it
doesn't actively transfer); `soc_physical_kwh` is a planner-layer projection
of what the inverter will actually reach, simulating self-consumption in
passive slots and following the LP exactly in active / force-export slots.
Plot both to see where the two diverge.

`setpoint_w` is the grid set-point the planner *would* write for that slot
under the rules in *Active vs passive control* (positive = import,
negative = export; `0` = passive / hand control to the EMS). For the
current slot it matches `sensor.pv_optimizer_planned_grid_setpoint`; the
rest of the horizon is what the planner would write next if the LP plan
holds. Use it directly as the chart series instead of re-implementing the
predicates client-side.

## Development & tests
```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
```

The test suite runs in a plain virtualenv, **without** installing Home
Assistant. It covers:
- the LP formulation in `tests/test_optimizer.py`
- the read→solve→apply planner pipeline in `tests/test_planner.py`,
  including legacy list, timestamp-keyed dict, alternate wrapper-attribute
  auto-discovery, and top-level ISO-keyed price formats, plus the
  stale-data hard-failure path.
- the median-over-N-days load forecaster in `tests/test_load_forecaster.py`,
  including spike rejection, partial-history fallback, time-weighted bucket
  averaging, weekday filtering, and result caching.
- the pure EV decision logic in `tests/test_ev_controller.py` — charger-state
  classification (including the `idle` and `low_soc` corner cases and custom
  vocabularies), reactive surplus tracking, session-done detection, LP slot-0
  translation, and the curtailed-surplus probe's arm matrix and regulator.
  EV behaviour that spans layers lives in the other two suites: LP scheduling,
  deadline cut-off and the soft deficit in `test_optimizer.py`; the mode
  surface, planned-start gate, re-plan triggers and probe wiring in
  `test_planner.py`.

The HA-side files (`coordinator.py`, `config_flow.py`, `sensor.py`, and the
EV control platforms `select.py` / `number.py` / `datetime.py` / `switch.py`)
are deliberately thin shims over the pure layer; they are exercised inside a
running HA instance, not in this repository's CI.

## License
MIT.
