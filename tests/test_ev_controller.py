"""Unit tests for the EV controller (pure decision logic)."""
from __future__ import annotations

import pytest

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


def test_classify_disconnected_default_substrings() -> None:
    assert classify_state("Disconnected") == EVStateClass.DISCONNECTED
    assert classify_state("Unplugged") == EVStateClass.DISCONNECTED


def test_classify_bare_idle_falls_back_to_idle() -> None:
    """'idle' is intentionally not a DISCONNECTED token so 'charging_idle'
    / 'connected_idle' aren't mis-routed. A bare 'idle' lands on the
    conservative CONNECTED_IDLE fallback.
    """
    assert classify_state("idle") == EVStateClass.CONNECTED_IDLE


def test_classify_connected_idle_compound_states() -> None:
    """Firmwares that emit 'connected_idle' should classify as IDLE
    (matches the 'connect' substring before the IDLE-fallback branch).
    """
    assert classify_state("connected_idle") == EVStateClass.CONNECTED_IDLE


def test_classify_connected_requesting_default_substrings() -> None:
    assert classify_state("Charging") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("Wait sun") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("wait_sun") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("Wait time") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("Wait start") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("WAIT RFID") == EVStateClass.CONNECTED_REQUESTING


def test_classify_evcs_waiting_for_substrings() -> None:
    """The EVCS HACS integration spells gated states as 'waiting_for_*'."""
    assert classify_state("waiting_for_sun") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("WAITING_FOR_START") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("waiting_for_rfid") == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("waiting_for_time") == EVStateClass.CONNECTED_REQUESTING


def test_classify_connected_idle_default_substrings() -> None:
    assert classify_state("Charged") == EVStateClass.CONNECTED_IDLE
    assert classify_state("Connected") == EVStateClass.CONNECTED_IDLE


def test_classify_low_soc_is_idle() -> None:
    """EVCS reports low_soc when home-battery preservation pauses charging.

    It does NOT signal car-side request, so we treat it as IDLE and let the
    LP plan or planner-manual mode decide whether to override the EVCS.
    """
    assert classify_state("low_soc") == EVStateClass.CONNECTED_IDLE
    assert classify_state("LOW_SOC") == EVStateClass.CONNECTED_IDLE


def test_classify_unknown_falls_back_to_connected_idle() -> None:
    """Conservative default: unknown plugged-in classifies safely."""
    assert classify_state("WeirdStatus") == EVStateClass.CONNECTED_IDLE


def test_classify_handles_none_and_unavailable() -> None:
    assert classify_state(None) == EVStateClass.DISCONNECTED
    assert classify_state("unknown") == EVStateClass.DISCONNECTED
    assert classify_state("unavailable") == EVStateClass.DISCONNECTED


def test_classify_precedence_disconnected_wins_over_requesting() -> None:
    """If a state somehow contains both a DISCONNECTED and a REQUESTING
    substring, DISCONNECTED takes precedence per §3.3."""
    # Pathological compound — pick disconnected on tie.
    assert classify_state("unplugged charging") == EVStateClass.DISCONNECTED


def test_classify_custom_vocab_override() -> None:
    custom = {
        EVStateClass.DISCONNECTED: ("frei",),
        EVStateClass.CONNECTED_REQUESTING: ("laedt",),
        EVStateClass.CONNECTED_IDLE: ("voll",),
    }
    assert classify_state("Frei", vocab=custom) == EVStateClass.DISCONNECTED
    assert classify_state("Laedt", vocab=custom) == EVStateClass.CONNECTED_REQUESTING
    assert classify_state("voll", vocab=custom) == EVStateClass.CONNECTED_IDLE


# ---------------------------------------------------------------------------
# Task 6: ReactiveDecision / decide_reactive
# ---------------------------------------------------------------------------

from custom_components.pv_optimizer.ev_controller import (
    ReactiveDecision,
    decide_reactive,
)
from custom_components.pv_optimizer.models import EVParams


def _ev() -> EVParams:
    return EVParams(
        max_charging_power_kw=8.0,
        max_charging_current_a=20.0,
        min_charging_current_a=6.0,
        car_battery_kwh=60.0,
        buy_price_threshold=0.0,
    )


def test_reactive_disconnected_writes_zero() -> None:
    out = decide_reactive(
        state_class=EVStateClass.DISCONNECTED,
        grid_power_w=0.0,
        ev_charging_power_w=0.0,
        price_buy=0.50,
        ev=_ev(),
    )
    assert out.max_current_a == 0


def test_reactive_requesting_no_surplus_no_cheap_grid_writes_zero() -> None:
    """REQUESTING is not a short-circuit: with no surplus and an
    above-threshold price, the function returns 0 even while the car
    is asking — auto mode honours the LP / surplus constraint rather
    than the car's request."""
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_REQUESTING,
        grid_power_w=5000.0,         # importing — no surplus
        ev_charging_power_w=0.0,
        price_buy=0.50,              # not cheap
        ev=_ev(),
    )
    assert out.max_current_a == 0


def test_reactive_requesting_with_surplus_tracks_surplus() -> None:
    """REQUESTING state falls through to surplus math — no short-circuit."""
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_REQUESTING,
        grid_power_w=-3000.0,        # 3 kW surplus
        ev_charging_power_w=0.0,
        price_buy=0.50,
        ev=_ev(),
    )
    # kw_per_amp = 0.4 -> 3 kW / 0.4 = 7.5 -> truncate to 7.
    assert out.max_current_a == 7


def test_reactive_requesting_with_cheap_grid_grants_max() -> None:
    """Cheap-grid still wins for REQUESTING too — it's price-driven, not request-driven."""
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_REQUESTING,
        grid_power_w=5000.0,
        ev_charging_power_w=0.0,
        price_buy=-0.05,             # below threshold
        ev=_ev(),
    )
    assert out.max_current_a == 20


def test_reactive_cheap_grid_grants_max() -> None:
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_IDLE,
        grid_power_w=0.0,
        ev_charging_power_w=0.0,
        price_buy=-0.05,  # below threshold of 0
        ev=_ev(),
    )
    assert out.max_current_a == 20


def test_reactive_surplus_tracking_above_min() -> None:
    """grid=-3kW (exporting 3kW) + ev=0 -> 3kW surplus -> 7.5A clamps to 6A floor? No 3000/400=7.5A."""
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_IDLE,
        grid_power_w=-3000.0,
        ev_charging_power_w=0.0,
        price_buy=0.30,
        ev=_ev(),
    )
    # kw_per_amp = 8/20 = 0.4. surplus = 3 kW. target_a = 3/0.4 = 7.5 -> rounded.
    assert out.max_current_a == 7


def test_reactive_surplus_back_adds_current_ev_power() -> None:
    """Convergence: when EV is already drawing, back-add so loop doesn't ramp down."""
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_IDLE,
        grid_power_w=0.0,          # net zero — EV consumes all surplus
        ev_charging_power_w=3000.0,
        price_buy=0.30,
        ev=_ev(),
    )
    # available = -0 + 3 = 3 kW -> 7A, stable.
    assert out.max_current_a == 7


def test_reactive_surplus_below_min_writes_zero() -> None:
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_IDLE,
        grid_power_w=-1000.0,  # 1 kW surplus -> 2.5A, below min 6A
        ev_charging_power_w=0.0,
        price_buy=0.30,
        ev=_ev(),
    )
    assert out.max_current_a == 0


def test_reactive_surplus_above_max_clamps() -> None:
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_IDLE,
        grid_power_w=-20000.0,  # huge surplus
        ev_charging_power_w=0.0,
        price_buy=0.30,
        ev=_ev(),
    )
    assert out.max_current_a == 20


def test_reactive_cheap_grid_threshold_inclusive() -> None:
    """price_buy <= threshold triggers cheap-grid; default threshold is 0."""
    out = decide_reactive(
        state_class=EVStateClass.CONNECTED_IDLE,
        grid_power_w=0.0,
        ev_charging_power_w=0.0,
        price_buy=0.0,  # equal -> trigger
        ev=_ev(),
    )
    assert out.max_current_a == 20


# ---------------------------------------------------------------------------
# Task 8: is_session_done
# ---------------------------------------------------------------------------

from custom_components.pv_optimizer.ev_controller import is_session_done


def test_session_done_when_disconnected() -> None:
    ev = _ev()
    assert is_session_done(
        state_class=EVStateClass.DISCONNECTED,
        ev_charging_power_w=0.0, low_power_seconds=0.0, ev=ev,
    )


def test_session_done_when_idle_and_low_power_long_enough() -> None:
    ev = _ev()
    assert is_session_done(
        state_class=EVStateClass.CONNECTED_IDLE,
        ev_charging_power_w=50.0, low_power_seconds=120.0, ev=ev,
    )


def test_session_not_done_when_idle_but_brief() -> None:
    ev = _ev()
    assert not is_session_done(
        state_class=EVStateClass.CONNECTED_IDLE,
        ev_charging_power_w=50.0, low_power_seconds=30.0, ev=ev,
    )


def test_session_not_done_when_idle_but_drawing_power() -> None:
    ev = _ev()
    assert not is_session_done(
        state_class=EVStateClass.CONNECTED_IDLE,
        ev_charging_power_w=2000.0, low_power_seconds=600.0, ev=ev,
    )


def test_session_not_done_when_still_requesting() -> None:
    ev = _ev()
    assert not is_session_done(
        state_class=EVStateClass.CONNECTED_REQUESTING,
        ev_charging_power_w=0.0, low_power_seconds=600.0, ev=ev,
    )


# ---------------------------------------------------------------------------
# Task 9: translate_lp_slot0
# ---------------------------------------------------------------------------

from custom_components.pv_optimizer.ev_controller import translate_lp_slot0


def test_translate_disconnected_yields_zero() -> None:
    ev = _ev()
    assert translate_lp_slot0(
        p_ev_chg_kw=0.0,
        state_class=EVStateClass.DISCONNECTED,
        ev=ev,
    ) == 0


def test_translate_lp_zero_yields_zero() -> None:
    ev = _ev()
    assert translate_lp_slot0(
        p_ev_chg_kw=0.0,
        state_class=EVStateClass.CONNECTED_IDLE,
        ev=ev,
    ) == 0


def test_translate_lp_positive_above_min_converts_to_amps() -> None:
    ev = _ev()
    # 4 kW / 0.4 kw/A = 10 A.
    assert translate_lp_slot0(
        p_ev_chg_kw=4.0,
        state_class=EVStateClass.CONNECTED_IDLE,
        ev=ev,
    ) == 10


def test_translate_lp_below_min_clamps_up_to_floor() -> None:
    """Contrast with reactive: LP path clamps up because user committed to a target."""
    ev = _ev()
    # 1 kW / 0.4 = 2.5 A < 6 A floor -> clamp UP.
    assert translate_lp_slot0(
        p_ev_chg_kw=1.0,
        state_class=EVStateClass.CONNECTED_IDLE,
        ev=ev,
    ) == 6


def test_translate_lp_above_max_clamps_down() -> None:
    ev = _ev()
    assert translate_lp_slot0(
        p_ev_chg_kw=100.0,
        state_class=EVStateClass.CONNECTED_IDLE,
        ev=ev,
    ) == 20


def test_translate_lp_zero_yields_zero_even_when_requesting() -> None:
    """LP plan of 0 yields 0 A regardless of car state — the user's plan
    is authoritative, not the car's request signal."""
    ev = _ev()
    assert translate_lp_slot0(
        p_ev_chg_kw=0.0,
        state_class=EVStateClass.CONNECTED_REQUESTING,
        ev=ev,
    ) == 0


def test_translate_lp_positive_when_requesting_uses_lp_value() -> None:
    """REQUESTING with a positive LP plan: current is derived from the
    LP-planned watts, not from the charger's max."""
    ev = _ev()
    # 4 kW / 0.4 kw/A = 10 A.
    assert translate_lp_slot0(
        p_ev_chg_kw=4.0,
        state_class=EVStateClass.CONNECTED_REQUESTING,
        ev=ev,
    ) == 10


def test_select_migrates_manual_to_car() -> None:
    import sys
    from unittest.mock import MagicMock

    # Stub out HA dependencies so the module can be imported without a full
    # HA install in the test environment.  RestoreEntity and SelectEntity must
    # be distinct classes so Python doesn't reject the MRO.
    class _FakeSelectEntity: ...
    class _FakeRestoreEntity: ...

    _ha_stubs = {
        "homeassistant": MagicMock(),
        "homeassistant.components": MagicMock(),
        "homeassistant.components.select": MagicMock(SelectEntity=_FakeSelectEntity),
        "homeassistant.config_entries": MagicMock(),
        "homeassistant.core": MagicMock(),
        "homeassistant.helpers": MagicMock(),
        "homeassistant.helpers.entity_platform": MagicMock(),
        "homeassistant.helpers.restore_state": MagicMock(RestoreEntity=_FakeRestoreEntity),
    }
    _inserted = {k for k in _ha_stubs if k not in sys.modules}
    sys.modules.update(_ha_stubs)
    sys.modules.pop("custom_components.pv_optimizer.select", None)
    try:
        from custom_components.pv_optimizer.select import _migrate_legacy_mode
        assert _migrate_legacy_mode("manual") == "car"
        assert _migrate_legacy_mode("auto") == "auto"
        assert _migrate_legacy_mode("car") == "car"
        assert _migrate_legacy_mode("off") == "off"
        assert _migrate_legacy_mode(None) is None
    finally:
        for k in _inserted:
            sys.modules.pop(k, None)
        sys.modules.pop("custom_components.pv_optimizer.select", None)


def test_car_auto_return_switch_class_shape() -> None:
    """Smoke test: switch platform imports cleanly and defaults to off."""
    import sys
    from unittest.mock import MagicMock

    # Stub out HA dependencies so the module can be imported without a full
    # HA install in the test environment.  RestoreEntity and SwitchEntity must
    # be distinct classes so Python doesn't reject the MRO.
    class _FakeSwitchEntity: ...
    class _FakeRestoreEntity: ...

    _ha_stubs = {
        "homeassistant": MagicMock(),
        "homeassistant.components": MagicMock(),
        "homeassistant.components.switch": MagicMock(SwitchEntity=_FakeSwitchEntity),
        "homeassistant.config_entries": MagicMock(),
        "homeassistant.core": MagicMock(),
        "homeassistant.helpers": MagicMock(),
        "homeassistant.helpers.entity_platform": MagicMock(),
        "homeassistant.helpers.restore_state": MagicMock(RestoreEntity=_FakeRestoreEntity),
    }
    _inserted = {k for k in _ha_stubs if k not in sys.modules}
    sys.modules.update(_ha_stubs)
    # Remove any previously-cached version of the module under test.
    sys.modules.pop("custom_components.pv_optimizer.switch", None)
    try:
        from custom_components.pv_optimizer.switch import _EVCarAutoReturnSwitch
        s = _EVCarAutoReturnSwitch("entry-id-1")
        assert s.entity_id == "switch.pv_optimizer_ev_car_auto_return"
        assert s._attr_unique_id == "entry-id-1_ev_car_auto_return"
        assert s.is_on is False
    finally:
        for k in _inserted:
            sys.modules.pop(k, None)
        sys.modules.pop("custom_components.pv_optimizer.switch", None)


# ---------------------------------------------------------------------------
# Task 3: should_probe_surplus
# ---------------------------------------------------------------------------

from custom_components.pv_optimizer.models import SurplusProbeParams


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


def test_should_probe_arms_in_curtailment_corner() -> None:
    assert should_probe_surplus(**_arm_kwargs()) is True


def test_should_probe_blocks_without_battery_power() -> None:
    assert should_probe_surplus(**_arm_kwargs(battery_power_available=False)) is False


def test_should_probe_blocks_without_grid() -> None:
    assert should_probe_surplus(**_arm_kwargs(grid_available=False)) is False


def test_should_probe_blocks_when_disconnected() -> None:
    assert should_probe_surplus(
        **_arm_kwargs(state_class=EVStateClass.DISCONNECTED)) is False


def test_should_probe_blocks_when_lp_charges() -> None:
    assert should_probe_surplus(**_arm_kwargs(p_ev_chg_kw=2.0)) is False


def test_should_probe_blocks_when_exporting() -> None:
    assert should_probe_surplus(**_arm_kwargs(p_sell_kw=1.0)) is False


def test_should_probe_blocks_when_battery_not_full() -> None:
    # 9.0 - 0.2 (SOC_FULL_EPS) = 8.8 is the arm floor; 8.5 is below it.
    assert should_probe_surplus(**_arm_kwargs(soc_kwh=8.5)) is False


def test_should_probe_blocks_without_forecast_surplus() -> None:
    assert should_probe_surplus(**_arm_kwargs(forecast_surplus_kw=0.1)) is False


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


# ---------------------------------------------------------------------------
# Task 4: decide_surplus_probe
# ---------------------------------------------------------------------------

# 7.2 kW / 32 A => 0.225 kW/A; min 6 A, max 32 A. Single-phase.
_PROBE_EV = EVParams(
    max_charging_power_kw=7.2, max_charging_current_a=32.0,
    min_charging_current_a=6.0, car_battery_kwh=60.0,
)

# 22 kW / 32 A => 0.6875 kW/A; min 6 A, max 32 A. Three-phase: the coarser
# per-amp quantization at three-phase is the root cause of the min-current
# dwell bug this effort fixes (see test_probe_does_not_chatter_across_ticks).
_EV_3P = EVParams(
    max_charging_power_kw=22.0, max_charging_current_a=32.0,
    min_charging_current_a=6.0, car_battery_kwh=60.0,
)


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


def test_probe_kicks_to_min_when_not_charging() -> None:
    d = _probe(current_a=0)
    assert d.current_a == 6
    assert d.cycles_since_up == 0


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


def test_probe_restart_cooldown_boundary_is_exclusive() -> None:
    """The gate is ``probe_off_seconds < cooldown``, so exactly-equal is
    treated as elapsed. Pinned because an off-by-one here is easy to
    introduce and would silently extend every cooldown by a full cycle.
    """
    d = _probe(current_a=0, probe_off_seconds=600.0)
    assert d.current_a == 6


def test_probe_restart_soc_boundary_is_exclusive() -> None:
    """The gate is ``soc_deficit_kwh > restart_eps``, so a deficit exactly at
    restart_eps still permits the restart.
    """
    d = _probe(current_a=0, probe_off_seconds=1200.0,
               soc_deficit_kwh=SOC_FULL_EPS_KWH)
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


def test_probe_holds_on_first_soft_battery_discharge() -> None:
    # Inside the soft band (300..1500 W): a single tick of drain is not enough
    # to give up the current. Hold — notably WITHOUT stepping up — and count.
    d = _probe(current_a=10, battery_discharge_w=500.0)
    assert d.current_a == 10
    assert d.cycles_overshooting == 1


def test_probe_steps_down_after_sustained_soft_battery_discharge() -> None:
    d = _probe(current_a=10, battery_discharge_w=500.0,
               cycles_overshooting=PROBE_OVERSHOOT_SUSTAIN_CYCLES - 1)
    assert d.current_a == 9
    assert d.cycles_since_up == 0
    assert d.cycles_overshooting == 0


def test_probe_soft_overshoot_counter_resets_on_clean_cycle() -> None:
    # A transient dip must not accumulate across unrelated ticks.
    d = _probe(current_a=10, battery_discharge_w=0.0, cycles_overshooting=1)
    assert d.cycles_overshooting == 0


def test_probe_steps_down_immediately_on_hard_battery_discharge() -> None:
    # Past the hard ceiling the drain is real (e.g. a cloud killed PV and the
    # car is pulling kW out of the house battery) — correct now, don't wait.
    d = _probe(current_a=10, battery_discharge_w=2000.0)
    assert d.current_a == 9
    assert d.cycles_since_up == 0
    assert d.cycles_overshooting == 0


def test_probe_steps_down_on_grid_import() -> None:
    # Grid import is never sustained-gated: we are paying for it.
    d = _probe(current_a=10, grid_import_w=800.0)
    assert d.current_a == 9
    assert d.cycles_overshooting == 0


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


def test_probe_holds_at_min_past_the_floor_while_soc_stays_full() -> None:
    """No maximum-on cap: past the floor, a near-full battery keeps charging
    however long it has been on.

    Note the absence of a cap is structural — there is no elapsed-time check
    after the floor — so one call past the floor is all a stateless function
    can demonstrate. A very large ``probe_on_seconds`` documents the intent.
    """
    d = _probe(current_a=6, battery_discharge_w=2000.0,
               probe_on_seconds=86400.0, soc_deficit_kwh=0.1)
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


# -- import escapes (minimum current) --

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


def test_probe_soft_import_at_min_sustained_overrides_the_floor() -> None:
    """Sustained soft import stops charging even inside the min-on floor,
    just as hard import does. Pinned separately because
    ``test_probe_soft_import_at_min_stops_once_sustained`` passes a
    probe_on_seconds already past the floor and so cannot tell the two
    explanations apart.
    """
    d = _probe(current_a=6, grid_import_w=800.0, import_over_seconds=601.0,
               probe_on_seconds=10.0)
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


def test_probe_soft_import_at_min_still_stops_on_spent_budget() -> None:
    """The soft-import fall-through does not force a hold. An exhausted SoC
    budget stops charging regardless of why we fell through.
    """
    d = _probe(current_a=6, grid_import_w=800.0, import_over_seconds=60.0,
               probe_on_seconds=1200.0, soc_deficit_kwh=1.2)
    assert d.current_a == 0


def test_probe_at_min_emits_zero_overshoot_count() -> None:
    """cycles_overshooting governs one-amp steps above min only. A stale count
    must not leak into a later above-min state.
    """
    d = _probe(current_a=6, battery_discharge_w=500.0, probe_on_seconds=120.0,
               cycles_overshooting=1)
    assert d.cycles_overshooting == 0


def test_probe_steps_up_every_cycle() -> None:
    # Ramp is aggressive: a clean cycle steps up, no rate-limit dwell.
    d = _probe(current_a=10, cycles_since_up=0)
    assert d.current_a == 11
    assert d.cycles_since_up == 0


def test_probe_steps_up_after_interval() -> None:
    d = _probe(current_a=10, cycles_since_up=PROBE_UP_INTERVAL_CYCLES - 1)
    assert d.current_a == 11
    assert d.cycles_since_up == 0


def test_probe_up_gated_by_forecast_headroom() -> None:
    # current 10 A -> 11 A needs 11*0.225 = 2.475 kW. Forecast surplus 2.0 kW
    # is below that, so no up-step even though the interval elapsed.
    d = _probe(current_a=10, cycles_since_up=PROBE_UP_INTERVAL_CYCLES - 1,
               forecast_surplus_kw=2.0)
    assert d.current_a == 10
    assert d.cycles_since_up == PROBE_UP_INTERVAL_CYCLES


def test_probe_does_not_chatter_across_ticks() -> None:
    """REGRESSION, composition-level: the reported bug was 0 -> min -> 0 -> min
    on consecutive ticks.

    Drives the real decision function over a tick sequence with three-phase
    quantization (22 kW / 32 A => 6 A is ~4.1 kW, larger than the 3 kW of
    curtailed surplus available), which is what made the old drain test
    permanently unsatisfiable at min current. Pins the run-length guarantees
    -- the min-on floor, the restart cooldown, and charging runs far longer
    than the reported ~15 minutes -- rather than a transition count: at these
    parameters the probe settles into a stable limit cycle of roughly 55 min
    charging against ~25 min off, which is expected (the SoC budget binds on
    a fixed schedule here), not chatter.
    """
    probe = SurplusProbeParams(soc_drop_kwh=0.9)
    cycle_s, surplus_kw = 300.0, 3.0

    current, on_s, off_s, deficit = 6, 0.0, float("inf"), 0.0
    cycles_since_up = cycles_overshooting = 0
    # Track alternating runs of charging / not-charging, in ticks.
    runs: list[tuple[bool, int]] = []

    for _ in range(48):  # 4 hours at a 300 s cadence
        draw_kw = current * _EV_3P.kw_per_amp
        drain_kw = max(0.0, draw_kw - surplus_kw)
        d = decide_surplus_probe(
            battery_discharge_w=drain_kw * 1000.0,
            grid_import_w=0.0,
            forecast_surplus_kw=surplus_kw,
            current_a=current,
            cycles_since_up=cycles_since_up,
            cycles_overshooting=cycles_overshooting,
            probe_on_seconds=on_s,
            probe_off_seconds=off_s,
            import_over_seconds=0.0,
            soc_deficit_kwh=deficit,
            ev=_EV_3P,
            probe=probe,
        )
        previous = current
        current = d.current_a
        cycles_since_up = d.cycles_since_up
        cycles_overshooting = d.cycles_overshooting

        is_charging = current > 0
        if runs and runs[-1][0] == is_charging:
            charging, n = runs[-1]
            runs[-1] = (charging, n + 1)
        else:
            runs.append((is_charging, 1))
        if current > 0:
            # A transition tick counts as zero elapsed in the new state, not
            # one whole cycle -- conservative for the floor/cooldown checks,
            # which see the smaller (not-yet-elapsed) value first.
            on_s = 0.0 if previous == 0 else on_s + cycle_s
            off_s = float("inf") if previous == 0 else off_s
        else:
            off_s = 0.0 if previous > 0 else off_s + cycle_s
            on_s = 0.0
        # The current in force during this tick is the pre-decision one,
        # which is what produced drain_kw; the new setpoint only takes effect
        # next tick. Gating on `current` here would credit a tick of recovery
        # on the very tick the car was still charging.
        rate_kw = drain_kw if previous > 0 else -surplus_kw
        deficit = max(0.0, deficit + rate_kw * cycle_s / 3600.0)

    # Only complete runs can be checked; the first and last are truncated by
    # the window, so drop them.
    complete = runs[1:-1]
    on_runs = [n * cycle_s for charging, n in complete if charging]
    off_runs = [n * cycle_s for charging, n in complete if not charging]

    assert on_runs and off_runs, f"expected alternating runs, got {runs}"

    # The connector-protection guarantee: no charging run is shorter than the
    # min-on floor.
    assert min(on_runs) >= probe.min_on_seconds, (
        f"charging run shorter than the min-on floor: {on_runs}")

    # The off-side guarantee: no pause is shorter than the restart cooldown.
    assert min(off_runs) >= probe.restart_cooldown_seconds, (
        f"pause shorter than the restart cooldown: {off_runs}")

    # The actual regression. The reported bug charged ~15 min and then cut out
    # for a cycle, indefinitely. Every charging run must now be substantially
    # longer than that, which is what makes the SoC budget rather than the
    # instantaneous drain the thing that ends a session. The 1800 s (30 min)
    # bar is derived from this test's own fixture: measured runs are ~55 min
    # at _EV_3P / surplus_kw=3.0 / probe.soc_drop_kwh=0.9, comfortably above
    # both the ~15 min broken behaviour and this bar. Changing any of those
    # three values changes the measured run length and may require
    # revisiting this bar too -- otherwise such a change could silently
    # weaken it rather than fail loudly.
    assert min(on_runs) >= 1800.0, (
        f"charging runs are still short — the drain test may be deciding "
        f"again rather than the SoC budget: "
        f"{[s / 60 for s in on_runs]} minutes")


def test_probe_does_not_exceed_max() -> None:
    d = _probe(current_a=32, cycles_since_up=PROBE_UP_INTERVAL_CYCLES - 1)
    assert d.current_a == 32


# ---------------------------------------------------------------------------
# SurplusProbeParams
# ---------------------------------------------------------------------------


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
    {"restart_cooldown_seconds": -1.0},
    {"soc_drop_kwh": 0.0},
    {"soc_drop_kwh": -0.5},
    {"import_hard_w": 0.0},
    {"import_hard_w": -100.0},
])
def test_probe_params_rejects_non_positive(kwargs) -> None:
    with pytest.raises(ValueError):
        SurplusProbeParams(**kwargs)


# ---------------------------------------------------------------------------
# probe_floor_outspends_budget
# ---------------------------------------------------------------------------

from custom_components.pv_optimizer.ev_controller import (
    probe_floor_outspends_budget,
)

# _EV_3P is defined above, near _PROBE_EV: 22 kW / 32 A => 0.6875 kW/A, so
# 6 A is ~4.1 kW.


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
