# EV surplus probe: minimum-current dwell and restart cooldown — design

Date: 2026-09-14
Status: Approved (pending spec review)

## Problem

The curtailed-surplus probe (see
`2026-06-23-ev-curtailed-surplus-probe-design.md`) chatters at minimum current.
Observed in the field: the car charges for roughly 15 minutes, stops, waits one
planner cycle, and starts again — indefinitely. That cycles the car's charge
connector far more than it should be cycled, and the dead cycle between
sessions throws away charging throughput.

### Mechanism

Two lines of `decide_surplus_probe` form the loop. At minimum current a
down-step has nowhere to go, so it becomes a stop:

```python
def _step_down() -> SurplusProbeDecision:
    new_a = current_a - 1
    if new_a < min_a:
        return SurplusProbeDecision(current_a=0, cycles_since_up=0)
```

and on the very next tick the probe — still armed, because SoC and forecast
have not changed — kicks straight back to minimum:

```python
if current_a < min_a:
    # Not charging yet — kick to min as the first probe.
    return SurplusProbeDecision(current_a=min_a, cycles_since_up=0)
```

With `PROBE_OVERSHOOT_SUSTAIN_CYCLES = 2` at the default 300 s cadence, the soft
discharge band tolerates drain for two cycles (~10 min) before stepping down,
which reproduces the reported ~15 min on / one cycle off exactly.

### Root cause

On a three-phase charger `min_charging_current_a = 6 A` is a **~4.1 kW
quantum** (`kw_per_amp ≈ 0.69`). Curtailed surplus is routinely smaller than
that, so at minimum current the instantaneous-drain test is permanently
unsatisfiable: 6 A against 3 kW of surplus draws ~1.1 kW from the battery,
comfortably above `PROBE_DISCHARGE_CEILING_W = 300`. The regulator reads this
as an overshoot every single time.

But it is not an overshoot. With the home battery **full** and PV **clipped**,
discharging the battery to feed the car opens headroom that the curtailed PV
immediately refills. Absorbing 3 kW of otherwise-wasted solar at the price of
1.1 kW of battery throughput is a good trade — it is the entire point of the
probe.

So the instrument is wrong, not the threshold. At minimum current the signal
that surplus has genuinely gone is not the drain *rate*; it is **SoC actually
falling and staying down**. Instantaneous drain cannot distinguish "feeding the
car from curtailed PV via the battery" from "draining the battery into the
car", because under curtailment both look identical for as long as the SoC
holds.

## Goal

At minimum current, keep charging while the curtailed surplus is substantially
covering it, stop when it genuinely is not, and bound how often the connector
can be cycled — without weakening the probe's protection against paying for
grid import or meaningfully draining the home battery.

## Approach

### Split the control law by where the current sits

Above minimum current, a down-step reduces load by one amp: cheap, reversible,
and correct to do immediately. At minimum current the only available move is to
stop, which is expensive (connector cycling, lost throughput) and should
therefore be governed differently. The existing law conflates the two.

The new branch structure for `decide_surplus_probe`:

```
1. Import escapes (only while current >= min; nothing to escape from at 0)
   import > probe.import_hard_w
       and current > min                               → step down one amp, now
       and current == min                              → stop, now
   import > PROBE_IMPORT_CEILING_W
       and current > min                               → step down one amp, now
                                                         (unchanged)
       and current == min                              → stop, but only once
                                                         import has persisted
                                                         PROBE_IMPORT_SUSTAIN_SECONDS

2. current < min — not charging; gated restart
   probe_off_seconds < probe.restart_cooldown_seconds  → stay at 0
   soc_deficit_kwh  > restart_eps                      → stay at 0 (not full again)
   otherwise                                           → kick to min

3. current > min — unchanged
   Two-tier discharge ceilings (hard immediate, soft after
   PROBE_OVERSHOOT_SUSTAIN_CYCLES), step down one amp.

4. current == min — no down-step exists, so the only move is off
   probe_on_seconds < probe.min_on_seconds             → hold at min
   soc_deficit_kwh  > probe.soc_drop_kwh               → stop
   otherwise                                           → hold at min, indefinitely
   (cycles_overshooting is emitted as 0 throughout this branch — it governs
   one-amp steps above minimum only, and must not carry a stale count into a
   later above-minimum state.)

5. Up-step — unchanged
   Rate-limited by PROBE_UP_INTERVAL_CYCLES, gated on forecast headroom, and
   suppressed while over the soft discharge ceiling.
```

Branch 4 is the substantive change. Three consequences worth stating plainly:

- **`PROBE_DISCHARGE_HARD_W` no longer stops charging instantly at minimum
  current.** That is deliberate — it is the behaviour being complained about.
  Its worst case is bounded instead by the minimum-on floor and the SoC budget
  (below).
- **There is no maximum-on cap.** If the surplus roughly covers the car, SoC
  stays near full, branch 4 falls through to "hold", and charging runs for
  hours uninterrupted. This is the throughput half of the fix; a fixed hold
  timer would have re-introduced the very duty-cycling being removed.
- **The escapes override the minimum-on floor.** Grid import is real money, so
  it is allowed to break the connector-protection floor. The restart cooldown
  bounds the resulting cycling to at most one start per cooldown window.

### The minimum-on floor and the SoC budget

Two independent gates, in that order:

- **`probe.min_on_seconds`** (default 600) is a floor on session length. It is
  the connector protection, and it is unconditional apart from the import
  escapes. Its worst case — PV vanishing the instant charging starts — costs
  `min_on_seconds × min_a × kw_per_amp` from the battery: ~0.68 kWh at 10 min
  and 4.1 kW.
- **`probe.soc_drop_kwh`** decides whether to keep going *after* the floor
  expires. It is an energy budget, not a timer, which is what makes the law
  self-pacing: with the surplus mostly covering the car the deficit accrues
  slowly and charging continues; with no sun at all the budget is spent in
  minutes and charging stops.

These two are coupled: if the floor can outspend the budget, the budget never
gets to decide anything and the floor silently becomes the whole law. Since
both are user-configurable, a pure predicate checks the relation at setup and
logs a warning (see "Validation" below) rather than failing or silently
clamping.

### Restart gating

A stop is followed by two conditions before charging may resume:

- **`probe.restart_cooldown_seconds`** (default 600) — a floor on off-time,
  bounding connector cycling from the other side.
- **Battery genuinely full again** — `soc_deficit_kwh <= restart_eps`, where

  ```
  restart_eps = min(SOC_FULL_EPS_KWH, probe.soc_drop_kwh / 2)
  ```

  i.e. normally the same epsilon the arm condition uses. This is stricter than
  the hold budget on purpose. If real curtailed surplus exists, a ~1 kWh deficit
  refills in roughly 20 minutes at 3 kW and the probe returns promptly; if
  there is no surplus, it never refills, which is exactly when the probe should
  stay off. The law is therefore self-limiting: a genuinely sunless spell costs
  one bounded drain event, not a repeating one.

The `min(...)` in `restart_eps` is load-bearing, not defensive. `soc_drop_kwh`
is user-configurable and derived from a percentage of battery capacity, so a
small percentage on a small battery can put it *below* the fixed 0.2 kWh arm
epsilon — 0.5% of 10 kWh is 0.05 kWh. A bare `SOC_FULL_EPS_KWH` restart gate
would then be looser than the stop gate: the probe would stop at a 0.05 kWh
deficit and immediately be cleared to restart at anything up to 0.2 kWh, which
is the original chatter with extra steps. Halving the budget guarantees the
ordering `restart_eps < soc_drop_kwh < soc_disarm_eps_kwh` for every
configurable value, and evaluates to exactly `SOC_FULL_EPS_KWH` for any budget
at or above 0.4 kWh — which is every realistic configuration. Deriving it this
way is preferred over validating-and-warning because the failure is a
correctness bug rather than a tuning preference.

### Interaction with disarm (must-fix, or the chatter relocates)

`SOC_DISARM_EPS_KWH` is currently a fixed 0.5 kWh. A hold with a ~1 kWh budget
would trip that disarm *first*: the probe hands back to the EVCS and resets its
state, SoC recovers, the probe re-arms and kicks to minimum — the same
oscillation by a different route.

Two changes prevent this:

- The disarm epsilon becomes **derived**, `soc_drop_kwh + 0.5`, exposed as a
  property on `SurplusProbeParams` and consumed by `should_probe_surplus` in
  place of the constant. Deriving rather than exposing it means the invariant
  `restart_eps < soc_drop_kwh < soc_disarm_eps_kwh` cannot be broken from the
  options form.
- `probe_stopped_at` **survives disarm**. The existing disarm block in
  `_run_surplus_probe` resets the probe fields; this one must be excluded from
  that reset, otherwise a disarm/re-arm round trip launders the cooldown.

### Time, not cycles

All three new dwells are measured in seconds, computed by the planner and
passed in — the same idiom as `is_session_done(low_power_seconds=...)`, keeping
`decide_surplus_probe` pure and clock-free.

Cycle counting would be the wrong clock here: `charger_state_entity` is a
re-plan trigger (`ev_replan_trigger_entities`, `planner.py:147`), so every
start/stop write changes the charger state and fires an extra off-cadence
planner tick. Cycle counts would run fast precisely around the transitions the
dwells exist to damp. The pre-existing `cycles_since_up` /
`cycles_overshooting` counters are left alone — they govern one-amp steps above
minimum current, where ticking fast is harmless.

## Module structure

### `models.py` — new `SurplusProbeParams`

A frozen dataclass for the four tunables, rather than extending `EVParams`: a
home-battery SoC budget is not a charger characteristic, and a separate
parameter object gives the probe a clean unit to test against.

| Field | Default | Role |
|---|---|---|
| `min_on_seconds` | 600.0 | floor on session length; escapes override |
| `restart_cooldown_seconds` | 600.0 | floor on off-time |
| `soc_drop_kwh` | `pct/100 × capacity` | ends a hold past the floor |
| `import_hard_w` | 2000.0 | immediate stop, overrides the floor |

Plus a derived property `soc_disarm_eps_kwh` → `soc_drop_kwh + 0.5`, and
`__post_init__` validation (all four `> 0`).

### `ev_controller.py`

- `decide_surplus_probe` gains `probe: SurplusProbeParams`, `probe_on_seconds`,
  `probe_off_seconds`, `import_over_seconds`, and `soc_deficit_kwh`, and takes
  the branch structure above.
- `should_probe_surplus` gains `probe` and uses `probe.soc_disarm_eps_kwh` in
  place of the module constant. `SOC_DISARM_EPS_KWH` is removed;
  `SOC_FULL_EPS_KWH` stays (arm condition, and the cap on `restart_eps`).
- `restart_eps` is computed inside `decide_surplus_probe` as
  `min(SOC_FULL_EPS_KWH, probe.soc_drop_kwh / 2)` rather than exposed as a
  property on `SurplusProbeParams`. The constant lives in `ev_controller.py`
  and `SurplusProbeParams` lives in `models.py`, which currently has no
  dependency on `ev_controller` — computing it at the point of use keeps that
  direction of dependency unchanged.
- New `PROBE_IMPORT_SUSTAIN_SECONDS` module constant (default 600, i.e. ~2
  cycles at the default cadence) — the soft import tier's dwell at minimum
  current. Not exposed on the form; the hard tier is the knob users need.
- New pure predicate `probe_floor_outspends_budget(ev, probe) -> bool`:
  `min_on_seconds × min_charging_current_a × kw_per_amp / 3600 > soc_drop_kwh`.

`soc_deficit_kwh` is passed as `max(0, soc_max_kwh − soc_kwh)`, normalised by
the caller in the same spirit as the existing
`battery_discharge_w = max(0, −battery_power)`.

### `planner.py`

Three new `EVRuntimeState` timestamps:

- `probe_started_at` — set on the 0 → ≥min transition; cleared on stop.
- `probe_stopped_at` — set when a decision commands 0; **excluded from the
  disarm reset**.
- `probe_import_over_since` — set on the first tick over
  `PROBE_IMPORT_CEILING_W`; cleared when import falls back below it.

All three are cleared when `state_class == DISCONNECTED`, so a freshly plugged
car starts promptly instead of inheriting a stale cooldown from the previous
session.

`_run_surplus_probe` takes `now` (already in hand in `_apply_ev`) and derives:

- `probe_on_seconds` — elapsed since `probe_started_at`, else `0.0`.
- `probe_off_seconds` — elapsed since `probe_stopped_at`, else `math.inf` (never
  stopped ⇒ no cooldown to serve).
- `import_over_seconds` — elapsed since `probe_import_over_since`, else `0.0`.
- `soc_deficit_kwh` — from the SoC reading it already takes.

The `0.0` fallbacks are the safe direction in both cases. After a config-entry
reload mid-charge, `probe_started_at` is `None` while the charger is still at
minimum current, so `probe_on_seconds` reads `0.0` and branch 4 grants a fresh
minimum-on floor — it delays a stop rather than causing one. Likewise a `None`
`probe_import_over_since` reads as "import has not been over the ceiling",
which is exactly what a cleared timestamp means. `probe_off_seconds` is the one
that must fall back to infinity rather than zero: a never-stopped probe has no
cooldown to serve, and `0.0` would block the very first kick to minimum for a
full cooldown window.

### `const.py` and `config_flow.py`

Four `CONF_*`/`DEFAULT_*` pairs, appended to `_EV_SCHEMA`:

| Key | Default | Selector |
|---|---|---|
| `ev_probe_min_on_seconds` | 600.0 | `_num(0.0, 3600.0, 30.0, "s")` |
| `ev_probe_restart_cooldown_seconds` | 600.0 | `_num(0.0, 3600.0, 30.0, "s")` |
| `ev_probe_soc_drop_pct` | 6.0 | `_num(0.5, 50.0, 0.5, "%")` |
| `ev_probe_import_hard_w` | 2000.0 | `_num(0.0, 20000.0, 100.0, "W")` |

The band is expressed in **percent**, matching every other SoC field on the
form (`battery_soc_min_pct`, `battery_soc_max_pct`, `battery_soc_health_pct`)
and converted to kWh against `battery.capacity_kwh` in `__init__.py`. On a
10–20 kWh battery the 6% default is ~0.6–1.2 kWh, landing the stop threshold at
roughly 94% of `soc_max` and the restart at ~98%. Both thresholds are offsets
below the configured `soc_max_kwh`, not absolute state-of-charge percentages.

Because `_OPTIONS_SCHEMA` extends `_EV_SCHEMA`, the fields appear on the
options screen with no extra wiring, and saving reloads the entry via the
existing update listener — no HA restart. The reload rebuilds `EVRuntimeState`,
so tuning clears the armed flag and the dwell timestamps; acceptable for a
deliberate change.

### `__init__.py`

Builds `SurplusProbeParams` (converting `%` → kWh), attaches it to `EVConfig`,
and logs a warning when `probe_floor_outspends_budget` is true.

## Validation

`probe_floor_outspends_budget` is a warning, not an error. The combination is
legal and merely means the floor dominates; refusing to start the integration
over a tuning choice would be disproportionate, and silently clamping would
hide it. A warning is the proportionate response for a knob the user is
expected to tune empirically.

## Data flow

Battery full, export disabled, car connected, broken cloud. Probe arms and
ramps to some current above minimum; a cloud arrives and the two-tier ceilings
walk the current down one amp at a time (branch 3, unchanged) until it reaches
6 A. At 6 A the surplus is ~3 kW against a 4.1 kW draw, so the battery supplies
~1.1 kW: branch 4 holds through the minimum-on floor, and because the curtailed
PV refills what the car takes, `soc_deficit_kwh` stays under the budget — so it
keeps holding, for hours if the weather stays that way. Charging is continuous
and ~3 kW of otherwise-curtailed solar reaches the car.

The sun then sets in earnest. Draw is now entirely from the battery, the
deficit crosses `soc_drop_kwh` within minutes of the floor expiring, and the
probe commands 0 and records `probe_stopped_at`. It does not restart: the
cooldown has to expire *and* SoC has to climb back within `restart_eps` of
`soc_max`, which without surplus never happens. One bounded drain event, then
quiet. Tomorrow morning the battery refills, the probe re-arms, and charging
resumes.

## Out of scope

- `decide_reactive` and its stateless skip-below-min. It is a different path
  (active only when no `charger_mode_entity` is configured) and is not the one
  producing the reported chatter.
- Sub-minimum-current operation — duty-cycling the charger to synthesise an
  effective current below `min_charging_current_a`. That is the other possible
  answer to the quantization problem and a much larger change.
- The arm conditions themselves (`should_probe_surplus`), apart from swapping
  the fixed disarm epsilon for the derived one.
- Deriving the discharge deadband from `kw_per_amp`, still deferred from the
  original probe spec.
- Any LP/optimizer change.

## Testing

**Pure logic (`tests/test_ev_controller.py`)** — the new branch structure:

- At minimum current, drain in the soft band, inside the floor → holds at
  minimum. *This is the regression test for the reported bug.*
- At minimum current, drain above `PROBE_DISCHARGE_HARD_W`, inside the floor →
  still holds. Documents the deliberate change from today's immediate stop.
- Past the floor, `soc_deficit_kwh` within budget → holds; repeated across
  several calls to assert there is no maximum-on cap.
- Past the floor, `soc_deficit_kwh` over budget → stops.
- At 0 with `probe_off_seconds` under the cooldown → stays at 0.
- At 0, cooldown elapsed but `soc_deficit_kwh > restart_eps` → stays at 0.
- At 0, cooldown elapsed and battery full → kicks to minimum.
- At 0 with no recorded stop (`probe_off_seconds` infinite) → kicks to minimum
  without serving a cooldown.
- `restart_eps` ordering: with a `soc_drop_kwh` below `2 × SOC_FULL_EPS_KWH`
  (e.g. 0.05 kWh), the deficit that triggers a stop must **not** also satisfy
  the restart gate. Parameterised over a range of budgets spanning the
  selector's bounds, asserting `restart_eps < soc_drop_kwh` throughout.
- Import above `import_hard_w` at minimum current, inside the floor → stops
  (escape overrides the floor).
- Import above `PROBE_IMPORT_CEILING_W` at minimum current: sustain not yet met
  → holds; sustain met → stops.
- Import above `PROBE_IMPORT_CEILING_W` above minimum current → steps down
  immediately, no sustain required (unchanged).
- `should_probe_surplus` stays armed at a deficit between `SOC_FULL_EPS_KWH`
  and the derived `soc_disarm_eps_kwh`, and disarms beyond it.
- `SurplusProbeParams` validation and the derived `soc_disarm_eps_kwh`.
- `probe_floor_outspends_budget` true/false cases.

The existing above-minimum two-tier tests must pass **untouched**. That is the
guard that this change is scoped to minimum current.

**Planner wiring (`tests/test_planner.py`)** — with `FakeReader`/`FakeCaller`:

- `probe_started_at` set on the 0 → minimum transition.
- The cooldown survives a disarm/re-arm round trip (the probe does not restart
  immediately after disarming and re-arming inside the cooldown window).
- All three timestamps cleared on `DISCONNECTED`.
- `import_over_seconds` accumulates across ticks while import stays high and
  resets when it falls back.

Existing planner tests for arming, disarm-to-Auto, and the planned-start gate
remain green unchanged.
