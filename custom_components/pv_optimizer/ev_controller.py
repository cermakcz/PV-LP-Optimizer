"""Pure decision logic for the EV charging feature.

No Home Assistant imports. Owned by tests/test_ev_controller.py.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Mapping, Sequence


class EVStateClass(enum.Enum):
    DISCONNECTED = "disconnected"
    CONNECTED_IDLE = "connected_idle"
    CONNECTED_REQUESTING = "connected_requesting"


# Default substring vocabulary. All matches are case-insensitive, plain
# substring tests (no tokenisation). Precedence in classify_state:
# DISCONNECTED > CONNECTED_REQUESTING > CONNECTED_IDLE.
#
# Needles must literally appear in the raw state. e.g. "wait_sun" does NOT
# match "waiting_for_sun" — the EVCS HACS integration spells these as
# "waiting_for_*", so we include the full form. Older / alternative
# firmwares using "wait_sun" / "wait sun" are also covered.
DEFAULT_STATE_VOCAB: Mapping[EVStateClass, Sequence[str]] = {
    # "idle" deliberately omitted: it appears inside connected-but-not-charging
    # state names like "charging_idle" / "connected_idle" on some firmwares
    # (Wallbox, SMA), and substring precedence would mis-route those to
    # DISCONNECTED. A bare "idle" state falls to the conservative
    # CONNECTED_IDLE fallback, which is safe.
    EVStateClass.DISCONNECTED: ("disconnect", "unplug"),
    EVStateClass.CONNECTED_REQUESTING: (
        "charging",
        # EVCS HACS spellings.
        "waiting_for_sun", "waiting_for_start",
        "waiting_for_rfid", "waiting_for_time",
        # Alternative firmware spellings.
        "wait sun", "wait_sun",
        "wait time", "wait start", "wait rfid",
    ),
    # "low_soc" is the EVCS-side pause when the home battery is below the
    # user-configured floor. It's reported regardless of whether the car is
    # currently asking for power, so we treat it as IDLE: the planner respects
    # the EVCS's home-battery protection unless the LP plan or planner-manual
    # mode explicitly overrides it.
    EVStateClass.CONNECTED_IDLE: ("charged", "connect", "low_soc"),
}

_UNAVAILABLE_STATES = frozenset({"unknown", "unavailable", "none", ""})


def classify_state(
    state: str | None,
    vocab: Mapping[EVStateClass, Sequence[str]] = DEFAULT_STATE_VOCAB,
) -> EVStateClass:
    """Classify a raw charger-state string into one of three classes.

    Returns ``DISCONNECTED`` for ``None`` / empty / ``unknown`` / ``unavailable``
    so the planner treats stale inputs as "no car" and bails (per spec §8).
    """
    if state is None:
        return EVStateClass.DISCONNECTED
    s = state.strip().lower()
    if not s or s in _UNAVAILABLE_STATES:
        return EVStateClass.DISCONNECTED
    # Precedence: disconnected > requesting > idle.
    for cls in (
        EVStateClass.DISCONNECTED,
        EVStateClass.CONNECTED_REQUESTING,
        EVStateClass.CONNECTED_IDLE,
    ):
        for needle in vocab.get(cls, ()):
            if needle.lower() in s:
                return cls
    return EVStateClass.CONNECTED_IDLE  # conservative fallback


# --- Curtailed-surplus probe (see specs/2026-06-23-ev-curtailed-surplus-probe) ---
# All tunable; initial values to validate empirically.
SOC_FULL_EPS_KWH = 0.2        # how close to soc_max counts as "full" (arm)
SOC_DISARM_EPS_KWH = 0.5      # wider margin to stay armed (avoid self-disarm)
PROBE_FORECAST_MARGIN_KW = 0.5  # forecast surplus must exceed this to arm
PROBE_DISCHARGE_CEILING_W = 300.0  # soft band floor: sustained drain steps down
PROBE_DISCHARGE_HARD_W = 1500.0    # hard ceiling: step down immediately
PROBE_IMPORT_CEILING_W = 500.0     # step down above this grid import
PROBE_UP_INTERVAL_CYCLES = 1       # min cycles between speculative up-steps
PROBE_OVERSHOOT_SUSTAIN_CYCLES = 2  # soft-band cycles before stepping down


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
    eps: float = 1e-6,
) -> bool:
    """True iff the planner should take over surplus charging from the EVCS.

    Arms only in the curtailment corner: battery full, no LP-planned EV charge,
    not exporting (so the EVCS's export-follower would be blind), forecast says
    surplus exists, car connected, and both signal sources are available.
    Uses a wider SoC margin while already armed so the probe's own brief
    overshoot-dip can't disarm it.
    """
    if not (battery_power_available and grid_available):
        return False
    if state_class == EVStateClass.DISCONNECTED:
        return False
    if p_ev_chg_kw > eps or p_sell_kw > eps:
        return False
    soc_eps = SOC_DISARM_EPS_KWH if currently_armed else SOC_FULL_EPS_KWH
    if soc_kwh < soc_max_kwh - soc_eps:
        return False
    if forecast_surplus_kw <= PROBE_FORECAST_MARGIN_KW:
        return False
    return True


@dataclass(frozen=True)
class SurplusProbeDecision:
    """One regulator step: next commanded current and updated counters."""

    current_a: int           # integer A; 0 disables charging
    cycles_since_up: int
    cycles_overshooting: int = 0   # consecutive cycles inside the soft band


def decide_surplus_probe(
    *,
    battery_discharge_w: float,
    grid_import_w: float,
    forecast_surplus_kw: float,
    current_a: int,
    cycles_since_up: int,
    cycles_overshooting: int,
    ev,  # EVParams
) -> SurplusProbeDecision:
    """Zero-import regulator step (see spec §"Control law").

    Down, two-tier. Past ``PROBE_DISCHARGE_HARD_W`` of battery discharge, or
    *any* grid import past ``PROBE_IMPORT_CEILING_W``, step down one amp
    immediately (below min -> 0): the loss is real and happening now. Inside
    the soft band (``PROBE_DISCHARGE_CEILING_W`` .. hard) the drain must
    persist ``PROBE_OVERSHOOT_SUSTAIN_CYCLES`` consecutive cycles first, so a
    transient — a kettle, a passing cloud, the settling tick right after an
    up-step — doesn't ratchet the current down. While counting we hold rather
    than step up: we already suspect we're over.

    Up (speculative): at most one amp every PROBE_UP_INTERVAL_CYCLES, only
    while not overshooting, below max, and the next amp still fits the
    forecast surplus headroom. Otherwise hold and advance the up-counter.
    """
    min_a = int(round(ev.min_charging_current_a))
    max_a = int(round(ev.max_charging_current_a))

    def _step_down() -> SurplusProbeDecision:
        new_a = current_a - 1
        if new_a < min_a:
            return SurplusProbeDecision(current_a=0, cycles_since_up=0)
        return SurplusProbeDecision(current_a=new_a, cycles_since_up=0)

    if (battery_discharge_w > PROBE_DISCHARGE_HARD_W
            or grid_import_w > PROBE_IMPORT_CEILING_W):
        return _step_down()

    if battery_discharge_w > PROBE_DISCHARGE_CEILING_W:
        sustained = cycles_overshooting + 1
        if sustained >= PROBE_OVERSHOOT_SUSTAIN_CYCLES:
            return _step_down()
        return SurplusProbeDecision(current_a=current_a, cycles_since_up=0,
                                    cycles_overshooting=sustained)

    if current_a < min_a:
        # Not charging yet — kick to min as the first probe.
        return SurplusProbeDecision(current_a=min_a, cycles_since_up=0)

    can_step_up = (
        current_a < max_a
        and cycles_since_up + 1 >= PROBE_UP_INTERVAL_CYCLES
        and (current_a + 1) * ev.kw_per_amp <= forecast_surplus_kw
    )
    if can_step_up:
        return SurplusProbeDecision(current_a=current_a + 1, cycles_since_up=0)
    return SurplusProbeDecision(current_a=current_a,
                                cycles_since_up=cycles_since_up + 1)


@dataclass(frozen=True)
class ReactiveDecision:
    """Decision output for one planner tick (reactive branch)."""

    max_current_a: int   # integer A; 0 disables charging


def decide_reactive(
    *,
    state_class: EVStateClass,
    grid_power_w: float,
    ev_charging_power_w: float,
    price_buy: float,
    ev,  # EVParams (avoid cyclic import at module top)
) -> ReactiveDecision:
    """One-shot reactive decision per §4.2 (no mode-switching).

    Args:
        state_class: classified state of the charger.
        grid_power_w: site-level grid power (positive = import, negative = export).
        ev_charging_power_w: power the EV is currently drawing.
        price_buy: current buy price (currency/kWh, all-in).
        ev: EVParams with kw_per_amp, min/max current, buy_price_threshold.
    """
    if state_class == EVStateClass.DISCONNECTED:
        return ReactiveDecision(max_current_a=0)
    if price_buy <= ev.buy_price_threshold:
        return ReactiveDecision(max_current_a=int(round(ev.max_charging_current_a)))
    # Surplus tracking: back-add what EV is already drawing so loop converges.
    surplus_kw = max(0.0, (-grid_power_w + ev_charging_power_w) / 1000.0)
    target_a = surplus_kw / ev.kw_per_amp
    if target_a < ev.min_charging_current_a:
        return ReactiveDecision(max_current_a=0)
    clamped = max(ev.min_charging_current_a,
                  min(ev.max_charging_current_a, target_a))
    # Truncate (not round) so we never overshoot available PV surplus.
    # E.g. with 7.5 A of headroom, rounding up to 8 A would pull the last
    # 0.5 A from the grid; truncating to 7 A keeps us on the export side.
    return ReactiveDecision(max_current_a=int(clamped))


def is_session_done(
    *,
    state_class: EVStateClass,
    ev_charging_power_w: float,
    low_power_seconds: float,
    ev,
) -> bool:
    """Return True per §6.1 session-done definition.

    Done iff:
        - disconnected; OR
        - connected_idle AND ev_power < session_done_power_w for
          ≥ session_done_seconds (caller tracks the duration).
    """
    if state_class == EVStateClass.DISCONNECTED:
        return True
    if (state_class == EVStateClass.CONNECTED_IDLE
            and ev_charging_power_w < ev.session_done_power_w
            and low_power_seconds >= ev.session_done_seconds):
        return True
    return False


def translate_lp_slot0(
    *,
    p_ev_chg_kw: float,
    state_class: EVStateClass,
    ev,
) -> int:
    """Convert the LP's slot-0 EV power into a charger max-current setpoint (A).

    - If disconnected, write 0.
    - If LP plans zero, write zero.
    - If LP plans > 0 but the converted current is below
      ``min_charging_current_a``, clamp UP (contrast with reactive's
      skip-below-min): the user has committed to a target, so a minor
      slot-0 overshoot is acceptable. The next tick re-plans with reduced
      remaining_kwh.
    """
    if state_class == EVStateClass.DISCONNECTED:
        return 0
    if p_ev_chg_kw <= 0:
        return 0
    target_a = p_ev_chg_kw / ev.kw_per_amp
    clamped = max(ev.min_charging_current_a,
                  min(ev.max_charging_current_a, target_a))
    return int(round(clamped))
