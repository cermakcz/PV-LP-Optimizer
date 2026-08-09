"""Unit tests for the planner (pure layer driving the optimizer + side effects)."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from custom_components.pv_optimizer.load_forecaster import (
    LoadForecaster,
    LoadForecasterConfig,
)
from custom_components.pv_optimizer.models import BatteryParams
from custom_components.pv_optimizer.planner import (
    Planner,
    PlannerConfig,
    StateView,
    naive_utc_to_iso,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeReader:
    def __init__(self, states: dict[str, StateView]) -> None:
        self._states = states

    def get(self, entity_id: str) -> StateView | None:
        return self._states.get(entity_id)


class FakeCaller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def call(self, domain: str, service: str, data: dict[str, Any]) -> None:
        self.calls.append((domain, service, data))


class FakeLiveAverager:
    """Trivial LiveAverager that returns a configured kW value (or None)."""

    def __init__(self, value: float | None) -> None:
        self.value = value
        self.calls: list[tuple[str, datetime, datetime]] = []

    def average_kw(self, entity_id: str, since: datetime, until: datetime
                   ) -> float | None:
        self.calls.append((entity_id, since, until))
        return self.value


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 1, 12, 0, 0)  # noon, naive UTC
ENTITY_IDS = dict(
    load_power_entity="sensor.load_w",
    pv_power_entity="sensor.pv_w",
    grid_power_entity="sensor.grid_w",
    battery_soc_entity="sensor.soc_pct",
    buy_price_today_entity="sensor.buy",
    sell_price_today_entity="sensor.sell",
    pv_forecast_entity="sensor.pv_forecast",
    grid_setpoint_entity="number.grid_setpoint",
    feedin_switch_entity="switch.feedin",
)


def _config(**overrides) -> PlannerConfig:
    bat = BatteryParams(
        capacity_kwh=10.0, soc_min_kwh=1.0, soc_max_kwh=9.0,
        p_chg_max_kw=5.0, p_dis_max_kw=5.0,
        eta_chg=1.0, eta_dis=1.0, cycle_cost_per_kwh=0.0,
    )
    return PlannerConfig(
        battery=bat,
        p_grid_imp_max_kw=10.0,
        p_grid_exp_max_kw=10.0,
        slot_minutes=60,
        horizon_hours=4,  # short horizon for fast tests
        **{**ENTITY_IDS, **overrides},
    )


def _states(*, soc_pct=50.0, load_w=1000.0,
            buy=None, sell=None, buy_attr="today", sell_attr="today",
            pv_forecast=None, missing: tuple[str, ...] = ()) -> dict[str, StateView]:
    """Build a fake state map. ``buy``/``sell`` may be a list[24] or any other
    shape (dict) that the planner accepts under the given attribute name."""
    if buy is None:
        buy = [0.30] * 24
    if sell is None:
        sell = [0.10] * 24
    if pv_forecast is None:
        # 4-hour horizon starting at noon -> hours 12..15 in wh_hours.
        pv_forecast = {
            (NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 0.0
            for h in range(4)
        }
    raw = {
        "sensor.soc_pct": StateView(state=str(soc_pct), attributes={}),
        "sensor.load_w": StateView(state=str(load_w), attributes={}),
        "sensor.pv_w": StateView(state="0", attributes={}),
        "sensor.grid_w": StateView(state="0", attributes={}),
        "sensor.buy": StateView(state="0.30", attributes={buy_attr: buy}),
        "sensor.sell": StateView(state="0.10", attributes={sell_attr: sell}),
        "sensor.pv_forecast": StateView(state="0", attributes={"wh_hours": pv_forecast}),
    }
    for k in missing:
        raw.pop(k, None)
    return raw


def _prague_iso(utc_dt: datetime) -> str:
    """Format a naive-UTC datetime as a Prague-local ISO 8601 string (+02:00 in May).

    The user's tariff plugin emits timestamps in local time with explicit offset.
    """
    local = utc_dt + timedelta(hours=2)
    return local.strftime("%Y-%m-%dT%H:00:00+02:00")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_step_happy_path_writes_setpoint_and_feedin() -> None:
    states = _states()  # constant 0.30 buy, 0.10 sell, no PV, 1 kW load
    caller = FakeCaller()
    planner = Planner(_config(), FakeReader(states), caller)

    cycle = planner.step(NOW)

    assert cycle.error is None
    assert cycle.result is not None and cycle.result.status == "Optimal"
    # LP plans to import 1 kW to cover the load with the battery idle.
    # Force-hold-import (PRD §8.6) pins the grid set-point to the planned
    # import so the inverter doesn't silently drain the battery instead.
    first = cycle.result.slots[0]
    assert first.p_buy_kw == pytest.approx(1.0, abs=1e-3)
    assert first.p_chg_kw == pytest.approx(0.0, abs=1e-3)
    assert first.p_dis_kw == pytest.approx(0.0, abs=1e-3)
    assert cycle.applied_setpoint_w == pytest.approx(1000.0, abs=1e-3)
    assert cycle.applied_feedin is False
    # number.set_value(0) still issued on the first cycle (no prior state),
    # and the feed-in switch is explicitly turned off.
    services = [(d, s) for d, s, _ in caller.calls]
    assert ("number", "set_value") in services
    assert ("switch", "turn_off") in services


def test_missing_required_sensor_records_error_and_no_writes() -> None:
    states = _states(missing=("sensor.load_w",))
    caller = FakeCaller()
    planner = Planner(_config(), FakeReader(states), caller)

    cycle = planner.step(NOW)

    assert cycle.error is not None and "load_w" in cycle.error
    assert cycle.result is None
    assert caller.calls == []


def test_setpoint_within_tolerance_not_rewritten() -> None:
    states = _states()
    caller = FakeCaller()
    cfg = _config(setpoint_tolerance_w=200.0)
    planner = Planner(cfg, FakeReader(states), caller)

    planner.step(NOW)
    n_after_first = len(caller.calls)
    # Same state -> same plan -> setpoint unchanged within tolerance.
    planner.step(NOW + timedelta(minutes=5))
    n_after_second = len(caller.calls)
    # Only feedin may be re-applied (it isn't, since unchanged); definitely no second number.set_value.
    assert all(s != "set_value" for _, s, _ in caller.calls[n_after_first:])
    assert n_after_second >= n_after_first  # sanity


def test_pv_surplus_triggers_feedin_on_without_forced_setpoint() -> None:
    # 5 kW PV against 1 kW load = 4 kW surplus. The LP wants to export it
    # but doesn't need the battery to do so — setpoint must stay at 0
    # (passive surplus export via the Multiplus's own logic), with feed-in on.
    pv_forecast = {(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 5000.0
                   for h in range(4)}
    states = _states(pv_forecast=pv_forecast)
    caller = FakeCaller()
    planner = Planner(_config(), FakeReader(states), caller)

    cycle = planner.step(NOW)

    first = cycle.result.slots[0]
    assert first.p_sell_kw > 0  # LP exports
    assert first.p_dis_kw == pytest.approx(0.0, abs=1e-3)  # but not from battery
    assert cycle.applied_setpoint_w == pytest.approx(0.0, abs=1e-3)
    assert cycle.applied_feedin is True
    assert ("switch", "turn_on") in [(d, s) for d, s, _ in caller.calls]


def test_physical_soc_projection_charges_in_passive_surplus() -> None:
    # Same passive-surplus scenario as above. The LP's bookkeeping leaves
    # soc_start_kwh flat (no p_chg_kw because the export is profitable enough
    # without cycling), but the inverter physically absorbs the 4 kW surplus
    # into the battery — soc_physical_kwh should reflect that.
    pv_forecast = {(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 5000.0
                   for h in range(4)}
    states = _states(pv_forecast=pv_forecast)
    planner = Planner(_config(), FakeReader(states), FakeCaller())

    cycle = planner.step(NOW)
    slots = cycle.result.slots

    # Initial SoC = 50% of 10 kWh = 5 kWh; soc_max = 9 kWh.
    assert slots[0].soc_physical_kwh == pytest.approx(5.0, abs=1e-3)
    # 4 kW surplus * 1 h * eta_chg=1.0 = 4 kWh charged within headroom (4 kWh).
    assert slots[1].soc_physical_kwh == pytest.approx(9.0, abs=1e-3)
    # Battery full; subsequent slots stay pinned at soc_max.
    for s in slots[1:]:
        assert s.soc_physical_kwh == pytest.approx(9.0, abs=1e-3)
    # LP bookkeeping never cycled the battery — projection diverges from it.
    assert slots[1].soc_start_kwh == pytest.approx(5.0, abs=1e-3)


def test_physical_soc_projection_follows_lp_when_force_exporting() -> None:
    # PV surplus with force-export enabled: the inverter pushes the surplus
    # to the grid instead of charging the battery, so the projection must
    # stay flat (mirroring the LP's idle-battery decision) rather than
    # ramping up via the passive self-consumption rule.
    pv_forecast = {(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 5000.0
                   for h in range(4)}
    states = _states(pv_forecast=pv_forecast)
    states["input_boolean.force_export"] = StateView(state="on", attributes={})
    cfg = _config(force_pv_export_entity="input_boolean.force_export")
    cycle = Planner(cfg, FakeReader(states), FakeCaller()).step(NOW)
    slots = cycle.result.slots

    # LP keeps battery idle (p_chg = p_dis = 0) and sells the surplus; the
    # projection must defer to that instead of inferring a passive charge.
    for s in slots:
        assert s.p_chg_kw == pytest.approx(0.0, abs=1e-3)
        assert s.p_dis_kw == pytest.approx(0.0, abs=1e-3)
    # Initial SoC = 5 kWh; with no battery action the projection stays at 5.
    for s in slots:
        assert s.soc_physical_kwh == pytest.approx(5.0, abs=1e-3)


def test_physical_soc_projection_follows_lp_when_forced() -> None:
    # Same setup as ``test_force_charge_writes_positive_setpoint``: cheap
    # noon buy, expensive afterwards, no profitable export. The LP forces
    # a battery charge from the grid in slot 0; the projection must defer
    # to that LP decision rather than re-running self-consumption logic.
    states = _states(
        load_w=500.0,
        buy=_hourly(value_at_noon=0.01, value_elsewhere=5.0),
        sell=[0.0] * 24,
    )
    planner = Planner(_config(), FakeReader(states), FakeCaller())

    cycle = planner.step(NOW)
    slots = cycle.result.slots

    first = slots[0]
    assert first.p_buy_kw > 1e-3 and first.p_chg_kw > 1e-3  # force-charge triggered
    # Under force-charge the projection mirrors the LP exactly.
    assert first.soc_physical_kwh == pytest.approx(first.soc_start_kwh, abs=1e-3)
    # Next slot's projection equals LP's next soc_start (eta=1.0 in tests).
    assert slots[1].soc_physical_kwh == pytest.approx(slots[1].soc_start_kwh, abs=1e-3)



# ---------------------------------------------------------------------------
# Dict-shaped (timestamp-keyed) price entity
# ---------------------------------------------------------------------------


def test_dict_price_format_happy_path() -> None:
    # User's tariff plugin shape: {"<iso-with-+02:00>": price}. Provide today
    # only (24 entries starting at Prague midnight, which is 22:00 UTC the
    # previous day). The 4-hour planning horizon at noon UTC = 14:00 local
    # falls comfortably inside today's data.
    today_local_midnight_utc = datetime(2026, 4, 30, 22, 0)  # 2026-05-01 00:00 +02:00
    buy_dict = {
        _prague_iso(today_local_midnight_utc + timedelta(hours=h)): 0.30
        for h in range(24)
    }
    sell_dict = {k: 0.10 for k in buy_dict}
    states = _states(buy=buy_dict, sell=sell_dict, buy_attr="prices", sell_attr="prices")
    caller = FakeCaller()
    cfg = _config()
    # Tell the planner which attribute name our integration uses.
    cfg = _config(price_today_attr="prices")
    planner = Planner(cfg, FakeReader(states), caller)

    cycle = planner.step(NOW)

    assert cycle.error is None
    assert cycle.result is not None
    assert len(cycle.result.slots) == 4  # full configured horizon used
    # Load coverage with battery idle — force-hold-import (PRD §8.6) pins
    # the set-point to the planned import so the EMS doesn't drain the
    # battery instead.
    assert cycle.result.slots[0].p_buy_kw == pytest.approx(1.0, abs=1e-3)
    assert cycle.applied_setpoint_w == pytest.approx(1000.0, abs=1e-3)


def test_dict_price_format_stale_now_missing_raises() -> None:
    # Dict only contains hours from yesterday -> current slot key absent.
    yesterday_midnight_utc = datetime(2026, 4, 29, 22, 0)
    buy_dict = {
        _prague_iso(yesterday_midnight_utc + timedelta(hours=h)): 0.30
        for h in range(24)
    }
    sell_dict = {k: 0.10 for k in buy_dict}
    states = _states(buy=buy_dict, sell=sell_dict, buy_attr="prices", sell_attr="prices")
    caller = FakeCaller()
    planner = Planner(_config(price_today_attr="prices"), FakeReader(states), caller)

    cycle = planner.step(NOW)

    assert cycle.result is None
    assert cycle.error is not None and "stale" in cycle.error.lower()
    assert caller.calls == []  # no setpoint written when planning fails


def test_dict_price_format_partial_horizon_truncates() -> None:
    # Provide only the current hour and the next one (= 2 slots) out of the
    # configured 4-hour horizon. Planner must succeed with a 2-slot plan.
    buy_dict = {
        _prague_iso(NOW + timedelta(hours=h)): 0.30
        for h in range(2)
    }
    sell_dict = {k: 0.10 for k in buy_dict}
    states = _states(buy=buy_dict, sell=sell_dict, buy_attr="prices", sell_attr="prices")
    caller = FakeCaller()
    planner = Planner(_config(price_today_attr="prices"), FakeReader(states), caller)

    cycle = planner.step(NOW)

    assert cycle.error is None
    assert cycle.result is not None
    assert len(cycle.result.slots) == 2  # truncated from 4 to available 2



def test_dict_price_format_autodiscovers_alternate_attribute_name() -> None:
    # Realistic scenario: a user-built template publishes the price map under
    # ``prices`` while the planner config still uses the Nordpool default
    # ``today``. The auto-discovery step should pick the iso-keyed dict
    # regardless of which key wraps it — no config change required.
    today_local_midnight_utc = datetime(2026, 4, 30, 22, 0)
    iso_buy = {
        _prague_iso(today_local_midnight_utc + timedelta(hours=h)): 0.30
        for h in range(24)
    }
    iso_sell = {k: 0.10 for k in iso_buy}
    buy_attrs = {"prices": iso_buy, "hours_published": 24, "source": "x"}
    sell_attrs = {"prices": iso_sell, "hours_published": 24}

    raw = {
        "sensor.soc_pct": StateView(state="50", attributes={}),
        "sensor.load_w": StateView(state="1000", attributes={}),
        "sensor.pv_w": StateView(state="0", attributes={}),
        "sensor.grid_w": StateView(state="0", attributes={}),
        "sensor.buy": StateView(state="0.30", attributes=buy_attrs),
        "sensor.sell": StateView(state="0.10", attributes=sell_attrs),
        "sensor.pv_forecast": StateView(state="0", attributes={"wh_hours": {
            (NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 0.0
            for h in range(4)
        }}),
    }
    caller = FakeCaller()
    planner = Planner(_config(), FakeReader(raw), caller)  # default attr="today"

    cycle = planner.step(NOW)

    assert cycle.error is None
    assert cycle.result is not None
    assert len(cycle.result.slots) == 4


def test_dict_price_format_top_level_iso_attributes() -> None:
    # Some integrations (e.g. spot_hodinovy_tarif) put the hour timestamps
    # *directly* on the entity's attributes dict — there is no wrapping
    # attribute. The planner should auto-detect this shape, ignore unrelated
    # metadata keys, and use whichever attr name the user configured (it
    # simply won't be found, triggering the fallback scan).
    today_local_midnight_utc = datetime(2026, 4, 30, 22, 0)  # 2026-05-01 +02:00
    iso_buy = {
        _prague_iso(today_local_midnight_utc + timedelta(hours=h)): 0.30
        for h in range(24)
    }
    iso_sell = {k: 0.10 for k in iso_buy}
    # Mix in non-price attributes to prove the lenient scan ignores them.
    buy_attrs = {**iso_buy, "unit_of_measurement": "EUR/kWh", "friendly_name": "Buy"}
    sell_attrs = {**iso_sell, "unit_of_measurement": "EUR/kWh"}

    raw = {
        "sensor.soc_pct": StateView(state="50", attributes={}),
        "sensor.load_w": StateView(state="1000", attributes={}),
        "sensor.pv_w": StateView(state="0", attributes={}),
        "sensor.grid_w": StateView(state="0", attributes={}),
        "sensor.buy": StateView(state="0.30", attributes=buy_attrs),
        "sensor.sell": StateView(state="0.10", attributes=sell_attrs),
        "sensor.pv_forecast": StateView(state="0", attributes={"wh_hours": {
            (NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 0.0
            for h in range(4)
        }}),
    }
    caller = FakeCaller()
    # The configured attr name doesn't exist on the entity — the fallback
    # scan over the whole attributes dict is what makes this work.
    planner = Planner(_config(price_today_attr="prices"), FakeReader(raw), caller)

    cycle = planner.step(NOW)

    assert cycle.error is None
    assert cycle.result is not None
    assert len(cycle.result.slots) == 4
    assert cycle.result.slots[0].p_buy_kw == pytest.approx(1.0, abs=1e-3)
    # Force-hold-import (PRD §8.6): pure-load coverage gets a positive
    # set-point so the EMS doesn't drain the battery instead.
    assert cycle.applied_setpoint_w == pytest.approx(1000.0, abs=1e-3)

def test_dict_price_format_cz_energy_spot_prices_shape() -> None:
    # Plug-and-play check for rnovacek/homeassistant_cz_energy_spot_prices.
    # That integration's ``Current Buy/Spot Electricity Price`` sensor exposes
    # both today's and (when published) tomorrow's hourly prices as a single
    # flat dict at the top of ``attributes``, keyed by Prague-local ISO 8601
    # timestamps with offset, alongside the usual HA metadata. There is no
    # wrapping attribute and no separate "tomorrow" sensor, so users should
    # only have to fill in the today fields and leave tomorrow blank.
    today_local_midnight_utc = datetime(2026, 4, 30, 22, 0)  # 2026-05-01 +02:00
    iso_buy = {
        _prague_iso(today_local_midnight_utc + timedelta(hours=h)): 0.30
        for h in range(48)  # 24 today + 24 tomorrow on the same sensor
    }
    iso_sell = {k: 0.10 for k in iso_buy}
    # Realistic HA metadata that the lenient ISO scan must skip.
    cz_meta = {
        "unit_of_measurement": "EUR/kWh",
        "device_class": "monetary",
        "state_class": "measurement",
        "icon": "mdi:cash",
        "friendly_name": "Current Buy Electricity Price",
    }
    buy_attrs = {**iso_buy, **cz_meta}
    sell_attrs = {**iso_sell, **cz_meta, "friendly_name": "Current Sell Electricity Price"}

    raw = {
        "sensor.soc_pct": StateView(state="50", attributes={}),
        "sensor.load_w": StateView(state="1000", attributes={}),
        "sensor.pv_w": StateView(state="0", attributes={}),
        "sensor.grid_w": StateView(state="0", attributes={}),
        "sensor.buy": StateView(state="0.30", attributes=buy_attrs),
        "sensor.sell": StateView(state="0.10", attributes=sell_attrs),
        "sensor.pv_forecast": StateView(state="0", attributes={"wh_hours": {
            (NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 0.0
            for h in range(4)
        }}),
    }
    # Default attr name (``today``) doesn't exist on the cz sensor; the
    # top-level fallback scan is what makes this plug-and-play. No tomorrow
    # entity is configured -- the today sensor already carries 48 h.
    planner = Planner(_config(), FakeReader(raw), FakeCaller())

    cycle = planner.step(NOW)

    assert cycle.error is None
    assert cycle.result is not None
    assert len(cycle.result.slots) == 4
    assert cycle.result.slots[0].p_buy_kw == pytest.approx(1.0, abs=1e-3)
    assert cycle.applied_setpoint_w == pytest.approx(1000.0, abs=1e-3)


def test_scalar_price_fills_all_slots() -> None:
    # Fixed-rate tariff: entity state is a plain number, no attributes with
    # price data. The planner should fill every slot with that constant value.
    raw = {
        "sensor.soc_pct": StateView(state="50", attributes={}),
        "sensor.load_w": StateView(state="1000", attributes={}),
        "sensor.pv_w": StateView(state="0", attributes={}),
        "sensor.grid_w": StateView(state="0", attributes={}),
        "sensor.buy": StateView(state="0.25", attributes={}),
        "sensor.sell": StateView(state="0.08", attributes={}),
        "sensor.pv_forecast": StateView(state="0", attributes={"wh_hours": {
            (NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 0.0
            for h in range(4)
        }}),
    }
    planner = Planner(_config(), FakeReader(raw), FakeCaller())
    cycle = planner.step(NOW)

    assert cycle.error is None
    assert cycle.result is not None
    assert len(cycle.result.slots) == 4
    # All slots should carry the flat buy/sell price from the scalar state.
    assert cycle.result.slots[0].p_buy_kw == pytest.approx(1.0, abs=1e-3)


def test_pv_forecast_solcast_detailed_hourly_shape() -> None:
    # Solcast HACS integration exposes the forecast as ``detailedHourly``:
    # a list of {period_start: <iso>, pv_estimate: <kW>} entries. The
    # planner should accept this shape natively.
    detailed_hourly = [
        {"period_start": (NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00+00:00"),
         "pv_estimate": 5.0,
         "pv_estimate10": 3.5,
         "pv_estimate90": 6.5}
        for h in range(4)
    ]
    states = _states()
    states["sensor.pv_forecast"] = StateView(
        state="0", attributes={"detailedHourly": detailed_hourly})
    planner = Planner(_config(), FakeReader(states), FakeCaller())

    cycle = planner.step(NOW)

    assert cycle.error is None
    assert cycle.result is not None
    # 5 kW PV - 1 kW load = 4 kW surplus → LP exports, feed-in on, passive setpoint.
    first = cycle.result.slots[0]
    assert first.p_sell_kw == pytest.approx(4.0, abs=1e-3)
    assert cycle.applied_setpoint_w == pytest.approx(0.0, abs=1e-3)
    assert cycle.applied_feedin is True


def test_pv_forecast_detailed_hourly_accepts_datetime_period_start() -> None:
    # The Solcast HACS integration stores ``period_start`` as a tz-aware
    # ``datetime`` (not an ISO string). _parse_iso must accept that shape
    # so users can point ``pv_forecast_entity`` at the Solcast sensor
    # directly without a template-sensor coercion step.
    detailed_hourly = [
        {"period_start": (NOW + timedelta(hours=h)).replace(tzinfo=timezone.utc),
         "pv_estimate": 5.0}
        for h in range(4)
    ]
    states = _states()
    states["sensor.pv_forecast"] = StateView(
        state="0", attributes={"detailedHourly": detailed_hourly})
    planner = Planner(_config(), FakeReader(states), FakeCaller())

    cycle = planner.step(NOW)

    assert cycle.error is None
    assert cycle.result.slots[0].p_sell_kw == pytest.approx(4.0, abs=1e-3)


def test_pv_forecast_multiple_entities_merge_today_and_tomorrow() -> None:
    # Solcast splits the forecast across a today and a tomorrow sensor;
    # passing both as a list to ``pv_forecast_entity`` must merge them so
    # the LP sees a continuous horizon spanning the two days.
    today = [
        {"period_start": (NOW + timedelta(hours=h)).replace(tzinfo=timezone.utc),
         "pv_estimate": 5.0}
        for h in range(2)  # NOW=12:00 -> today covers slots 0-1 (12:00, 13:00).
    ]
    tomorrow = [
        {"period_start": (NOW + timedelta(hours=h)).replace(tzinfo=timezone.utc),
         "pv_estimate": 5.0}
        for h in range(2, 4)  # slots 2-3 (14:00, 15:00) supplied by tomorrow sensor.
    ]
    states = _states()
    states["sensor.pv_today"] = StateView(
        state="0", attributes={"detailedHourly": today})
    states["sensor.pv_tomorrow"] = StateView(
        state="0", attributes={"detailedHourly": tomorrow})
    cfg = _config(pv_forecast_entity=["sensor.pv_today", "sensor.pv_tomorrow"])
    planner = Planner(cfg, FakeReader(states), FakeCaller())

    cycle = planner.step(NOW)

    assert cycle.error is None
    # All four slots see 4 kW surplus (5 PV - 1 load) once the merged map
    # covers the entire horizon. If the merge weren't happening, slots 2-3
    # would fall back to 0 PV and produce a 1 kW import instead.
    for s in cycle.result.slots:
        assert s.p_sell_kw == pytest.approx(4.0, abs=1e-3)


class _FakeHistory:
    """Step-wise history with a single constant value, density 15 minutes."""

    def __init__(self, value_kw: float) -> None:
        self.value_kw = value_kw

    def get_history(self, entity_id: str, start: datetime, end: datetime
                    ) -> list[tuple[datetime, float]]:
        out: list[tuple[datetime, float]] = []
        cursor = start
        while cursor < end:
            out.append((cursor, self.value_kw))
            cursor += timedelta(minutes=15)
        return out


def test_built_in_forecaster_drives_load_when_no_entity_set() -> None:
    # Forecaster reports 2.5 kW (vs the 1 kW current-load fallback). With
    # battery idle this is force-hold-import (PRD §8.6) — the LP's
    # forecaster-driven import target is mirrored 1:1 in the set-point.
    states = _states(buy=[0.10] * 24, sell=[0.05] * 24, load_w=1000.0)
    caller = FakeCaller()
    forecaster = LoadForecaster(
        LoadForecasterConfig(entity_id="sensor.load_w"),
        _FakeHistory(value_kw=2.5),
    )
    planner = Planner(_config(), FakeReader(states), caller, load_forecaster=forecaster)

    cycle = planner.step(NOW)

    assert cycle.error is None and cycle.result is not None
    assert cycle.result.slots[0].p_buy_kw == pytest.approx(2.5, abs=1e-3)
    assert cycle.applied_setpoint_w == pytest.approx(2500.0, abs=1e-3)


def test_built_in_forecaster_falls_back_when_no_history() -> None:
    # No history at all → forecaster returns 0 days_used, planner falls back
    # to the current-load reading (1 kW). Force-hold-import pins set-point
    # to that import.
    states = _states(buy=[0.10] * 24, sell=[0.05] * 24, load_w=1000.0)
    caller = FakeCaller()

    class _Empty:
        def get_history(self, *_a, **_k):
            return []

    forecaster = LoadForecaster(
        LoadForecasterConfig(entity_id="sensor.load_w"), _Empty(),
    )
    planner = Planner(_config(), FakeReader(states), caller, load_forecaster=forecaster)

    cycle = planner.step(NOW)

    assert cycle.error is None
    assert cycle.result.slots[0].p_buy_kw == pytest.approx(1.0, abs=1e-3)
    assert cycle.applied_setpoint_w == pytest.approx(1000.0, abs=1e-3)



# ---------------------------------------------------------------------------
# Active modes. The setpoint should be a non-zero override only when the LP
# wants to move energy *between the battery and the grid*. Force-discharge
# (battery → grid arbitrage) gets a negative setpoint; force-charge (grid →
# battery, e.g. cheap-hour buying) gets a positive one.
# ---------------------------------------------------------------------------


def _hourly(value_at_noon: float, value_elsewhere: float) -> list[float]:
    """24-hour price array with a distinct value at hour 12 (= NOW's slot)."""
    out = [value_elsewhere] * 24
    out[12] = value_at_noon
    return out


def test_force_discharge_writes_negative_setpoint() -> None:
    # Slot 0 (hour 12) has a profitable sell price; the next 3 hours offer
    # cheap re-charge. The LP should drain the battery in slot 0 and refill
    # over the rest of the horizon — that requires *actively forcing* the
    # export, so the setpoint must go negative.
    # Slot-0 buy is high so the LP can't run a pure import→export arbitrage;
    # the only way to capture the slot-0 sell price is from the battery.
    states = _states(
        load_w=500.0,
        buy=_hourly(value_at_noon=5.0, value_elsewhere=0.01),
        sell=_hourly(value_at_noon=1.0, value_elsewhere=0.0),
    )
    caller = FakeCaller()
    planner = Planner(_config(), FakeReader(states), caller)

    cycle = planner.step(NOW)

    first = cycle.result.slots[0]
    assert first.p_dis_kw > 1e-3 and first.p_sell_kw > 1e-3   # active discharge to grid
    assert first.p_buy_kw == pytest.approx(0.0, abs=1e-3)
    assert cycle.applied_setpoint_w < -100.0                  # negative, well past dead-band
    assert cycle.applied_setpoint_w == pytest.approx(
        (first.p_buy_kw - first.p_sell_kw) * 1000.0, abs=1e-3,
    )
    assert cycle.applied_feedin is True


def test_force_charge_writes_positive_setpoint() -> None:
    # Slot 0 (hour 12) has effectively free buy; the next 3 hours are very
    # expensive. The LP should fill the battery from the grid now and
    # discharge it to cover load later — that requires *actively forcing*
    # extra import beyond the load, so the setpoint must go positive.
    states = _states(
        load_w=500.0,
        buy=_hourly(value_at_noon=0.01, value_elsewhere=5.0),
        sell=[0.0] * 24,                      # no profitable export anywhere
    )
    caller = FakeCaller()
    planner = Planner(_config(), FakeReader(states), caller)

    cycle = planner.step(NOW)

    first = cycle.result.slots[0]
    assert first.p_chg_kw > 1e-3 and first.p_buy_kw > 1e-3    # active charge from grid
    assert first.p_sell_kw == pytest.approx(0.0, abs=1e-3)
    assert cycle.applied_setpoint_w > 100.0                   # positive, well past dead-band
    assert cycle.applied_setpoint_w == pytest.approx(
        first.p_buy_kw * 1000.0, abs=1e-3,                    # p_sell == 0 here
    )
    assert cycle.applied_feedin is False


# ---------------------------------------------------------------------------
# Force-PV-export toggle. With the toggle on, a pure PV-surplus slot (battery
# idle, no LP-driven battery transfer) flips from passive (setpoint = 0,
# inverter prioritises self-consumption -> battery) to active (negative
# setpoint, inverter exports the surplus to the grid). This is only safe
# because slot-0 PV is clamped to ``min(forecast, live trailing avg)`` so
# the LP can't speculate above measured production.
# ---------------------------------------------------------------------------


def test_force_pv_export_off_keeps_passive_setpoint() -> None:
    # 5 kW PV vs 1 kW load surplus; toggle absent -> passive (setpoint = 0),
    # matching the existing self-consumption behaviour.
    pv_forecast = {(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 5000.0
                   for h in range(4)}
    states = _states(pv_forecast=pv_forecast)
    cycle = Planner(_config(), FakeReader(states), FakeCaller()).step(NOW)

    first = cycle.result.slots[0]
    assert first.p_sell_kw > 1e-3 and first.p_chg_kw < 1e-3
    assert cycle.applied_setpoint_w == pytest.approx(0.0, abs=1e-3)
    assert cycle.force_pv_export_enabled is False


def test_force_pv_export_on_writes_negative_setpoint() -> None:
    # Same surplus scenario, toggle on -> active export. Setpoint mirrors the
    # LP's net grid flow (p_buy - p_sell) so the inverter pushes PV to the
    # grid instead of charging the battery.
    pv_forecast = {(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 5000.0
                   for h in range(4)}
    states = _states(pv_forecast=pv_forecast)
    states["input_boolean.force_export"] = StateView(state="on", attributes={})
    cfg = _config(force_pv_export_entity="input_boolean.force_export")
    cycle = Planner(cfg, FakeReader(states), FakeCaller()).step(NOW)

    first = cycle.result.slots[0]
    assert first.p_sell_kw > 1e-3 and first.p_chg_kw < 1e-3 and first.p_dis_kw < 1e-3
    assert cycle.applied_setpoint_w == pytest.approx(
        (first.p_buy_kw - first.p_sell_kw) * 1000.0, abs=1e-3,
    )
    assert cycle.applied_setpoint_w < -100.0
    assert cycle.force_pv_export_enabled is True


def test_force_pv_export_on_inactive_when_lp_doesnt_export() -> None:
    # Toggle on but the LP has no surplus to sell (no PV, 1 kW load). The
    # force-export branch must remain dormant -- the LP plans pure import
    # for the load instead, which fires force-hold-import (PRD §8.6).
    states = _states()  # default: 0 PV, 1 kW load
    states["input_boolean.force_export"] = StateView(state="on", attributes={})
    cfg = _config(force_pv_export_entity="input_boolean.force_export")
    cycle = Planner(cfg, FakeReader(states), FakeCaller()).step(NOW)

    first = cycle.result.slots[0]
    assert first.p_sell_kw == pytest.approx(0.0, abs=1e-3)
    assert first.p_buy_kw == pytest.approx(1.0, abs=1e-3)
    assert cycle.applied_setpoint_w == pytest.approx(1000.0, abs=1e-3)
    assert cycle.force_pv_export_enabled is True  # toggle read, branch idle


# ---------------------------------------------------------------------------
# Slot-0 PV refinement. The planner clamps the slot-0 forecast against a
# trailing measured average so the force-export branch can't speculate above
# real-time production. The clamp is one-sided: live > forecast must be
# ignored, leaving the upstream forecaster's view intact.
# ---------------------------------------------------------------------------


def test_live_pv_average_caps_slot0_when_below_forecast() -> None:
    # Forecast: 5 kW PV across the horizon. Live trailing average reports
    # 1 kW (clouded over). Slot 0's PV must drop to 1 kW so the active
    # export branch only commits to what's actually being produced; later
    # slots stay at the forecast.
    pv_forecast = {(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 5000.0
                   for h in range(4)}
    states = _states(pv_forecast=pv_forecast)
    states["input_boolean.force_export"] = StateView(state="on", attributes={})
    cfg = _config(force_pv_export_entity="input_boolean.force_export")
    averager = FakeLiveAverager(value=1.0)
    cycle = Planner(cfg, FakeReader(states), FakeCaller(),
                    live_averager=averager).step(NOW)

    slots = cycle.result.slots
    # Slot 0: 1 kW PV - 1 kW load = nothing left to sell; setpoint stays at 0
    # because the force-export branch only fires when p_sell > 0.
    assert slots[0].p_sell_kw == pytest.approx(0.0, abs=1e-3)
    assert cycle.applied_setpoint_w == pytest.approx(0.0, abs=1e-3)
    # Subsequent slots still see the forecast and export as before.
    assert slots[1].p_sell_kw > 1e-3
    # The averager was queried for the configured PV power entity over a
    # window matching update_seconds.
    assert averager.calls and averager.calls[0][0] == "sensor.pv_w"
    window = averager.calls[0][2] - averager.calls[0][1]
    assert window == timedelta(seconds=cfg.update_seconds)


def test_live_pv_average_above_forecast_keeps_forecast() -> None:
    # Live PV briefly spikes above the forecast (e.g., burst of clear sky).
    # The clamp is one-sided -- slot 0 must stay at the forecast so the LP
    # never speculates beyond the upstream forecaster's slot-average view.
    pv_forecast = {(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 5000.0
                   for h in range(4)}
    states = _states(pv_forecast=pv_forecast)
    cycle = Planner(_config(), FakeReader(states), FakeCaller(),
                    live_averager=FakeLiveAverager(value=8.0)).step(NOW)

    first = cycle.result.slots[0]
    # Slot-0 export equals the forecast surplus (5 - 1 = 4 kW), not 7 kW.
    assert first.p_sell_kw == pytest.approx(4.0, abs=1e-3)


def test_live_pv_average_none_keeps_forecast() -> None:
    # No history yet (averager returns None) -- the planner must fall back
    # to the unmodified forecast rather than zeroing PV out.
    pv_forecast = {(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 5000.0
                   for h in range(4)}
    states = _states(pv_forecast=pv_forecast)
    cycle = Planner(_config(), FakeReader(states), FakeCaller(),
                    live_averager=FakeLiveAverager(value=None)).step(NOW)

    assert cycle.result.slots[0].p_sell_kw == pytest.approx(4.0, abs=1e-3)


# ---------------------------------------------------------------------------
# Solver-failure surfacing. A crash inside the LP solver (e.g. PuLP raising
# PulpError, or CBC exec'ing into FileNotFoundError) must be caught and
# recorded as ``cycle.error`` so the coordinator stays green and the user
# sees the problem on the diagnostic sensor instead of in the HA log as an
# unhandled exception every 5 minutes.
# ---------------------------------------------------------------------------


def test_planner_surfaces_solver_oserror_as_cycle_error(monkeypatch) -> None:
    import pulp

    from custom_components.pv_optimizer import optimizer as optimizer_mod

    class _BoomSolver:
        def actualSolve(self, *_a, **_kw):
            raise FileNotFoundError(2, "No such file or directory", "/missing/cbc")

    optimizer_mod._SOLVER_FACTORY = None
    monkeypatch.setattr(optimizer_mod, "_make_solver", lambda: _BoomSolver())

    states = _states()
    caller = FakeCaller()
    planner = Planner(_config(), FakeReader(states), caller)

    cycle = planner.step(NOW)

    assert cycle.result is None
    assert cycle.error is not None
    assert "subprocess failed" in cycle.error
    assert caller.calls == []  # no setpoint write on failure


def test_planner_surfaces_solver_pulperror_as_cycle_error(monkeypatch) -> None:
    import pulp

    from custom_components.pv_optimizer import optimizer as optimizer_mod

    class _BoomSolver:
        def actualSolve(self, *_a, **_kw):
            raise pulp.PulpError("synthetic")

    optimizer_mod._SOLVER_FACTORY = None
    monkeypatch.setattr(optimizer_mod, "_make_solver", lambda: _BoomSolver())

    states = _states()
    caller = FakeCaller()
    planner = Planner(_config(), FakeReader(states), caller)

    cycle = planner.step(NOW)

    assert cycle.result is None
    assert cycle.error is not None and "LP solver failed" in cycle.error



# ---------------------------------------------------------------------------
# Timestamp serialisation. Slot/forecast keys are naive-UTC internally;
# ``naive_utc_to_iso`` must emit explicit ``+00:00`` so apexcharts-card and
# similar frontends position the series at the correct local-time x-coord.
# ---------------------------------------------------------------------------


def test_naive_utc_to_iso_tags_naive_input_with_utc_offset() -> None:
    iso = naive_utc_to_iso(datetime(2026, 5, 2, 15, 0))
    assert iso == "2026-05-02T15:00:00+00:00"


def test_naive_utc_to_iso_normalises_aware_input_to_utc() -> None:
    prague = timezone(timedelta(hours=2))
    iso = naive_utc_to_iso(datetime(2026, 5, 2, 17, 0, tzinfo=prague))
    assert iso == "2026-05-02T15:00:00+00:00"



# ---------------------------------------------------------------------------
# Minimum sell price floor. Slots whose sell price falls below the configured
# threshold get ``feedin_allowed=False``, which the optimizer enforces as
# ``p_sell[t] = 0``. Default 0.0 is a no-op (any non-negative sell price
# clears the floor) so this is opt-in and backward compatible.
# ---------------------------------------------------------------------------


def test_min_sell_price_default_zero_preserves_passive_export() -> None:
    # Regression guard: with the default floor (0.0), the existing passive-
    # surplus behaviour from test_pv_surplus_triggers_feedin_on_... must
    # still hold. Sell price 0.10 > 0.0, so no slot is gated off.
    pv_forecast = {(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 5000.0
                   for h in range(4)}
    states = _states(pv_forecast=pv_forecast)
    cycle = Planner(_config(), FakeReader(states), FakeCaller()).step(NOW)

    first = cycle.result.slots[0]
    assert first.p_sell_kw > 1e-3
    assert cycle.applied_feedin is True


def test_min_sell_price_above_all_prices_disables_export() -> None:
    # Floor above the constant 0.10 sell price -> every slot has
    # feedin_allowed=False -> p_sell pinned to 0 across the horizon, slot-0
    # setpoint stays at 0, and the feed-in switch is turned off.
    pv_forecast = {(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 5000.0
                   for h in range(4)}
    states = _states(pv_forecast=pv_forecast)
    cfg = _config(min_sell_price_per_kwh=0.20)
    caller = FakeCaller()
    cycle = Planner(cfg, FakeReader(states), caller).step(NOW)

    assert all(s.p_sell_kw == pytest.approx(0.0, abs=1e-3) for s in cycle.result.slots)
    assert cycle.applied_setpoint_w == pytest.approx(0.0, abs=1e-3)
    assert cycle.applied_feedin is False
    assert ("switch", "turn_off") in [(d, s) for d, s, _ in caller.calls]


def test_min_sell_price_partial_horizon_gates_only_cheap_slots() -> None:
    # Two-tier sell price: first two slots at 0.05 (below floor), last two at
    # 0.30 (above). With floor=0.10 the LP must keep PV in the battery during
    # the cheap morning slots and export it in the expensive afternoon ones.
    pv_forecast = {(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): 5000.0
                   for h in range(4)}
    # NOW is hour 12 and the horizon is 4 slots, so indices 12..15 of the
    # 24-hour sell array drive the LP. Cheap-then-expensive within the
    # horizon makes the gate observable.
    sell = [0.30] * 12 + [0.05, 0.05, 0.30, 0.30] + [0.30] * 8
    states = _states(pv_forecast=pv_forecast, sell=sell)
    cfg = _config(min_sell_price_per_kwh=0.10)
    cycle = Planner(cfg, FakeReader(states), FakeCaller()).step(NOW)

    slots = cycle.result.slots
    assert slots[0].p_sell_kw == pytest.approx(0.0, abs=1e-3)
    assert slots[1].p_sell_kw == pytest.approx(0.0, abs=1e-3)
    # At least one of the eligible later slots must export something.
    assert slots[2].p_sell_kw + slots[3].p_sell_kw > 1e-3
    # Slot 0 is gated -> feed-in switch off this cycle.
    assert cycle.applied_feedin is False


# ---------------------------------------------------------------------------
# Force-hold-import (PRD §8.6). When the LP plans pure grid coverage of the
# load with the battery idle (``p_buy > 0`` ∧ ``p_chg = p_dis = 0``), the
# planner must pin the grid set-point to the planned import. Otherwise the
# inverter's native EMS would silently drain the battery to cover the load —
# violating the LP's intent (typically driven by the §8.5 health-floor
# penalty making further discharge expensive, or by ``soc_min`` sitting
# above the inverter's BMS floor).
# ---------------------------------------------------------------------------


def test_force_hold_import_writes_positive_setpoint_matching_load() -> None:
    # Pure nighttime load (no PV, 1.5 kW load), nothing to arbitrage. The LP
    # plans grid coverage with the battery idle; force-hold-import pins the
    # set-point to ``p_buy * 1000`` so the EMS doesn't drain the battery.
    states = _states(load_w=1500.0)
    caller = FakeCaller()
    cycle = Planner(_config(), FakeReader(states), caller).step(NOW)

    first = cycle.result.slots[0]
    assert first.p_buy_kw == pytest.approx(1.5, abs=1e-3)
    assert first.p_chg_kw == pytest.approx(0.0, abs=1e-3)
    assert first.p_dis_kw == pytest.approx(0.0, abs=1e-3)
    assert first.p_sell_kw == pytest.approx(0.0, abs=1e-3)
    assert cycle.applied_setpoint_w == pytest.approx(1500.0, abs=1e-3)
    assert cycle.applied_setpoint_w == pytest.approx(
        (first.p_buy_kw - first.p_sell_kw) * 1000.0, abs=1e-3,
    )
    assert cycle.applied_feedin is False


def test_force_hold_import_dormant_when_lp_discharges_battery() -> None:
    # Slot 0 is expensive to buy from (0.50), the rest of the horizon is
    # cheap (0.05). The LP discharges the battery now and recharges later
    # (sell=0 throughout, so no export confounds the picture). Slot 0 has
    # p_dis > 0 ∧ p_buy = 0 -> force-hold-import does NOT fire and the
    # set-point stays passive (0); the inverter's self-consumption mode
    # handles the discharge naturally.
    states = _states(
        load_w=1000.0,
        buy=_hourly(value_at_noon=0.50, value_elsewhere=0.05),
        sell=[0.0] * 24,
    )
    cycle = Planner(_config(), FakeReader(states), FakeCaller()).step(NOW)

    first = cycle.result.slots[0]
    assert first.p_buy_kw == pytest.approx(0.0, abs=1e-3)
    assert first.p_dis_kw > 1e-3                               # battery covers load
    assert cycle.applied_setpoint_w == pytest.approx(0.0, abs=1e-3)


def test_planner_config_ev_optional_when_unset() -> None:
    """Existing planner construction with no EV inputs keeps working."""
    cfg = _config()
    assert cfg.ev is None


def test_planner_config_with_ev_config() -> None:
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        session_energy_entity="sensor.ev_session_energy",
        start_switch_entity="switch.ev_start",
        charger_mode_entity="select.ev_mode",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        target_pct_entity="number.pv_optimizer_ev_target_pct",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
    )
    cfg = _config(ev=ev_cfg)
    assert cfg.ev is ev_cfg


def test_planner_reads_ev_state_no_target_runs_reactive() -> None:
    """When target=0, planner uses the reactive path with surplus tracking.

    With no surplus and an above-threshold price, the reactive path returns 0 A.
    """
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_params = EVParams(
        max_charging_power_kw=8.0, max_charging_current_a=20.0,
        min_charging_current_a=6.0, car_battery_kwh=60.0,
    )
    ev_cfg = EVConfig(
        params=ev_params,
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
    )
    states = _states()
    states["sensor.ev_state"] = StateView(state="Charging")  # requesting
    states["sensor.ev_power"] = StateView(state="0")
    states["number.ev_max_current"] = StateView(state="0")
    states["select.pv_optimizer_ev_mode"] = StateView(state="auto")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    states["datetime.pv_optimizer_ev_deadline"] = StateView(state="")
    reader, caller = FakeReader(states), FakeCaller()
    cfg = _config(ev=ev_cfg)
    p = Planner(cfg, reader, caller)
    p.step(NOW)
    # Reactive path: grid=0, ev_power=0 -> no surplus -> max_current = 0.
    ev_writes = [c for c in caller.calls if c[2].get("entity_id") == "number.ev_max_current"]
    assert ev_writes, "planner should write to ev_max_current"
    last = ev_writes[-1]
    assert last == ("number", "set_value", {"entity_id": "number.ev_max_current", "value": 0})


def test_planner_off_mode_writes_nothing_to_ev() -> None:
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
    )
    states = _states()
    states["sensor.ev_state"] = StateView(state="Charging")
    states["sensor.ev_power"] = StateView(state="0")
    states["number.ev_max_current"] = StateView(state="0")
    states["select.pv_optimizer_ev_mode"] = StateView(state="off")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    states["datetime.pv_optimizer_ev_deadline"] = StateView(state="")
    reader, caller = FakeReader(states), FakeCaller()
    p = Planner(_config(ev=ev_cfg), reader, caller)
    p.step(NOW)
    ev_writes = [c for c in caller.calls if c[2].get("entity_id") == "number.ev_max_current"]
    assert ev_writes == [], "off mode must not write to ev_max_current"


def test_physical_soc_projection_stays_flat_under_force_hold_import() -> None:
    # Same scenario as the basic force-hold-import test: pure-load coverage
    # from the grid with battery idle. The physical-SoC projection must
    # mirror the LP (battery flat) instead of running the passive-deficit
    # branch that would project a discharge equal to load × dt.
    #
    # Non-zero cycle_cost makes the all-slots-idle answer strictly cheaper
    # than gratuitous round-trips (LP would otherwise be indifferent under
    # 100% efficiency + zero cycle cost).
    cfg = _config()
    cfg = replace(cfg, battery=replace(cfg.battery, cycle_cost_per_kwh=0.01))
    states = _states(soc_pct=50.0, load_w=1500.0)  # 50% of 10 kWh -> 5.0 kWh
    cycle = Planner(cfg, FakeReader(states), FakeCaller()).step(NOW)

    slots = cycle.result.slots
    # All slots are pure-load coverage -> all force-hold-import.
    for s in slots:
        assert s.p_buy_kw == pytest.approx(1.5, abs=1e-3)
        assert s.p_chg_kw == pytest.approx(0.0, abs=1e-3)
        assert s.p_dis_kw == pytest.approx(0.0, abs=1e-3)
    # Projection stays flat across the whole horizon (no physical drain).
    for s in slots:
        assert s.soc_physical_kwh == pytest.approx(5.0, abs=1e-3)


def test_planner_car_mode_writes_max() -> None:
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        start_switch_entity="switch.ev_start",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
    )
    states = _states()
    states["sensor.ev_state"] = StateView(state="Connected")  # idle
    states["sensor.ev_power"] = StateView(state="0")
    states["number.ev_max_current"] = StateView(state="0")
    states["switch.ev_start"] = StateView(state="off")
    states["select.pv_optimizer_ev_mode"] = StateView(state="car")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    p.step(NOW)
    writes = [c for c in p.caller.calls
              if c[2].get("entity_id") == "number.ev_max_current"]
    assert writes, "car mode should write max-current"
    assert writes[-1][2]["value"] == 20
    starts = [c for c in p.caller.calls
              if c[2].get("entity_id") == "switch.ev_start"]
    assert starts and starts[-1][1] == "turn_on"


def test_planner_car_mode_writes_start_and_charger_mode_every_tick() -> None:
    """The start-switch and charger-mode writes are unconditional so the
    planner self-corrects against firmware resets (EVCS clears the charging
    switch on Auto→Manual on some builds) and external user toggles. The
    current write keeps its dedupe — number entities are noisier in the
    recorder and we have explicit mode-flip invalidation instead.
    """
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        start_switch_entity="switch.ev_start",
        charger_mode_entity="select.ev_mode",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
    )
    states = _states()
    states["sensor.ev_state"] = StateView(state="Connected")
    states["sensor.ev_power"] = StateView(state="0")
    states["number.ev_max_current"] = StateView(state="0")
    states["switch.ev_start"] = StateView(state="off")
    states["select.ev_mode"] = StateView(state="Auto")
    states["select.pv_optimizer_ev_mode"] = StateView(state="car")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    for i in range(3):
        p.step(NOW + timedelta(seconds=300 * i))
    starts = [c for c in p.caller.calls
              if c[2].get("entity_id") == "switch.ev_start"]
    mode_writes = [c for c in p.caller.calls
                   if c[2].get("entity_id") == "select.ev_mode"]
    current_writes = [c for c in p.caller.calls
                      if c[2].get("entity_id") == "number.ev_max_current"]
    assert len(starts) == 3, "start switch must be written every tick"
    assert all(s[1] == "turn_on" for s in starts)
    assert len(mode_writes) == 3, "charger mode must be written every tick"
    assert all(m[2]["option"] == "Manual" for m in mode_writes)
    # Current stays deduped (target unchanged, role unchanged).
    assert len(current_writes) == 1, "current write keeps idempotent dedupe"


def test_planner_engages_lp_when_target_and_deadline_set() -> None:
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
    )
    # Cheap slot 0 (0.05), expensive others (0.30); 4-h horizon.
    states = _states(buy=[0.05] + [0.30] * 23)
    states["sensor.ev_state"] = StateView(state="Connected")  # idle (not requesting)
    states["sensor.ev_power"] = StateView(state="0")
    states["number.ev_max_current"] = StateView(state="0")
    states["select.pv_optimizer_ev_mode"] = StateView(state="auto")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="5")
    # Deadline 3h from NOW; planner stores it as exclusive index.
    deadline = (NOW + timedelta(hours=3)).isoformat() + "+00:00"
    states["datetime.pv_optimizer_ev_deadline"] = StateView(state=deadline)
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    cycle = p.step(NOW)
    # LP should plan EV charging in slot 0 (cheapest).
    assert cycle.result is not None
    assert cycle.result.slots[0].p_ev_chg_kw > 0
    # And the planner should write a non-zero max-current.
    writes = [c for c in p.caller.calls
              if c[2].get("entity_id") == "number.ev_max_current"]
    assert writes and writes[-1][2]["value"] >= 6


def test_planner_planned_start_pre_schedules_window_in_auto() -> None:
    """When ``planned_start`` is in the future, the LP reserves a charging
    window from that slot onward (even with the car currently disconnected),
    while ``_apply_ev`` parks the charger idle in passive ("Auto") mode with
    the start switch OFF. The planner drives no charging early — not even from
    free PV surplus — since a future start may be deliberate (cheap/negative
    prices later). Charging is the LP's job from planned_start on.
    """
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
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
    # Slot 0 is cheapest but must NOT be used (before planned_start).
    # planned_start at +2h => slot index 2 (slot_minutes=60, NOW=noon).
    # The exact in-window slot the LP picks is left to the optimiser: when
    # the sell price sits above the buy price the battery competes with the
    # EV for the import/export slots (a slot can do one or the other, not
    # both), so the cost-optimal charge slot is not simply "cheapest buy".
    # The contract under test is the gate, not the slot.
    buy = [0.05] * 24
    buy[2] = 0.06  # only-slightly-more-expensive in-window
    buy[3] = 0.30  # discourage spill
    states = _states(buy=buy)
    states["sensor.ev_state"] = StateView(state="Disconnected")
    states["sensor.ev_power"] = StateView(state="0")
    states["number.ev_max_current"] = StateView(state="0")
    states["switch.ev_start"] = StateView(state="off")
    states["select.ev_mode"] = StateView(state="Auto")
    states["select.pv_optimizer_ev_mode"] = StateView(state="auto")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="5")
    deadline = (NOW + timedelta(hours=4)).isoformat() + "+00:00"
    states["datetime.pv_optimizer_ev_deadline"] = StateView(state=deadline)
    planned_start = (NOW + timedelta(hours=2)).isoformat() + "+00:00"
    states["datetime.pv_optimizer_ev_planned_start"] = StateView(
        state=planned_start)
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    cycle = p.step(NOW)
    assert cycle.result is not None
    slots = cycle.result.slots
    # Gate: no EV charging before planned_start (slots 0 and 1).
    assert slots[0].p_ev_chg_kw == 0
    assert slots[1].p_ev_chg_kw == 0
    # The gate does not strand the schedule: the full 5 kWh target is met
    # from planned_start (slot 2) onward, within the deadline window.
    in_window = sum(sp.p_ev_chg_kw * sp.duration_h for sp in slots[2:])
    assert in_window == pytest.approx(5.0, abs=1e-3)
    # And no slot fabricates a grid round-trip (import and export at once).
    for sp in slots:
        assert not (sp.p_buy_kw > 1e-6 and sp.p_sell_kw > 1e-6)
    # Hardware: charger parked idle in passive "Auto" mode during the gate —
    # start switch OFF (no surplus charging), and no current write at all.
    mode_writes = [c for c in p.caller.calls
                   if c[2].get("entity_id") == "select.ev_mode"]
    current_writes = [c for c in p.caller.calls
                      if c[2].get("entity_id") == "number.ev_max_current"]
    starts = [c for c in p.caller.calls
              if c[2].get("entity_id") == "switch.ev_start"]
    assert mode_writes and mode_writes[-1][2]["option"] == "Auto"
    assert all(c[2]["option"] == "Auto" for c in mode_writes)
    assert current_writes == []
    assert starts and all(c[1] == "turn_off" for c in starts)


def test_planner_planned_start_parks_connected_car_idle() -> None:
    """With the car physically connected and idle while ``planned_start`` is
    still in the future, the gate must put the EVCS in passive ("Auto") mode
    with the start switch OFF so it does not charge — not even from free PV
    surplus. The planner must NOT engage any charging before the time.
    """
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
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
    states = _states()
    states["sensor.ev_state"] = StateView(state="Connected")
    states["sensor.ev_power"] = StateView(state="0")
    states["number.ev_max_current"] = StateView(state="0")
    states["switch.ev_start"] = StateView(state="off")
    states["select.ev_mode"] = StateView(state="Manual")
    states["select.pv_optimizer_ev_mode"] = StateView(state="auto")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="5")
    deadline = (NOW + timedelta(hours=8)).isoformat() + "+00:00"
    states["datetime.pv_optimizer_ev_deadline"] = StateView(state=deadline)
    planned_start = (NOW + timedelta(hours=3)).isoformat() + "+00:00"
    states["datetime.pv_optimizer_ev_planned_start"] = StateView(
        state=planned_start)
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    p.step(NOW)
    mode_writes = [c for c in p.caller.calls
                   if c[2].get("entity_id") == "select.ev_mode"]
    current_writes = [c for c in p.caller.calls
                      if c[2].get("entity_id") == "number.ev_max_current"]
    starts = [c for c in p.caller.calls
              if c[2].get("entity_id") == "switch.ev_start"]
    assert mode_writes and mode_writes[-1][2]["option"] == "Auto"
    assert all(c[2]["option"] == "Auto" for c in mode_writes)
    assert current_writes == []
    assert starts and all(c[1] == "turn_off" for c in starts)


def test_planner_planned_start_in_past_does_not_gate() -> None:
    """A ``planned_start`` whose time has already rolled past must behave
    exactly as if it were unset — otherwise the gate would silently strand
    the schedule after the user's planned arrival.
    """
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
        planned_start_entity="datetime.pv_optimizer_ev_planned_start",
    )
    states = _states(buy=[0.05] + [0.30] * 23)
    states["sensor.ev_state"] = StateView(state="Connected")
    states["sensor.ev_power"] = StateView(state="0")
    states["number.ev_max_current"] = StateView(state="0")
    states["select.pv_optimizer_ev_mode"] = StateView(state="auto")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="5")
    deadline = (NOW + timedelta(hours=3)).isoformat() + "+00:00"
    states["datetime.pv_optimizer_ev_deadline"] = StateView(state=deadline)
    planned_start = (NOW - timedelta(hours=1)).isoformat() + "+00:00"
    states["datetime.pv_optimizer_ev_planned_start"] = StateView(
        state=planned_start)
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    cycle = p.step(NOW)
    assert cycle.result is not None
    assert cycle.result.slots[0].p_ev_chg_kw > 0
    writes = [c for c in p.caller.calls
              if c[2].get("entity_id") == "number.ev_max_current"]
    assert writes and writes[-1][2]["value"] >= 6


def test_planner_planned_start_does_not_gate_car_mode() -> None:
    """Car mode is the user explicitly demanding "charge now" — the
    schedule must not silently block them. The LP still ignores the car
    (because LP-side gating is shared with auto), but ``_apply_ev`` still
    writes the charger mode/current/start to the EVCS.
    """
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
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
    states = _states()
    states["sensor.ev_state"] = StateView(state="Connected")
    states["sensor.ev_power"] = StateView(state="0")
    states["number.ev_max_current"] = StateView(state="0")
    states["switch.ev_start"] = StateView(state="off")
    states["select.ev_mode"] = StateView(state="Auto")
    states["select.pv_optimizer_ev_mode"] = StateView(state="car")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    planned_start = (NOW + timedelta(hours=5)).isoformat() + "+00:00"
    states["datetime.pv_optimizer_ev_planned_start"] = StateView(
        state=planned_start)
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    p.step(NOW)
    starts = [c for c in p.caller.calls
              if c[2].get("entity_id") == "switch.ev_start"]
    mode_writes = [c for c in p.caller.calls
                   if c[2].get("entity_id") == "select.ev_mode"]
    current_writes = [c for c in p.caller.calls
                      if c[2].get("entity_id") == "number.ev_max_current"]
    assert starts and starts[-1][1] == "turn_on"
    assert mode_writes and mode_writes[-1][2]["option"] == "Manual"
    assert current_writes and current_writes[-1][2]["value"] >= 6


def test_planner_car_mode_auto_returns_on_disconnect() -> None:
    """With the auto-return switch ON, 'car' mode writes 'auto' back to the
    mode select immediately when the car disconnects.
    """
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
        car_auto_return_entity="switch.pv_optimizer_ev_car_auto_return",
    )
    states = _states()
    states["sensor.ev_state"] = StateView(state="Disconnected")
    states["sensor.ev_power"] = StateView(state="0")
    states["number.ev_max_current"] = StateView(state="0")
    states["select.pv_optimizer_ev_mode"] = StateView(state="car")
    states["switch.pv_optimizer_ev_car_auto_return"] = StateView(state="on")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    p.step(NOW)
    mode_writes = [c for c in p.caller.calls
                   if c[2].get("entity_id") == "select.pv_optimizer_ev_mode"]
    assert mode_writes, "car mode should auto-return on disconnect when switch is ON"
    assert mode_writes[-1][2]["option"] == "auto"


def test_planner_car_mode_holds_through_low_soc_pause() -> None:
    """With the auto-return switch ON, 'car' mode holds through EVCS gating
    (e.g. low_soc) when the car has never drawn power during this session —
    the idle+low-power dwell guard prevents a premature return to 'auto'.
    """
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        start_switch_entity="switch.ev_start",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
        car_auto_return_entity="switch.pv_optimizer_ev_car_auto_return",
    )
    states = _states()
    states["sensor.ev_state"] = StateView(state="low_soc")  # IDLE per vocab
    states["sensor.ev_power"] = StateView(state="0")
    states["number.ev_max_current"] = StateView(state="0")
    states["switch.ev_start"] = StateView(state="off")
    states["select.pv_optimizer_ev_mode"] = StateView(state="car")
    states["switch.pv_optimizer_ev_car_auto_return"] = StateView(state="on")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    # Step several ticks well past the session_done_seconds dwell (60s).
    p.step(NOW)
    p.step(NOW + timedelta(seconds=90))
    p.step(NOW + timedelta(seconds=180))
    mode_writes = [c for c in p.caller.calls
                   if c[2].get("entity_id") == "select.pv_optimizer_ev_mode"]
    assert not mode_writes, (
        "car mode holds through EVCS low_soc gating when car has never charged"
    )
    current_writes = [c for c in p.caller.calls
                      if c[2].get("entity_id") == "number.ev_max_current"]
    assert current_writes and current_writes[-1][2]["value"] == 20


def test_planner_car_mode_auto_exits_after_real_session() -> None:
    """With the auto-return switch ON, 'car' mode returns to 'auto' once
    the car has actually charged and then stopped drawing for the
    session-done dwell — that constitutes a finished session.
    """
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        start_switch_entity="switch.ev_start",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
        car_auto_return_entity="switch.pv_optimizer_ev_car_auto_return",
    )
    states = _states()
    states["sensor.ev_state"] = StateView(state="Charging")
    states["sensor.ev_power"] = StateView(state="6000")  # > session_done_power_w
    states["number.ev_max_current"] = StateView(state="0")
    states["switch.ev_start"] = StateView(state="off")
    states["select.pv_optimizer_ev_mode"] = StateView(state="car")
    states["switch.pv_optimizer_ev_car_auto_return"] = StateView(state="on")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    # Tick 1: car is charging — flag flips to True.
    p.step(NOW)
    # Now car finishes — state goes to IDLE-class ("Charged"), power drops.
    states["sensor.ev_state"] = StateView(state="Charged")
    states["sensor.ev_power"] = StateView(state="0")
    # Tick 2: low-power dwell starts.
    p.step(NOW + timedelta(seconds=30))
    # Tick 3: dwell exceeds session_done_seconds (60).
    p.step(NOW + timedelta(seconds=120))
    mode_writes = [c for c in p.caller.calls
                   if c[2].get("entity_id") == "select.pv_optimizer_ev_mode"]
    assert mode_writes, "car mode auto-returns once session truly done when switch is ON"
    assert mode_writes[-1][2]["option"] == "auto"


def test_planner_car_mode_sticky_on_disconnect_when_auto_return_off() -> None:
    """With the auto-return switch OFF (default), 'car' mode persists
    through a physical disconnect — no mode write back to 'auto'."""
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
        car_auto_return_entity="switch.pv_optimizer_ev_car_auto_return",
    )
    states = _states()
    states["sensor.ev_state"] = StateView(state="Disconnected")
    states["sensor.ev_power"] = StateView(state="0")
    states["number.ev_max_current"] = StateView(state="0")
    states["select.pv_optimizer_ev_mode"] = StateView(state="car")
    states["switch.pv_optimizer_ev_car_auto_return"] = StateView(state="off")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    p.step(NOW)
    mode_writes = [c for c in p.caller.calls
                   if c[2].get("entity_id") == "select.pv_optimizer_ev_mode"]
    assert not mode_writes, "car mode must stay sticky when auto-return switch is OFF"


def test_planner_car_mode_sticky_after_session_when_auto_return_off() -> None:
    """With the auto-return switch OFF, a finished session does not
    bounce 'car' mode back to 'auto'."""
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        start_switch_entity="switch.ev_start",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
        car_auto_return_entity="switch.pv_optimizer_ev_car_auto_return",
    )
    states = _states()
    states["sensor.ev_state"] = StateView(state="Charging")
    states["sensor.ev_power"] = StateView(state="6000")
    states["number.ev_max_current"] = StateView(state="0")
    states["switch.ev_start"] = StateView(state="off")
    states["select.pv_optimizer_ev_mode"] = StateView(state="car")
    states["switch.pv_optimizer_ev_car_auto_return"] = StateView(state="off")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    p.step(NOW)
    states["sensor.ev_state"] = StateView(state="Charged")
    states["sensor.ev_power"] = StateView(state="0")
    p.step(NOW + timedelta(seconds=30))
    p.step(NOW + timedelta(seconds=120))
    mode_writes = [c for c in p.caller.calls
                   if c[2].get("entity_id") == "select.pv_optimizer_ev_mode"]
    assert not mode_writes, "car mode is sticky — finished session must not bounce back to auto"


def test_planner_car_mode_auto_return_toggle_resets_session_seen() -> None:
    """ON→OFF→ON toggle on the auto-return switch resets the
    'charging seen' flag, so a subsequent idle dwell does not trigger
    a stale auto-return."""
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        start_switch_entity="switch.ev_start",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
        car_auto_return_entity="switch.pv_optimizer_ev_car_auto_return",
    )
    states = _states()
    states["sensor.ev_state"] = StateView(state="Charging")
    states["sensor.ev_power"] = StateView(state="6000")
    states["number.ev_max_current"] = StateView(state="0")
    states["switch.ev_start"] = StateView(state="off")
    states["select.pv_optimizer_ev_mode"] = StateView(state="car")
    states["switch.pv_optimizer_ev_car_auto_return"] = StateView(state="on")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())

    # Tick 1: switch ON, car charging — flag flips to True.
    p.step(NOW)
    assert p.ev_state.car_session_charging_seen is True

    # User flips the auto-return switch OFF mid-session. State still
    # 'car' and car still connected. Power has tapered to 0 (perhaps
    # the EVCS is now gating).
    states["switch.pv_optimizer_ev_car_auto_return"] = StateView(state="off")
    states["sensor.ev_state"] = StateView(state="Charged")
    states["sensor.ev_power"] = StateView(state="0")
    p.step(NOW + timedelta(seconds=30))
    assert p.ev_state.car_session_charging_seen is False, (
        "switch flipping OFF must clear the stale charging-seen flag"
    )

    # Re-enable the switch. Without the reset, the stale True flag plus
    # idle+low-power dwell would trip auto-return on the very next tick.
    # With the reset, it must NOT.
    states["switch.pv_optimizer_ev_car_auto_return"] = StateView(state="on")
    p.step(NOW + timedelta(seconds=180))
    mode_writes = [c for c in p.caller.calls
                   if c[2].get("entity_id") == "select.pv_optimizer_ev_mode"]
    assert not mode_writes, (
        "stale charging-seen must not survive a switch toggle and trigger auto-return"
    )


def test_planner_integrates_session_energy_when_no_sensor() -> None:
    """Without ev_session_energy_entity, planner integrates ev_charging_power_entity."""
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
    )
    states = _states()
    states["sensor.ev_state"] = StateView(state="Charging")
    states["sensor.ev_power"] = StateView(state="2000")  # 2 kW
    states["number.ev_max_current"] = StateView(state="0")
    states["select.pv_optimizer_ev_mode"] = StateView(state="auto")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    p.step(NOW)
    # Advance 30 min — integrator should be 2 kW * 0.5 h = 1.0 kWh.
    p.step(NOW + timedelta(minutes=30))
    assert p.ev_state is not None
    assert p.ev_state.session_energy_kwh == pytest.approx(1.0, abs=1e-3)


def test_session_energy_kwh_reads_external_entity_when_configured() -> None:
    """Regression guard: when the user binds ev_session_energy_entity the
    public ``session_energy_kwh()`` must read from it, not from the
    internal integrator field (which is correctly skipped to avoid
    double-counting and therefore stays at 0). The sensor platform
    routes through this method, so a wrong source here surfaces as a
    permanently-zero ``sensor.pv_optimizer_ev_session_energy``.
    """
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        session_energy_entity="sensor.ev_session_energy",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
    )
    states = _states()
    states["sensor.ev_state"] = StateView(state="Charging")
    states["sensor.ev_power"] = StateView(state="2000")
    states["sensor.ev_session_energy"] = StateView(state="3.7")
    states["number.ev_max_current"] = StateView(state="0")
    states["select.pv_optimizer_ev_mode"] = StateView(state="auto")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    # Internal integrator must NOT run (external entity is configured).
    p.step(NOW)
    p.step(NOW + timedelta(minutes=30))
    assert p.ev_state is not None
    assert p.ev_state.session_energy_kwh == pytest.approx(0.0, abs=1e-9)
    # But the public accessor returns the external entity's value.
    assert p.session_energy_kwh() == pytest.approx(3.7, abs=1e-6)


def test_session_energy_kwh_returns_zero_when_disconnected() -> None:
    """Many EVCS firmwares only reset their external session counter on
    plug-in, so while the car is unplugged the entity reports the *last*
    session's total. Without a guard the planner would compute
    ``remaining = target - stale_value`` and skip a freshly scheduled
    session. ``session_energy_kwh()`` must return 0 whenever the charger
    state classifies as DISCONNECTED, regardless of what the entity holds.
    """
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        session_energy_entity="sensor.ev_session_energy",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
        planned_start_entity="datetime.pv_optimizer_ev_planned_start",
    )
    # Cheap slot 0 (0.05), expensive others (0.30); target = 10 kWh,
    # external entity still reports last session's 10 kWh while unplugged.
    states = _states(buy=[0.05] + [0.30] * 23)
    states["sensor.ev_state"] = StateView(state="Disconnected")
    states["sensor.ev_power"] = StateView(state="0")
    states["sensor.ev_session_energy"] = StateView(state="10.0")
    states["number.ev_max_current"] = StateView(state="0")
    states["select.pv_optimizer_ev_mode"] = StateView(state="auto")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="10")
    deadline = (NOW + timedelta(hours=3)).isoformat() + "+00:00"
    states["datetime.pv_optimizer_ev_deadline"] = StateView(state=deadline)
    planned_start = (NOW + timedelta(hours=1)).isoformat() + "+00:00"
    states["datetime.pv_optimizer_ev_planned_start"] = StateView(
        state=planned_start)
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    assert p.session_energy_kwh() == 0.0
    cycle = p.step(NOW)
    # Despite the stale 10 kWh on the entity, LP must plan the full target.
    assert cycle.result is not None
    total_planned = sum(s.p_ev_chg_kw * s.duration_h
                        for s in cycle.result.slots)
    assert total_planned > 0


def test_planner_resets_session_energy_on_plug_in() -> None:
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
    )
    states = _states()
    states["sensor.ev_state"] = StateView(state="Charging")
    states["sensor.ev_power"] = StateView(state="2000")
    states["number.ev_max_current"] = StateView(state="0")
    states["select.pv_optimizer_ev_mode"] = StateView(state="auto")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    p.step(NOW)
    p.step(NOW + timedelta(minutes=30))
    assert p.ev_state.session_energy_kwh > 0
    # Now unplug.
    states["sensor.ev_state"] = StateView(state="Disconnected")
    states["sensor.ev_power"] = StateView(state="0")
    p.step(NOW + timedelta(hours=1))
    # Plug back in.
    states["sensor.ev_state"] = StateView(state="Connected")
    p.step(NOW + timedelta(hours=2))
    # Integrator must have reset on plug-in.
    assert p.ev_state.session_energy_kwh == pytest.approx(0.0, abs=1e-3)


def test_planner_reactive_mode_switch_writes_correct_option_strings() -> None:
    """The mode-switching reactive variant must drive the charger_mode_entity
    with 'Auto' in the passive branch and 'Manual' in the active branch.
    Guards the shared ``_write_ev_charger_mode`` helper against a broken
    role→option mapping.
    """
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0,
            buy_price_threshold=0.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        start_switch_entity="switch.ev_start",
        charger_mode_entity="select.ev_mode",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
    )
    # Tick 1: expensive buy (0.30 > 0.0 threshold) → passive branch → "Auto".
    states = _states()
    states["sensor.ev_state"] = StateView(state="Connected")  # IDLE class
    states["sensor.ev_power"] = StateView(state="0")
    states["number.ev_max_current"] = StateView(state="0")
    states["switch.ev_start"] = StateView(state="off")
    states["select.ev_mode"] = StateView(state="Auto")
    states["select.pv_optimizer_ev_mode"] = StateView(state="auto")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    p.step(NOW)
    mode_writes = [c for c in p.caller.calls
                   if c[2].get("entity_id") == "select.ev_mode"]
    assert mode_writes and mode_writes[-1][2]["option"] == "Auto"
    # Passive branch must still arm the start switch ON — the EVCS can only
    # surplus-charge with start on, so a stale OFF (e.g. from the planned-start
    # gate) must not be left in place.
    starts = [c for c in p.caller.calls
              if c[2].get("entity_id") == "switch.ev_start"]
    assert starts and starts[-1][1] == "turn_on"
    # Tick 2: cheap buy (0.0 ≤ 0.0) → cheap_grid_active → active branch → "Manual".
    states["sensor.buy"] = StateView(state="0.00",
                                     attributes={"today": [0.0] * 24})
    p.step(NOW + timedelta(seconds=300))
    mode_writes = [c for c in p.caller.calls
                   if c[2].get("entity_id") == "select.ev_mode"]
    assert mode_writes[-1][2]["option"] == "Manual"


def test_planner_reactive_mode_uses_custom_option_strings() -> None:
    """Non-EVCS chargers (Zappi: 'Eco+'/'Stopped') must work — the option
    text is sourced from EVConfig, not hardcoded.
    """
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0,
            buy_price_threshold=0.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        start_switch_entity="switch.ev_start",
        charger_mode_entity="select.ev_mode",
        charger_mode_option_active="Eco+",
        charger_mode_option_passive="Stopped",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
    )
    states = _states()
    states["sensor.ev_state"] = StateView(state="Connected")
    states["sensor.ev_power"] = StateView(state="0")
    states["number.ev_max_current"] = StateView(state="0")
    states["switch.ev_start"] = StateView(state="off")
    states["select.ev_mode"] = StateView(state="Stopped")
    states["select.pv_optimizer_ev_mode"] = StateView(state="auto")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    # Passive branch — expensive price.
    p.step(NOW)
    mode_writes = [c for c in p.caller.calls
                   if c[2].get("entity_id") == "select.ev_mode"]
    assert mode_writes and mode_writes[-1][2]["option"] == "Stopped"
    # Active branch — cheap price flips the latch.
    states["sensor.buy"] = StateView(state="0.00",
                                     attributes={"today": [0.0] * 24})
    p.step(NOW + timedelta(seconds=300))
    mode_writes = [c for c in p.caller.calls
                   if c[2].get("entity_id") == "select.ev_mode"]
    assert mode_writes[-1][2]["option"] == "Eco+"


def test_planner_reactive_mode_transition_re_asserts_current() -> None:
    """On a passive→active mode flip the planner must re-assert max-current
    even if the target value is unchanged: some charger firmwares reset the
    internal current register on a mode transition, so the idempotent write
    cache (``last_written_current_a``) is invalidated when mode changes.
    """
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0,
            buy_price_threshold=0.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        start_switch_entity="switch.ev_start",
        charger_mode_entity="select.ev_mode",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
    )
    states = _states()
    states["sensor.ev_state"] = StateView(state="Connected")  # IDLE class
    states["sensor.ev_power"] = StateView(state="0")
    states["number.ev_max_current"] = StateView(state="0")
    states["switch.ev_start"] = StateView(state="off")
    states["select.ev_mode"] = StateView(state="Auto")
    states["select.pv_optimizer_ev_mode"] = StateView(state="auto")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    p = Planner(_config(ev=ev_cfg), FakeReader(states), FakeCaller())
    # Tick 1: passive → writes max-current (20 A), caches it.
    p.step(NOW)
    current_writes = [c for c in p.caller.calls
                      if c[2].get("entity_id") == "number.ev_max_current"]
    assert len(current_writes) == 1
    assert current_writes[-1][2]["value"] == 20
    # Tick 2: same target (20 A) — dedupe cache suppresses the write.
    p.step(NOW + timedelta(seconds=300))
    current_writes = [c for c in p.caller.calls
                      if c[2].get("entity_id") == "number.ev_max_current"]
    assert len(current_writes) == 1, "duplicate passive write must be deduped"
    # Tick 3: cheap buy → active branch. Mode flips passive→active, which
    # must invalidate ``last_written_current_a`` so the current write fires
    # through even though the target is still 20 A.
    states["sensor.buy"] = StateView(state="0.00",
                                     attributes={"today": [0.0] * 24})
    p.step(NOW + timedelta(seconds=600))
    current_writes = [c for c in p.caller.calls
                      if c[2].get("entity_id") == "number.ev_max_current"]
    assert len(current_writes) == 2, (
        "mode flip must re-assert current (firmware may reset register)"
    )
    assert current_writes[-1][2]["value"] == 20


def test_planner_forecaster_subtract_ev_gated_on_auto_mode() -> None:
    """Planner reads EV mode; subtract_ev=True only in auto, False in car/off."""

    class _SpyForecaster:
        """Stand-in for LoadForecaster that records the subtract_ev kwarg."""

        def __init__(self) -> None:
            self.calls: list[bool] = []
            self.last_forecast = None

        def forecast(self, slot_starts, *, subtract_ev: bool = True):
            self.calls.append(subtract_ev)
            from custom_components.pv_optimizer.load_forecaster import LoadForecast
            kw = {s: 1.0 for s in slot_starts}
            used = {s: 7 for s in slot_starts}
            self.last_forecast = LoadForecast(
                kw_per_slot=kw, days_used_per_slot=used,
                ev_subtracted=subtract_ev,
            )
            return self.last_forecast

    # Build a planner config with an EV binding so cfg.ev is not None.
    # Use the same EV-config shape the existing car/off tests use.
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=7.4, max_charging_current_a=32.0,
            min_charging_current_a=6.0, car_battery_kwh=50.0,
        ),
        charger_state_entity="sensor.evcs_state",
        charging_power_entity="sensor.ev_power_w",
        max_current_entity="number.ev_current",
        mode_entity="select.pv_optimizer_ev_mode",
    )
    base_states = _states(buy=[0.10] * 24, sell=[0.05] * 24, load_w=1000.0)
    base_states["sensor.evcs_state"] = StateView(state="disconnected")
    base_states["sensor.ev_power_w"] = StateView(state="0")

    for mode, expected in (("auto", True), ("car", False), ("off", False)):
        states = dict(base_states)
        states["select.pv_optimizer_ev_mode"] = StateView(state=mode)
        spy = _SpyForecaster()
        planner = Planner(
            _config(ev=ev_cfg),
            FakeReader(states),
            FakeCaller(),
            load_forecaster=spy,
        )
        planner.step(NOW)
        assert spy.calls, f"forecaster not called in mode={mode}"
        assert spy.calls[-1] is expected, (
            f"mode={mode} expected subtract_ev={expected}, got {spy.calls[-1]}"
        )


def test_ev_replan_trigger_entities_full_set() -> None:
    from custom_components.pv_optimizer.planner import (
        EVConfig, ev_replan_trigger_entities,
    )
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
        mode_entity="select.pv_optimizer_ev_mode",
        target_kwh_entity="number.pv_optimizer_ev_target_kwh",
        target_pct_entity="number.pv_optimizer_ev_target_pct",
        deadline_entity="datetime.pv_optimizer_ev_deadline",
        planned_start_entity="datetime.pv_optimizer_ev_planned_start",
    )
    assert ev_replan_trigger_entities(ev_cfg) == [
        "sensor.ev_state",
        "number.pv_optimizer_ev_target_kwh",
        "number.pv_optimizer_ev_target_pct",
        "datetime.pv_optimizer_ev_deadline",
        "datetime.pv_optimizer_ev_planned_start",
        "select.pv_optimizer_ev_mode",
    ]


def test_ev_replan_trigger_entities_filters_empty() -> None:
    """The integration-created ids default to '' on EVConfig; drop them.

    The charging-power entity is deliberately NOT watched (its wattage
    fluctuates during normal charging and would trigger needless solves).
    """
    from custom_components.pv_optimizer.planner import (
        EVConfig, ev_replan_trigger_entities,
    )
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=8.0, max_charging_current_a=20.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
    )
    result = ev_replan_trigger_entities(ev_cfg)
    assert result == ["sensor.ev_state"]
    assert "sensor.ev_power" not in result


def test_planner_config_accepts_battery_power_entity() -> None:
    cfg = _config(battery_power_entity="sensor.batt_w")
    assert cfg.battery_power_entity == "sensor.batt_w"


def test_planner_config_battery_power_entity_defaults_none() -> None:
    assert _config().battery_power_entity is None


def test_read_float_or_none_reads_and_handles_bad() -> None:
    states = _states()
    states["sensor.batt_w"] = StateView(state="-1500")
    states["sensor.bad"] = StateView(state="unavailable")
    p = Planner(_config(), FakeReader(states), FakeCaller())
    assert p._read_float_or_none("sensor.batt_w") == -1500.0
    assert p._read_float_or_none("sensor.bad") is None
    assert p._read_float_or_none("sensor.missing") is None
    assert p._read_float_or_none(None) is None


def test_planner_caches_first_slot_pv_and_load() -> None:
    # pv forecast 3 kW at slot 0, load ~1 kW -> cached surplus ~2 kW.
    pv_forecast = {
        (NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): (3000.0 if h == 0 else 0.0)
        for h in range(4)
    }
    states = _states(load_w=1000.0, pv_forecast=pv_forecast)
    p = Planner(_config(), FakeReader(states), FakeCaller())
    p.step(NOW)
    assert p._cached_first_pv_kw == pytest.approx(3.0, abs=1e-6)
    assert p._cached_first_load_kw == pytest.approx(1.0, abs=0.2)


def test_write_ev_current_force_bypasses_tolerance() -> None:
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    ev_cfg = EVConfig(
        params=EVParams(
            max_charging_power_kw=7.2, max_charging_current_a=32.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0,
            current_tolerance_a=1.0),
        charger_state_entity="sensor.ev_state",
        charging_power_entity="sensor.ev_power",
        max_current_entity="number.ev_max_current",
    )
    p = Planner(_config(ev=ev_cfg), FakeReader(_states()), FakeCaller())
    p.ev_state.last_written_current_a = 10
    # 1 A delta is within tolerance -> suppressed without force.
    p._write_ev_current(11)
    assert not [c for c in p.caller.calls
                if c[2].get("entity_id") == "number.ev_max_current"]
    # force=True writes anyway.
    p._write_ev_current(11, force=True)
    writes = [c for c in p.caller.calls
              if c[2].get("entity_id") == "number.ev_max_current"]
    assert writes and writes[-1][2]["value"] == 11


# ---------------------------------------------------------------------------
# Surplus-probe tests (Task 6)
# ---------------------------------------------------------------------------


def _probe_ev_cfg():
    from custom_components.pv_optimizer.planner import EVConfig
    from custom_components.pv_optimizer.models import EVParams
    return EVConfig(
        params=EVParams(
            max_charging_power_kw=7.2, max_charging_current_a=32.0,
            min_charging_current_a=6.0, car_battery_kwh=60.0,
            current_tolerance_a=1.0, buy_price_threshold=0.0),
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


def _probe_states():
    # Battery full (soc_max is 9 kWh of 10 kWh capacity => 90 %), sunny
    # forecast at slot 0, sell price below 0 so the LP never exports, no EV
    # target so the LP plans no charge -> reactive branch.
    pv_forecast = {
        (NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00"): (4000.0 if h == 0 else 0.0)
        for h in range(4)
    }
    states = _states(soc_pct=90.0, load_w=1000.0, sell=[-0.01] * 24,
                     pv_forecast=pv_forecast)
    states["sensor.ev_state"] = StateView(state="Connected")
    states["sensor.ev_power"] = StateView(state="0")
    states["number.ev_max_current"] = StateView(state="0")
    states["switch.ev_start"] = StateView(state="off")
    states["select.ev_mode"] = StateView(state="Manual")
    states["select.pv_optimizer_ev_mode"] = StateView(state="auto")
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="0")
    states["sensor.batt_w"] = StateView(state="0")  # battery idle
    states["sensor.grid_w"] = StateView(state="0")
    return states


def test_probe_arms_and_drives_manual_in_reactive_branch() -> None:
    states = _probe_states()
    p = Planner(_config(ev=_probe_ev_cfg(), battery_power_entity="sensor.batt_w"),
                FakeReader(states), FakeCaller())
    p.step(NOW)
    mode_writes = [c for c in p.caller.calls if c[2].get("entity_id") == "select.ev_mode"]
    current_writes = [c for c in p.caller.calls if c[2].get("entity_id") == "number.ev_max_current"]
    starts = [c for c in p.caller.calls if c[2].get("entity_id") == "switch.ev_start"]
    assert mode_writes and mode_writes[-1][2]["option"] == "Manual"
    assert current_writes and current_writes[-1][2]["value"] == 6  # kicked to min
    assert starts and starts[-1][1] == "turn_on"
    assert p.ev_state.probe_armed is True
    assert p.ev_state.probe_current_a == 6


def test_probe_disabled_without_battery_power_entity() -> None:
    states = _probe_states()
    p = Planner(_config(ev=_probe_ev_cfg()),  # no battery_power_entity
                FakeReader(states), FakeCaller())
    p.step(NOW)
    mode_writes = [c for c in p.caller.calls if c[2].get("entity_id") == "select.ev_mode"]
    # Falls back to the reactive passive handback (EVCS "Auto").
    assert mode_writes and mode_writes[-1][2]["option"] == "Auto"
    assert p.ev_state.probe_armed is False


def test_probe_disarms_back_to_auto_when_exporting() -> None:
    states = _probe_states()
    states["sensor.sell"] = StateView(state="0.30", attributes={"today": [0.30] * 24})
    p = Planner(_config(ev=_probe_ev_cfg(), battery_power_entity="sensor.batt_w"),
                FakeReader(states), FakeCaller())
    p.ev_state.probe_armed = True       # pretend we were probing
    p.ev_state.probe_current_a = 12
    p.step(NOW)
    assert p.ev_state.probe_armed is False
    assert p.ev_state.probe_current_a == 0
    assert p.ev_state.probe_cycles_since_up == 0
    assert p.ev_state.probe_cycles_overshooting == 0
    mode_writes = [c for c in p.caller.calls if c[2].get("entity_id") == "select.ev_mode"]
    assert mode_writes and mode_writes[-1][2]["option"] == "Auto"


def test_probe_holds_then_steps_down_on_sustained_drain() -> None:
    # 500 W sits in the soft band: the first tick holds the current (and does
    # not step up), the second gives an amp back.
    states = _probe_states()
    states["sensor.batt_w"] = StateView(state="-500")
    p = Planner(_config(ev=_probe_ev_cfg(), battery_power_entity="sensor.batt_w"),
                FakeReader(states), FakeCaller())
    p.ev_state.probe_armed = True
    p.ev_state.probe_current_a = 10
    p.step(NOW)
    assert p.ev_state.probe_current_a == 10
    assert p.ev_state.probe_cycles_overshooting == 1
    p.step(NOW + timedelta(minutes=5))
    assert p.ev_state.probe_current_a == 9
    assert p.ev_state.probe_cycles_overshooting == 0


def test_probe_steps_to_zero_and_stops_on_overshoot() -> None:
    states = _probe_states()
    # Past the hard ceiling -> immediate, no sustain wait.
    states["sensor.batt_w"] = StateView(state="-2000")
    p = Planner(_config(ev=_probe_ev_cfg(), battery_power_entity="sensor.batt_w"),
                FakeReader(states), FakeCaller())
    p.ev_state.probe_armed = True
    p.ev_state.probe_current_a = 6   # at min; one step down -> 0
    p.step(NOW)
    current_writes = [c for c in p.caller.calls if c[2].get("entity_id") == "number.ev_max_current"]
    starts = [c for c in p.caller.calls if c[2].get("entity_id") == "switch.ev_start"]
    assert current_writes and current_writes[-1][2]["value"] == 0
    assert starts and starts[-1][1] == "turn_off"
    assert p.ev_state.probe_current_a == 0
    assert p.ev_state.probe_armed is True   # still armed, just not charging


def test_probe_does_not_arm_inside_planned_start_gate() -> None:
    states = _probe_states()
    states["number.pv_optimizer_ev_target_kwh"] = StateView(state="5")
    deadline = (NOW + timedelta(hours=3)).isoformat() + "+00:00"
    states["datetime.pv_optimizer_ev_deadline"] = StateView(state=deadline)
    planned_start = (NOW + timedelta(hours=2)).isoformat() + "+00:00"
    states["datetime.pv_optimizer_ev_planned_start"] = StateView(state=planned_start)
    p = Planner(_config(ev=_probe_ev_cfg(), battery_power_entity="sensor.batt_w"),
                FakeReader(states), FakeCaller())
    p.step(NOW)
    # Gate behavior (idle handback) wins; probe never armed.
    assert p.ev_state.probe_armed is False
    starts = [c for c in p.caller.calls if c[2].get("entity_id") == "switch.ev_start"]
    assert starts and starts[-1][1] == "turn_off"


def test_probe_arms_despite_clipped_live_pv() -> None:
    # Curtailment: the live PV reading is itself clipped to ~load (1 kW) while
    # the raw forecast is 4 kW. The probe must arm off the RAW forecast — if it
    # used the live-clamped value, forecast_surplus would be ~0 and it would go
    # blind exactly when curtailment happens.
    class _ClippedAverager:
        def average_kw(self, entity_id, start, end):
            return 1.0  # clipped to ~load

    states = _probe_states()
    p = Planner(_config(ev=_probe_ev_cfg(), battery_power_entity="sensor.batt_w"),
                FakeReader(states), FakeCaller(), live_averager=_ClippedAverager())
    p.step(NOW)
    assert p._cached_first_pv_kw == pytest.approx(4.0, abs=1e-6)  # raw, not clamped
    assert p.ev_state.probe_armed is True
