"""Pure planning layer: reads from a state source, runs the optimizer, applies setpoints.

This module has no Home Assistant dependency. The HA coordinator (separate
module) is a thin shim that supplies ``StateReader``/``ServiceCaller``
implementations backed by ``hass``.
"""
from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta, timezone
from typing import Any, Protocol, Sequence

import pulp

from .const import BAD_STATES as _BAD_STATES
from .ev_controller import (
    DEFAULT_STATE_VOCAB,
    EVStateClass,
    classify_state,
    decide_reactive,
    is_session_done,
    translate_lp_slot0,
    should_probe_surplus,
    decide_surplus_probe,
)
from .load_forecaster import LoadForecaster
from .models import (
    BatteryParams,
    EVParams,
    OptimizerError,
    OptimizerInputs,
    OptimizerResult,
    SlotPlan,
    TariffSlot,
)
from .optimizer import solve

_LOGGER = logging.getLogger(__name__)

# Threshold (kW) above which an LP variable is treated as a meaningful
# command rather than solver noise. Shared between force-charge / force-
# discharge detection and the passive-projection helper so both agree on
# which slots are "active" vs "passive".
_FORCE_EPS = 1e-3


@dataclass
class EVRuntimeState:
    """Per-planner mutable EV state — cheap_grid flag + dwell timers."""

    cheap_grid_active: bool = False
    last_state_class: "EVStateClass" = None
    low_power_since: datetime | None = None
    last_written_current_a: int | None = None
    # Tracks the previously written role ("active" / "passive") on the
    # charger_mode select. Not used for dedupe — the mode write itself is
    # unconditional so the planner self-corrects against firmware resets or
    # external toggles — but used to detect *transitions*, which invalidate
    # ``last_written_current_a`` because some charger firmwares reset their
    # internal current register on a mode flip.
    last_written_charger_mode: str | None = None  # "active" or "passive"
    last_mode: str | None = None
    # Tracks whether the car has drawn meaningful power (>= session_done_power_w)
    # at any point during the current 'car' mode session (auto-return path only).
    # Without this, the auto-return would fire on the IDLE+low-power dwell
    # whenever the EVCS is gating (e.g. low_soc, waiting_for_*) — defeating
    # the point of car mode. Reset whenever we leave car mode or the car disconnects.
    car_session_charging_seen: bool = False
    last_session_plug_in: datetime | None = None
    session_energy_kwh: float = 0.0
    last_charging_power_kw: float | None = None
    last_tick: datetime | None = None
    # Curtailed-surplus probe state.
    probe_armed: bool = False
    probe_current_a: int = 0
    probe_cycles_since_up: int = 0
    probe_cycles_overshooting: int = 0


@dataclass(frozen=True)
class StateView:
    """Minimal state shape (mirrors HA's ``State`` for our purposes)."""

    state: str | None
    attributes: dict[str, Any] = field(default_factory=dict)


class StateReader(Protocol):
    def get(self, entity_id: str) -> StateView | None: ...


class ServiceCaller(Protocol):
    def call(self, domain: str, service: str, data: dict[str, Any]) -> None: ...


class LiveAverager(Protocol):
    """Computes a time-weighted-average of a numeric power entity.

    The planner uses this to refine slot-0 PV against live measurements,
    so the active force-export branch responds to clouds within one
    planner cycle instead of waiting for the upstream PV forecaster
    (typically hourly) to refresh.
    """

    def average_kw(self, entity_id: str, since: datetime, until: datetime
                   ) -> float | None: ...


@dataclass(frozen=True)
class EVConfig:
    """All EV-related entity bindings + static params bundled together.

    A single ``EVConfig`` field on ``PlannerConfig`` keeps the EV surface
    optional: when ``cfg.ev is None`` the planner does no EV work at all.
    """

    # Static parameters.
    params: EVParams
    # Input entities (read by the planner).
    charger_state_entity: str
    charging_power_entity: str  # W by default; only treated as kW when unit_of_measurement == "kW"
    max_current_entity: str     # number entity (A) — output
    session_energy_entity: str | None = None
    start_switch_entity: str | None = None
    charger_mode_entity: str | None = None
    # Option strings driven on charger_mode_entity. Vendor-specific:
    # EVCS uses "Manual"/"Auto", Zappi uses "Eco+"/"Stopped", etc.
    # "Active" = planner controls current; "Passive" = charger decides.
    charger_mode_option_active: str = "Manual"
    charger_mode_option_passive: str = "Auto"
    # Integration-created entity ids (the planner reads these to learn
    # the user's mode/target/deadline; HA layer owns their write side).
    mode_entity: str = ""           # select.pv_optimizer_ev_mode
    target_kwh_entity: str = ""     # number.pv_optimizer_ev_target_kwh
    target_pct_entity: str = ""     # number.pv_optimizer_ev_target_pct
    deadline_entity: str = ""       # datetime.pv_optimizer_ev_deadline
    planned_start_entity: str = ""  # datetime.pv_optimizer_ev_planned_start
    # Opt-in switch: when ON, 'car' mode auto-returns to 'auto' on
    # session-done (disconnect, or idle+low-power dwell after the car
    # has actually drawn power at least once). When OFF (default), 'car'
    # mode is sticky — stays until the user switches it back.
    car_auto_return_entity: str = ""


def ev_replan_trigger_entities(ev: EVConfig) -> list[str]:
    """Entity ids whose state changes should trigger an immediate re-plan.

    Watches the charger *connection state* and the user-facing EV inputs
    (target kWh/%, deadline, planned start, mode). The charging-*power*
    entity is intentionally excluded: its wattage fluctuates throughout a
    charging session and would trigger needless (CPU-bound) LP solves.
    ``car_auto_return_entity`` is also intentionally excluded: toggling it
    only affects the non-time-critical session-done auto-return path, which
    is adequately handled by the periodic planning cycle.

    Empty ids are filtered out so a never-configured optional input never
    subscribes to ``""``. Order is stable for deterministic tests.
    """
    candidates = [
        ev.charger_state_entity,
        ev.target_kwh_entity,
        ev.target_pct_entity,
        ev.deadline_entity,
        ev.planned_start_entity,
        ev.mode_entity,
    ]
    return [e for e in candidates if e]


@dataclass(frozen=True)
class PlannerConfig:
    # Inputs (entity IDs).
    load_power_entity: str
    pv_power_entity: str
    grid_power_entity: str
    battery_soc_entity: str
    buy_price_today_entity: str
    sell_price_today_entity: str
    # Either a single entity ID or a list of them. Multiple entities are
    # read in order and their per-hour forecasts merged (later entries
    # override earlier ones for the same hour); this lets users point at
    # e.g. the Solcast HACS today + tomorrow sensors directly without a
    # template-sensor merge step.
    pv_forecast_entity: str | Sequence[str]
    buy_price_tomorrow_entity: str | None = None
    sell_price_tomorrow_entity: str | None = None
    load_forecast_entity: str | None = None
    feedin_override_entity: str | None = None
    force_pv_export_entity: str | None = None
    battery_power_entity: str | None = None
    # Outputs (entity IDs).
    grid_setpoint_entity: str = ""
    feedin_switch_entity: str = ""
    # Battery / solver parameters.
    battery: BatteryParams = field(default_factory=lambda: BatteryParams(
        capacity_kwh=10.0, soc_min_kwh=1.0, soc_max_kwh=9.0,
        p_chg_max_kw=5.0, p_dis_max_kw=5.0,
    ))
    p_grid_imp_max_kw: float = 25.0
    p_grid_exp_max_kw: float = 25.0
    slot_minutes: int = 60
    horizon_hours: int = 24
    setpoint_tolerance_w: float = 50.0
    # Per-slot floor on sell price. Slots with ``price_sell`` strictly below
    # this value have their ``feedin_allowed`` flag forced off, which the
    # optimizer enforces as ``p_sell[t] = 0``. Useful when the marginal sell
    # revenue (e.g. 0.1 CZK/kWh) doesn't justify exporting at all. 0.0
    # disables the floor entirely (any non-negative sell price is acceptable).
    min_sell_price_per_kwh: float = 0.0
    # Cycle cadence; doubles as the trailing window for the slot-0 PV
    # smoothing helper so the active force-export branch reacts to clouds
    # at the same granularity it replans.
    update_seconds: int = 300
    # Attribute keys used to read hourly price arrays (Nordpool convention).
    price_today_attr: str = "today"
    price_tomorrow_attr: str = "tomorrow"
    ev: EVConfig | None = None


@dataclass
class PlanCycle:
    """One planner step result, suitable for diagnostics / sensors."""

    now: datetime
    result: OptimizerResult | None
    applied_setpoint_w: float | None
    applied_feedin: bool | None
    force_pv_export_enabled: bool | None = None
    error: str | None = None


class Planner:
    """End-to-end: read state, solve, apply setpoint + feed-in switch."""

    def __init__(self, config: PlannerConfig, reader: StateReader, caller: ServiceCaller,
                 load_forecaster: LoadForecaster | None = None,
                 live_averager: LiveAverager | None = None) -> None:
        self.config = config
        self.reader = reader
        self.caller = caller
        self.load_forecaster = load_forecaster
        # Optional. When supplied, slot-0 PV is refined via
        # ``min(forecast, trailing_avg)`` so the active force-export branch
        # can't speculate above measured production.
        self.live_averager = live_averager
        self.last: PlanCycle | None = None
        self.ev_state: EVRuntimeState | None = (
            EVRuntimeState() if config.ev is not None else None
        )
        self._cached_first_buy_price: float = 0.0
        self._cached_ev_remaining_kwh: float = 0.0
        self._cached_first_pv_kw: float = 0.0
        self._cached_first_load_kw: float = 0.0

    # ---- public API ------------------------------------------------------
    def step(self, now: datetime) -> PlanCycle:
        # Per-phase timing breadcrumbs at DEBUG so a slow cycle can be
        # localised to inputs/solve/projection without external profiling.
        t0 = _time.perf_counter()
        try:
            inputs = self._build_inputs(now)
            t1 = _time.perf_counter()
            result = solve(inputs)
            t2 = _time.perf_counter()
        except (OptimizerError, ValueError, KeyError, pulp.PulpError, OSError) as exc:
            _LOGGER.warning("Planning step failed: %s", exc)
            cycle = PlanCycle(now=now, result=None, applied_setpoint_w=None,
                              applied_feedin=None, error=str(exc))
            self.last = cycle
            return cycle

        # Read the force-export toggle once so the projection and the
        # control branch agree on which slots are LP-driven and which fall
        # back to inverter self-consumption.
        force_pv_export_enabled = self._read_bool_optional(
            self.config.force_pv_export_entity, default=False)

        # Augment the LP slots with a physical SoC projection. The LP
        # bookkeeping curtails surplus PV in passive slots (so soc_start_kwh
        # may stay flat across midday); the projection re-runs the inverter's
        # self-consumption rule to estimate the SoC the battery will actually
        # reach. Useful for chart overlays, not used for control.
        result = replace(result,
                         slots=_attach_physical_soc(
                             inputs, result.slots, _FORCE_EPS,
                             force_pv_export_enabled=force_pv_export_enabled))
        if self.config.ev is not None:
            result.extras["ev_remaining_kwh"] = self._cached_ev_remaining_kwh

        # Translate the LP plan for the next slot into inverter actions.
        # ``setpoint_w`` is precomputed per slot by ``_attach_physical_soc``
        # under the §8.1 active-vs-passive rules (see the rationale there)
        # so the control path and the dashboard projection share a single
        # source of truth for the predicates. The feed-in switch follows
        # the simpler "LP wants to sell in slot 0" predicate.
        first = result.slots[0]
        setpoint_w = first.setpoint_w if first.setpoint_w is not None else 0.0
        feedin = first.p_sell_kw > _FORCE_EPS

        prev_sp = self.last.applied_setpoint_w if self.last else None
        if prev_sp is None or abs(setpoint_w - prev_sp) > self.config.setpoint_tolerance_w:
            self._apply_setpoint(setpoint_w)
        else:
            setpoint_w = prev_sp  # unchanged
        prev_fi = self.last.applied_feedin if self.last else None
        if prev_fi is None or feedin != prev_fi:
            self._apply_feedin(feedin)

        # EV control. Always last so it can read the latest first-slot price
        # and the LP's p_ev_chg_kw decision.
        self._apply_ev(now, result.slots[0])

        cycle = PlanCycle(now=now, result=result, applied_setpoint_w=setpoint_w,
                          applied_feedin=feedin,
                          force_pv_export_enabled=force_pv_export_enabled,
                          error=None)
        self.last = cycle
        t3 = _time.perf_counter()
        _LOGGER.debug(
            "planner.step: build_inputs=%.2fs solve=%.2fs post=%.2fs total=%.2fs",
            t1 - t0, t2 - t1, t3 - t2, t3 - t0,
        )
        return cycle

    # ---- input assembly --------------------------------------------------
    def _build_inputs(self, now: datetime) -> OptimizerInputs:
        cfg = self.config
        ti0 = _time.perf_counter()
        soc_pct = self._read_float(cfg.battery_soc_entity)
        load_kw_now = self._read_float(cfg.load_power_entity) / 1000.0

        slot_h = cfg.slot_minutes / 60.0
        n_slots = max(1, int(round(cfg.horizon_hours / slot_h)))
        first_start = _floor_to_slot(now, cfg.slot_minutes)
        candidate_starts = [first_start + timedelta(minutes=cfg.slot_minutes * i) for i in range(n_slots)]

        # Build {hour-key -> price} maps. Both list (legacy Nordpool-style) and
        # timestamp-keyed dict shapes are accepted; a missing future hour
        # truncates the horizon, while a missing current hour is a hard error
        # (treats stale-data scenarios explicitly).
        buy_map = self._build_price_map(
            cfg.buy_price_today_entity, cfg.price_today_attr,
            cfg.buy_price_tomorrow_entity, cfg.price_tomorrow_attr,
            first_start.date(),
        )
        sell_map = self._build_price_map(
            cfg.sell_price_today_entity, cfg.price_today_attr,
            cfg.sell_price_tomorrow_entity, cfg.price_tomorrow_attr,
            first_start.date(),
        )
        ti1 = _time.perf_counter()
        feedin_global = self._read_bool_optional(cfg.feedin_override_entity, default=True)

        slots: list[TariffSlot] = []
        slot_starts: list[datetime] = []
        for start in candidate_starts:
            hour_key = start.replace(minute=0, second=0, microsecond=0)
            pb = buy_map.get(hour_key)
            ps = sell_map.get(hour_key)
            if pb is None or ps is None:
                if not slots:
                    raise ValueError(
                        f"price data for current slot {hour_key.isoformat()} is missing "
                        "(stale tariff sensor?)"
                    )
                break  # truncate horizon at first missing future hour
            # Per-slot sell-price floor. Sub-threshold slots get
            # ``feedin_allowed=False`` so the optimizer pins ``p_sell=0`` and
            # the LP plans around the lost revenue (e.g. keeps PV in the
            # battery for a higher-price slot later in the day).
            feedin_for_slot = feedin_global and (ps >= cfg.min_sell_price_per_kwh)
            slots.append(TariffSlot(
                start=start, duration_h=slot_h,
                price_buy=pb, price_sell=ps,
                feedin_allowed=feedin_for_slot,
            ))
            slot_starts.append(start)

        pv_kw = self._read_pv_forecast(cfg.pv_forecast_entity, slot_starts, slot_h)
        # Cache the raw (pre-clamp) slot-0 forecast for the surplus probe. The
        # probe's arm condition must use the forecast before any live-average
        # clamp: under curtailment (battery full, export disabled) the inverter
        # clips measured PV to ~load, so the live average ≈ load and
        # min(forecast, live) would drive forecast_surplus to ~0 — exactly when
        # the probe should fire. The LP still uses the clamped pv_kw[0].
        self._cached_first_pv_kw = pv_kw[0] if pv_kw else 0.0
        ti2 = _time.perf_counter()
        # Refine slot-0 PV against a measured trailing average so the active
        # force-export branch responds to clouds within one planner cycle.
        # ``min(forecast, live)`` is asymmetric on purpose: we only cut the
        # plan when reality undershoots the forecast — never speculate above
        # the upstream forecaster's view of the slot average.
        if pv_kw and self.live_averager is not None and cfg.pv_power_entity:
            window = max(timedelta(seconds=cfg.update_seconds), timedelta(seconds=30))
            live = self.live_averager.average_kw(
                cfg.pv_power_entity, now - window, now)
            if live is not None:
                pv_kw[0] = min(pv_kw[0], max(0.0, live))
        ti3 = _time.perf_counter()
        load_kw = self._read_load_forecast(cfg.load_forecast_entity, slot_starts, slot_h, load_kw_now)
        ti4 = _time.perf_counter()
        soc_kwh = max(cfg.battery.soc_min_kwh,
                      min(cfg.battery.soc_max_kwh,
                          soc_pct / 100.0 * cfg.battery.capacity_kwh))
        _LOGGER.debug(
            "planner.build_inputs: prices=%.2fs pv_forecast=%.2fs live_pv=%.2fs "
            "load_forecast=%.2fs",
            ti1 - ti0, ti2 - ti1, ti3 - ti2, ti4 - ti3,
        )

        self._cached_first_buy_price = slots[0].price_buy
        self._cached_first_load_kw = load_kw[0] if load_kw else 0.0

        # EV inputs (only when an EV config exists, the user has set a
        # positive target, and a deadline lies inside the planning horizon).
        ev_params = None
        ev_target = 0.0
        ev_deadline_idx = None
        ev_start_idx = 0
        if cfg.ev is not None:
            target_kwh = self._read_float_optional(
                cfg.ev.target_kwh_entity, default=0.0)
            # Convenience input: a % entity lets users who don't know their
            # car's kWh capacity off-hand express a target. We take the
            # larger of the two so neither field has to be cleared when the
            # other is set.
            target_pct = self._read_float_optional(
                cfg.ev.target_pct_entity, default=0.0)
            if target_pct > 0:
                target_kwh = max(
                    target_kwh,
                    (target_pct / 100.0) * cfg.ev.params.car_battery_kwh,
                )
            deadline = self._read_datetime_optional(
                cfg.ev.deadline_entity)
            # ``planned_start`` (when set and in the future) means: pretend
            # the car will be plugged in by then, regardless of current
            # physical state, and reserve the LP window from that slot on.
            # This lets the user pre-schedule a charging block before
            # arriving home. ``_apply_ev`` keeps a parallel gate: it drives no
            # charging before the time (not even from free PV surplus) and just
            # parks the EVCS idle in passive "Auto" mode until the time rolls
            # in.
            planned_start = self._read_datetime_optional(
                cfg.ev.planned_start_entity)
            if planned_start is not None and planned_start > now:
                connected = True
            else:
                connected = (
                    classify_state(self._read_text(cfg.ev.charger_state_entity))
                    != EVStateClass.DISCONNECTED
                )
            session_done = self.session_energy_kwh()
            remaining = max(0.0, target_kwh - session_done)
            self._cached_ev_remaining_kwh = remaining
            if (connected and remaining > 0 and deadline is not None
                    and deadline > now):
                # Slot index nearest to (but not past) the deadline.
                deadline_floor = _floor_to_slot(deadline, cfg.slot_minutes)
                if deadline_floor > slot_starts[-1]:
                    ev_deadline_idx = len(slot_starts)
                else:
                    # Inclusive of deadline_floor since charging in that hour is allowed.
                    ev_deadline_idx = next(
                        (i for i, s in enumerate(slot_starts) if s >= deadline_floor),
                        len(slot_starts),
                    )
                if planned_start is not None and planned_start > now:
                    start_floor = _floor_to_slot(planned_start, cfg.slot_minutes)
                    ev_start_idx = next(
                        (i for i, s in enumerate(slot_starts) if s >= start_floor),
                        len(slot_starts),
                    )
                if ev_start_idx < ev_deadline_idx:
                    ev_params = cfg.ev.params
                    ev_target = remaining

        return OptimizerInputs(
            slots=slots, pv_kw=pv_kw, load_kw=load_kw,
            initial_soc_kwh=soc_kwh, battery=cfg.battery,
            p_grid_imp_max_kw=cfg.p_grid_imp_max_kw,
            p_grid_exp_max_kw=cfg.p_grid_exp_max_kw,
            ev=ev_params,
            ev_target_kwh=ev_target,
            ev_deadline_index=ev_deadline_idx,
            ev_start_index=ev_start_idx,
        )

    # ---- price source readers -------------------------------------------
    def _build_price_map(self, entity_today: str, attr_today: str,
                         entity_tomorrow: str | None, attr_tomorrow: str,
                         today_date) -> dict[datetime, float]:
        """Merge today's (and optionally tomorrow's) price entity into one map.

        Keys are naive UTC datetimes truncated to the hour. Tomorrow's entries
        override today's on overlap (rare, but explicit).
        """
        out: dict[datetime, float] = {}
        out.update(self._read_price_source(entity_today, attr_today, today_date))
        if entity_tomorrow:
            try:
                out.update(self._read_price_source(
                    entity_tomorrow, attr_tomorrow, today_date + timedelta(days=1)))
            except (KeyError, ValueError):
                # Tomorrow not yet published — that's fine, planner truncates.
                pass
        return out

    def _read_price_source(self, entity_id: str, attr: str,
                           default_date) -> dict[datetime, float]:
        """Read one price entity.

        Five shapes are accepted, tried in order:
        * ``attributes[attr]`` is a ``dict[iso_timestamp, float]`` (wrapped),
        * ``attributes[attr]`` is a ``list[float]`` of 24 hourly prices,
        * any *other* dict-valued attribute looks like an ISO-keyed price map
          (auto-discovery — handles templates that publish under names like
          ``prices`` / ``raw_today`` without requiring user config),
        * the entity exposes hour timestamps as *top-level* attribute keys
          (``attributes`` itself is the price map). This is what plugins like
          ``spot_hodinovy_tarif`` do — there is no wrapping attribute.
        * the entity's ``state`` is a plain number — used as a flat price for
          every slot in the horizon (useful for fixed-rate tariffs).
        """
        st = self._state(entity_id)
        raw = st.attributes.get(attr)
        if isinstance(raw, dict) and raw:
            return self._parse_iso_keyed_dict(raw, lenient=False)
        if isinstance(raw, (list, tuple)) and raw:
            return {datetime.combine(default_date, time(hour=i)): float(v)
                    for i, v in enumerate(raw)}
        # Auto-discovery: scan other dict-valued attributes for one whose keys
        # parse as ISO timestamps. Picks the largest such map, so a stray small
        # dict elsewhere in attributes doesn't shadow the real price map.
        best: dict[datetime, float] = {}
        for k, v in st.attributes.items():
            if k == attr or not isinstance(v, dict) or not v:
                continue
            parsed = self._parse_iso_keyed_dict(v, lenient=True)
            if len(parsed) > len(best):
                best = parsed
        if best:
            return best
        # Last resort: hour timestamps are themselves the attribute keys.
        scanned = self._parse_iso_keyed_dict(st.attributes, lenient=True)
        if scanned:
            return scanned
        # Scalar fallback: entity state is a plain number — fill every hour
        # in a 48 h window (today + tomorrow) with the same flat price.
        if st.state not in _BAD_STATES:
            try:
                flat = float(st.state)
                return {datetime.combine(default_date + timedelta(days=d), time(hour=h)): flat
                        for d in range(2) for h in range(24)}
            except (TypeError, ValueError):
                pass
        raise ValueError(
            f"entity {entity_id!r} attribute {attr!r} missing or unsupported shape "
            "(expected list[float], dict[iso_timestamp, float], iso-keyed attributes, "
            "or a scalar state)"
        )

    @staticmethod
    def _parse_iso_keyed_dict(raw: dict, *, lenient: bool) -> dict[datetime, float]:
        """Parse {iso_timestamp: number} into {naive-UTC hour: float}.

        With ``lenient=True``, entries whose key isn't an ISO timestamp or
        whose value isn't numeric are silently skipped (used when scanning a
        whole ``attributes`` dict that may carry unrelated metadata).
        """
        out: dict[datetime, float] = {}
        for k, v in raw.items():
            try:
                key = _parse_iso(str(k)).replace(minute=0, second=0, microsecond=0)
            except (ValueError, TypeError):
                if lenient:
                    continue
                raise
            try:
                out[key] = float(v)
            except (TypeError, ValueError):
                if lenient:
                    continue
                raise
        return out


    # ---- low-level readers ----------------------------------------------
    def _state(self, entity_id: str) -> StateView:
        st = self.reader.get(entity_id)
        if st is None:
            raise KeyError(f"entity {entity_id!r} not found")
        return st

    def _read_float(self, entity_id: str) -> float:
        st = self._state(entity_id)
        if st.state in _BAD_STATES:
            raise ValueError(f"entity {entity_id!r} has no numeric state")
        return float(st.state)

    def _read_bool_optional(self, entity_id: str | None, *, default: bool) -> bool:
        if not entity_id:
            return default
        st = self.reader.get(entity_id)
        if st is None or st.state in _BAD_STATES:
            return default
        return str(st.state).lower() in ("on", "true", "1", "yes")

    def _read_float_optional(self, entity_id: str | None, *,
                             default: float) -> float:
        if not entity_id:
            return default
        st = self.reader.get(entity_id)
        if st is None or st.state in _BAD_STATES:
            return default
        try:
            return float(st.state)
        except (TypeError, ValueError):
            return default

    def _read_float_or_none(self, entity_id: str | None) -> float | None:
        """Like ``_read_float_optional`` but returns ``None`` (not a default)
        when the entity is unset, missing, or non-numeric — so callers can
        fail-safe on a genuinely-absent reading."""
        if not entity_id:
            return None
        st = self.reader.get(entity_id)
        if st is None or st.state in _BAD_STATES:
            return None
        try:
            return float(st.state)
        except (TypeError, ValueError):
            return None

    def _read_datetime_optional(self, entity_id: str | None
                                 ) -> datetime | None:
        if not entity_id:
            return None
        st = self.reader.get(entity_id)
        if st is None or st.state in _BAD_STATES:
            return None
        try:
            dt = _parse_iso(st.state)
        except (TypeError, ValueError):
            return None
        return dt

    def session_energy_kwh(self) -> float:
        """Current EV session energy in kWh.

        Prefers the user-configured session-energy sensor; falls back to
        the internal integrator (Task 14). Returns 0.0 when no EV is
        configured. Public so the sensor platform reads from the same
        source as the LP — otherwise the sensor would show the never-
        updated integrator field while the LP correctly tracks the
        external entity.

        When the car is physically disconnected, returns 0 regardless of
        what the external sensor / internal integrator is holding. Many
        EVCS firmwares only reset their session counter on plug-in, so
        between sessions the entity reports the *previous* session's
        total — without this guard the planner would conclude the target
        is already met and skip a freshly scheduled session.
        """
        ev_cfg = self.config.ev
        if ev_cfg is None:
            return 0.0
        if (classify_state(self._read_text(ev_cfg.charger_state_entity))
                == EVStateClass.DISCONNECTED):
            return 0.0
        if ev_cfg.session_energy_entity:
            return self._read_float_optional(
                ev_cfg.session_energy_entity, default=0.0)
        return self.ev_state.session_energy_kwh if self.ev_state else 0.0

    def _read_pv_forecast(self, entity_id: str | Sequence[str],
                          slot_starts: list[datetime], slot_h: float) -> list[float]:
        # Normalise to a list so the rest of the function is uniform; a bare
        # string is the legacy single-entity case.
        entity_ids: list[str] = (
            [entity_id] if isinstance(entity_id, str) else list(entity_id)
        )
        if not entity_ids:
            raise ValueError("PV forecast entity list is empty")
        merged: dict[datetime, float] = {}
        for eid in entity_ids:
            merged.update(self._read_pv_forecast_mapping(eid))
        return [_lookup_forecast(merged, s, slot_h) for s in slot_starts]

    def _read_pv_forecast_mapping(self, entity_id: str) -> dict[datetime, float]:
        st = self._state(entity_id)
        # Accept any of:
        #   * {"wh_hours": {"<iso>": Wh, ...}}                         (forecast.solar)
        #   * {"forecast": [{"datetime": "<iso>", "power_kw": <kW>}]}  (generic)
        #   * {"detailedHourly": [{"period_start": "<iso>",
        #                          "pv_estimate": <kW>}, ...]}         (Solcast HACS)
        wh_hours = st.attributes.get("wh_hours")
        if isinstance(wh_hours, dict) and wh_hours:
            # wh_hours values are energy per 1h slot; convert Wh -> kW (since slot=1h).
            # If our slot != 1h we still treat each entry as average power for that hour.
            return {_parse_iso(k): float(v) / 1000.0 for k, v in wh_hours.items()}
        forecast = st.attributes.get("forecast")
        if isinstance(forecast, list) and forecast:
            return {_parse_iso(p["datetime"]): float(p.get("power_kw", p.get("power", 0)))
                    for p in forecast if "datetime" in p}
        # Solcast: ``pv_estimate`` is the median (P50) average kW over the
        # hour. Users wanting a conservative bias can build a template
        # sensor that re-keys ``pv_estimate10`` under the same attribute.
        detailed_hourly = st.attributes.get("detailedHourly")
        if isinstance(detailed_hourly, list) and detailed_hourly:
            return {_parse_iso(p["period_start"]): float(p["pv_estimate"])
                    for p in detailed_hourly
                    if "period_start" in p and "pv_estimate" in p}
        raise ValueError(f"PV forecast entity {entity_id!r} has no recognised attribute")

    def _read_load_forecast(self, entity_id: str | None, slot_starts: list[datetime],
                            slot_h: float, fallback_kw: float) -> list[float]:
        # External-entity escape hatch takes precedence over the built-in
        # forecaster, so users can plug in their own forecast without code
        # changes.
        if entity_id:
            st = self.reader.get(entity_id)
            if st is not None:
                forecast = st.attributes.get("forecast")
                if isinstance(forecast, list) and forecast:
                    mapping = {_parse_iso(p["datetime"]): float(p.get("power_kw", p.get("power", 0)))
                               for p in forecast if "datetime" in p}
                    return [_lookup_forecast(mapping, s, slot_h, default=fallback_kw)
                            for s in slot_starts]
            return [max(0.0, fallback_kw)] * len(slot_starts)
        if self.load_forecaster is not None:
            # Subtract EV history from the load forecast only in auto mode,
            # where the LP separately models p_ev_chg. In car/off the planner
            # ignores/suppresses LP-side EV writes, so historical EV is
            # legitimate opaque household load that the LP must plan around.
            subtract_ev = (
                self.config.ev is not None and self._read_mode() == "auto"
            )
            fc = self.load_forecaster.forecast(slot_starts, subtract_ev=subtract_ev)
            return [fc.kw_per_slot.get(s, max(0.0, fallback_kw)) if fc.days_used_per_slot.get(s, 0) > 0
                    else max(0.0, fallback_kw) for s in slot_starts]
        return [max(0.0, fallback_kw)] * len(slot_starts)

    # ---- output appliers -------------------------------------------------
    def _apply_setpoint(self, value_w: float) -> None:
        if not self.config.grid_setpoint_entity:
            return
        self.caller.call("number", "set_value",
                         {"entity_id": self.config.grid_setpoint_entity, "value": float(value_w)})

    def _apply_feedin(self, allowed: bool) -> None:
        if not self.config.feedin_switch_entity:
            return
        service = "turn_on" if allowed else "turn_off"
        self.caller.call("switch", service, {"entity_id": self.config.feedin_switch_entity})

    # ---- EV control -------------------------------------------------------

    def _apply_ev(self, now: datetime, plan_first: SlotPlan) -> None:
        cfg = self.config.ev
        if cfg is None or self.ev_state is None:
            return
        mode = self._read_mode()  # "auto" / "car" / "off"
        self.ev_state.last_mode = mode
        # Future-planned-start gate: in auto mode, the planner drives no
        # charging at all before the scheduled start — that stays the LP's job
        # from planned_start on. We do not even soak up free PV surplus early:
        # a user who set a future start may have done so deliberately (cheap or
        # even negative grid prices later), so topping the car up from solar
        # now could be the wrong trade. Still, don't leave the charger in
        # whatever stale state it was in: hand it to passive "Auto" mode but
        # keep it idle (start OFF), so it returns to a clean, not-charging
        # state. Writing start OFF (rather than skipping the write) also
        # self-corrects a stale ON from a prior session. Car mode explicitly
        # overrides — user intent beats schedule.
        if mode == "auto":
            planned_start = self._read_datetime_optional(
                cfg.planned_start_entity)
            if planned_start is not None and planned_start > now:
                self._write_ev_charger_mode_passive()
                self._write_ev_start(False)
                return
        if mode == "off":
            return
        # Read inputs.
        raw_state = self._read_text(cfg.charger_state_entity)
        state_class = classify_state(raw_state)
        ev_power_w = self._read_charging_power_w(cfg.charging_power_entity)
        price_buy = self._first_slot_buy_price()
        grid_w = self._read_float(self.config.grid_power_entity)
        # Session energy integrator (only when user has no session_energy_entity).
        es = self.ev_state
        if cfg.session_energy_entity is None and es is not None:
            if (es.last_state_class == EVStateClass.DISCONNECTED
                    and state_class != EVStateClass.DISCONNECTED):
                # Plug-in transition — reset integrator.
                es.session_energy_kwh = 0.0
                es.last_charging_power_kw = None
                es.last_tick = None
            # Trapezoidal integration of ev_charging_power between ticks.
            cur_kw = ev_power_w / 1000.0
            if (es.last_tick is not None
                    and es.last_charging_power_kw is not None
                    and state_class != EVStateClass.DISCONNECTED):
                dt_h = max(0.0,
                           (now - es.last_tick).total_seconds() / 3600.0)
                avg_kw = (es.last_charging_power_kw + cur_kw) / 2.0
                es.session_energy_kwh += avg_kw * dt_h
            es.last_tick = now
            es.last_charging_power_kw = cur_kw
        # Update dwells.
        if es.last_state_class != state_class:
            es.last_state_class = state_class
        if ev_power_w < cfg.params.session_done_power_w:
            if es.low_power_since is None:
                es.low_power_since = now
            low_power_s = (now - es.low_power_since).total_seconds()
        else:
            es.low_power_since = None
            low_power_s = 0.0
        # Reset car-mode "charging seen" flag whenever we leave car mode
        # or the car goes away. Both transitions end the session whether
        # or not it ever actually drew power.
        if mode != "car" or state_class == EVStateClass.DISCONNECTED:
            es.car_session_charging_seen = False
        # Car mode: planner stays out of the car's way — write max current,
        # active charger mode, and start switch every tick. Auto-return to
        # 'auto' is opt-in via switch.pv_optimizer_ev_car_auto_return; OFF
        # by default (sticky), ON enables today's session-done behavior.
        if mode == "car":
            car_auto_return = self._read_bool_optional(
                cfg.car_auto_return_entity, default=False)
            if not car_auto_return:
                # Bookkeeping only runs while auto-return is enabled; clear
                # any stale flag from a prior switch-ON window so a later
                # re-enable starts from a fresh "no charging seen yet"
                # baseline.
                es.car_session_charging_seen = False
            if car_auto_return:
                if ev_power_w >= cfg.params.session_done_power_w:
                    es.car_session_charging_seen = True
                disconnected_done = state_class == EVStateClass.DISCONNECTED
                idle_done = (
                    es.car_session_charging_seen
                    and is_session_done(state_class=state_class,
                                        ev_charging_power_w=ev_power_w,
                                        low_power_seconds=low_power_s,
                                        ev=cfg.params)
                )
                if disconnected_done or idle_done:
                    self._write_mode_auto()
                    return
            # Mode first so any mode-transition cache invalidation lands
            # before the current write — otherwise the current would be
            # written, then immediately have its cache cleared, and re-fire
            # unnecessarily on the next tick.
            self._write_ev_charger_mode_active()
            self._write_ev_current(cfg.params.max_charging_current_a)
            self._write_ev_start(True)
            return
        # Auto mode: dispatch on LP-plan presence.
        if plan_first.p_ev_chg_kw > 0:
            current = translate_lp_slot0(
                p_ev_chg_kw=plan_first.p_ev_chg_kw,
                state_class=state_class,
                ev=cfg.params,
            )
            # Same mode-first ordering rationale as the car branch above.
            if current > 0:
                self._write_ev_charger_mode_active()
            else:
                self._write_ev_charger_mode_passive()
            self._write_ev_current(current)
            self._write_ev_start(current > 0)
            return
        # Reactive path.
        if self._run_surplus_probe(plan_first):
            return
        es.cheap_grid_active = price_buy <= cfg.params.buy_price_threshold
        if cfg.charger_mode_entity:
            # Mode-switching variant: cheap-grid drives the mode flip. When
            # not cheap, hand back to the EVCS (passive + max so its own
            # surplus/solar logic decides). Start is written ON in both
            # branches: the EVCS can only surplus-charge with the start switch
            # on, so leaving it untouched would strand a stale OFF (e.g. one
            # left by the future-planned-start gate) and silently block
            # surplus charging.
            if es.cheap_grid_active:
                self._write_ev_charger_mode_active()
                self._write_ev_current(cfg.params.max_charging_current_a)
                self._write_ev_start(True)
            else:
                self._write_ev_charger_mode_passive()
                self._write_ev_current(cfg.params.max_charging_current_a)
                self._write_ev_start(True)
        else:
            # No mode entity: decide_reactive owns everything.
            decision = decide_reactive(
                state_class=state_class,
                grid_power_w=grid_w,
                ev_charging_power_w=ev_power_w,
                price_buy=price_buy,
                ev=cfg.params,
            )
            self._write_ev_current(decision.max_current_a)
            self._write_ev_start(decision.max_current_a > 0)

    # ---- EV helpers -------------------------------------------------------

    def _run_surplus_probe(self, plan_first) -> bool:
        """If conditions warrant, take over surplus charging from the EVCS and
        return True (writes active charger mode if a charger_mode_entity is
        configured, regulated current, and start). Otherwise reset any probe
        state and return False so the caller's reactive path runs.
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
        )
        if not armed:
            if es.probe_armed:
                es.probe_armed = False
                es.probe_current_a = 0
                es.probe_cycles_since_up = 0
                es.probe_cycles_overshooting = 0
            return False
        decision = decide_surplus_probe(
            battery_discharge_w=max(0.0, -batt),
            grid_import_w=max(0.0, grid),
            forecast_surplus_kw=forecast_surplus,
            current_a=es.probe_current_a,
            cycles_since_up=es.probe_cycles_since_up,
            cycles_overshooting=es.probe_cycles_overshooting,
            ev=cfg.params,
        )
        # Mode first so any active/passive transition cache invalidation lands
        # before the (forced) current write.
        self._write_ev_charger_mode_active()
        self._write_ev_current(decision.current_a, force=True)
        self._write_ev_start(decision.current_a > 0)
        es.probe_armed = True
        es.probe_current_a = decision.current_a
        es.probe_cycles_since_up = decision.cycles_since_up
        es.probe_cycles_overshooting = decision.cycles_overshooting
        return True

    def _read_mode(self) -> str:
        if not self.config.ev or not self.config.ev.mode_entity:
            return "auto"
        st = self.reader.get(self.config.ev.mode_entity)
        if st is None or st.state in _BAD_STATES:
            return "auto"
        return str(st.state).lower()

    def _read_text(self, entity_id: str) -> str | None:
        st = self.reader.get(entity_id)
        return None if st is None else st.state

    def _read_charging_power_w(self, entity_id: str) -> float:
        st = self.reader.get(entity_id)
        if st is None or st.state in _BAD_STATES:
            return 0.0
        try:
            v = float(st.state)
        except (TypeError, ValueError):
            return 0.0
        unit = str(st.attributes.get("unit_of_measurement", "")).lower()
        if unit == "kw":
            return v * 1000.0
        return v

    def _first_slot_buy_price(self) -> float:
        return self._cached_first_buy_price

    def _write_ev_current(self, value_a: float, *, force: bool = False) -> None:
        cfg = self.config.ev
        if cfg is None:
            return
        es = self.ev_state
        new_val = int(round(value_a))
        if (not force and es and es.last_written_current_a is not None
                and abs(new_val - es.last_written_current_a)
                <= cfg.params.current_tolerance_a):
            return  # within tolerance
        self.caller.call("number", "set_value", {
            "entity_id": cfg.max_current_entity, "value": new_val,
        })
        if es is not None:
            es.last_written_current_a = new_val

    def _write_ev_start(self, on: bool) -> None:
        # Unconditional: write every tick so the planner self-corrects against
        # firmware resets (EVCS clears the charging switch on Auto→Manual on
        # some builds) and external user toggles. A switch service call is
        # cheap; the logbook entry per planner tick is acceptable noise.
        cfg = self.config.ev
        if cfg is None or not cfg.start_switch_entity:
            return
        service = "turn_on" if on else "turn_off"
        self.caller.call("switch", service, {"entity_id": cfg.start_switch_entity})

    def _write_ev_charger_mode(self, role: str) -> None:
        """Set the charger's own auto/manual mode entity.

        ``role`` is the internal label ("active" / "passive"); the option
        string sent to HA comes from EVConfig (vendor-specific: EVCS uses
        "Manual"/"Auto", Zappi different). Unconditional: we self-correct
        against external mode toggles by writing every tick. When the role
        differs from the previously written one we also invalidate
        ``last_written_current_a`` because some charger firmwares reset
        their internal current register on a mode transition, so the next
        ``_write_ev_current`` must be allowed through even if the target
        value is unchanged.
        """
        cfg = self.config.ev
        if cfg is None or not cfg.charger_mode_entity:
            return
        option = (cfg.charger_mode_option_active if role == "active"
                  else cfg.charger_mode_option_passive)
        self.caller.call("select", "select_option", {
            "entity_id": cfg.charger_mode_entity,
            "option": option,
        })
        es = self.ev_state
        if es is not None:
            if es.last_written_charger_mode != role:
                es.last_written_current_a = None  # firmware may reset register
            es.last_written_charger_mode = role

    def _write_ev_charger_mode_active(self) -> None:
        self._write_ev_charger_mode("active")

    def _write_ev_charger_mode_passive(self) -> None:
        self._write_ev_charger_mode("passive")

    def _write_mode_auto(self) -> None:
        cfg = self.config.ev
        if cfg is None or not cfg.mode_entity:
            return
        self.caller.call("select", "select_option", {
            "entity_id": cfg.mode_entity, "option": "auto",
        })


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _floor_to_slot(now: datetime, slot_minutes: int) -> datetime:
    minute = (now.minute // slot_minutes) * slot_minutes
    return now.replace(minute=minute, second=0, microsecond=0)


def _parse_iso(s: str | datetime) -> datetime:
    # Accept "...Z" and offset-aware/naive ISO strings, or pre-parsed
    # ``datetime`` objects (e.g. Solcast HACS exposes ``period_start`` as a
    # tz-aware datetime, not a string). Always returns a naive-UTC,
    # second/microsecond-stripped datetime suitable for slot keying.
    if isinstance(s, datetime):
        dt = s
    else:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(second=0, microsecond=0)


def naive_utc_to_iso(dt: datetime) -> str:
    """Serialise a naive-UTC datetime as an offset-aware ISO 8601 string.

    Internal slot keys are naive UTC (see ``_parse_iso``). Frontends like
    apexcharts-card need an explicit offset to convert reliably to the
    browser's local timezone; without one, ISO strings are interpreted
    inconsistently across cards/browsers and series end up misaligned on
    the x-axis. Already-aware datetimes are converted to UTC first so the
    output is always ``...+00:00``.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def _lookup_forecast(mapping: dict[datetime, float], slot_start: datetime,
                     slot_h: float, default: float = 0.0) -> float:
    key = slot_start.replace(tzinfo=None) if slot_start.tzinfo else slot_start
    key = key.replace(second=0, microsecond=0, minute=(key.minute // 60) * 60)
    return float(mapping.get(key, default))


def _attach_physical_soc(inputs: OptimizerInputs,
                         plan_slots: list[SlotPlan],
                         eps: float,
                         *,
                         force_pv_export_enabled: bool = False) -> list[SlotPlan]:
    """Return ``plan_slots`` with ``soc_physical_kwh`` and ``setpoint_w``
    filled in.

    Both fields share the same per-slot active/passive predicates so they
    stay in lock-step and a single function owns the §8.1 rules — slot 0's
    ``setpoint_w`` is what the planner actually writes to the inverter,
    and the chart series for the rest of the horizon comes from the same
    field, so the dashboard never has to re-implement the predicates.

    The grid setpoint should only override the inverter's natural self-
    consumption logic when the LP actively wants to move energy *between
    the battery and the grid*. In every other case (PV surplus export,
    PV-deficit import, idle battery) we hand control back to the inverter
    with setpoint = 0 — the Multiplus then runs self-consumption mode
    (PV → load → battery → grid) with surplus exported iff the feed-in
    switch is on. Forcing a non-zero setpoint when the LP doesn't intend a
    battery transfer is brittle to forecast error: a PV undershoot would
    make the inverter discharge the battery to hit a negative target it
    was never asked to defend.

    Two opt-in / always-on extensions punch through that default:

    * Force-PV-export (PRD §8.2, opt-in via ``force_pv_export_entity``):
      when the toggle is on, a pure-PV-export slot (``p_sell > 0`` with
      battery idle) gets an active negative setpoint instead of being left
      passive. Safe because ``_build_inputs`` clamps slot-0 PV to
      ``min(forecast, live_avg)``, so the LP can't speculate above
      measured production.
    * Force-hold-import (PRD §8.6, always on): the LP can rationally plan
      to cover load purely from the grid with the battery idle (typically
      when the §8.5 health floor makes further discharge expensive, or
      when the configured ``soc_min`` sits above the inverter's BMS
      floor). The native EMS would still drain the battery first in that
      case, silently violating the plan, so we explicitly pin the grid
      setpoint to the planned import. When the predicate doesn't fire
      (e.g. battery already at the hard floor) the forced positive
      setpoint produces the same physical behaviour as setpoint 0.

    Per-slot behaviour:

    * If the LP forces a battery action (force-charge, force-discharge,
      force-export, or force-hold-import), the inverter follows the LP
      and we apply the LP's own SoC update; ``setpoint_w`` is set to
      ``(p_buy − p_sell) · 1000``. For force-export and force-hold-import
      the LP keeps the battery idle (``p_chg = p_dis = 0``) so the
      projection stays flat too.
    * Otherwise the inverter runs in self-consumption mode
      (``setpoint_w = 0``): surplus PV charges the battery up to
      ``soc_max``, deficit is met by discharging down to ``soc_min``; the
      rest spills to grid or is curtailed. This is what the Multiplus
      actually does between forced slots, so the projected SoC tracks
      reality more closely than the LP bookkeeping does (which curtails
      surplus PV in passive slots).
    """
    bat = inputs.battery
    soc = inputs.initial_soc_kwh
    out: list[SlotPlan] = []
    for t, sp in enumerate(plan_slots):
        dt = inputs.slots[t].duration_h
        pv = inputs.pv_kw[t]
        load = inputs.load_kw[t]
        force_discharge = sp.p_dis_kw > eps and sp.p_sell_kw > eps
        force_charge = sp.p_buy_kw > eps and sp.p_chg_kw > eps
        force_export = (force_pv_export_enabled
                        and sp.p_sell_kw > eps
                        and sp.p_chg_kw < eps
                        and sp.p_dis_kw < eps)
        force_hold_import = (sp.p_buy_kw > eps
                             and sp.p_chg_kw < eps
                             and sp.p_dis_kw < eps)
        active = force_charge or force_discharge or force_export or force_hold_import
        setpoint_w = (sp.p_buy_kw - sp.p_sell_kw) * 1000.0 if active else 0.0
        out.append(replace(sp, soc_physical_kwh=soc, setpoint_w=setpoint_w))
        if active:
            dsoc = dt * (bat.eta_chg * sp.p_chg_kw - sp.p_dis_kw / bat.eta_dis)
        else:
            net = pv - load  # >0 surplus, <0 deficit, all on AC side
            if net > eps:
                # AC kW that fits in remaining headroom over this slot.
                max_chg_ac = (bat.soc_max_kwh - soc) / (bat.eta_chg * dt) if dt > 0 else 0.0
                chg_ac = min(net, bat.p_chg_max_kw, max(0.0, max_chg_ac))
                dsoc = dt * bat.eta_chg * chg_ac
            elif net < -eps:
                # AC kW deliverable from remaining usable energy over this slot.
                max_dis_ac = (soc - bat.soc_min_kwh) * bat.eta_dis / dt if dt > 0 else 0.0
                dis_ac = min(-net, bat.p_dis_max_kw, max(0.0, max_dis_ac))
                dsoc = -dt * dis_ac / bat.eta_dis
            else:
                dsoc = 0.0
        soc = max(bat.soc_min_kwh, min(bat.soc_max_kwh, soc + dsoc))
    return out
