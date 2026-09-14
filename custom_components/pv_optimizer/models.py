"""Pure data models used by the optimizer.

These dataclasses contain no Home Assistant dependencies so the optimizer
remains fully unit-testable in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence


@dataclass(frozen=True)
class BatteryParams:
    """Static battery parameters."""

    capacity_kwh: float
    soc_min_kwh: float
    soc_max_kwh: float
    p_chg_max_kw: float
    p_dis_max_kw: float
    eta_chg: float = 0.95
    eta_dis: float = 0.95
    cycle_cost_per_kwh: float = 0.05    # currency/kWh delivered (discharge)
    # Soft "health" floor: per-slot penalty rate applied to (soc_health -
    # soc[t])+ so the LP avoids long dwells at low SoC without a hard
    # constraint. Disabled when ``low_soc_penalty_per_kwh_h == 0`` or
    # ``soc_health_kwh <= soc_min_kwh`` — both defaults make this a no-op.
    soc_health_kwh: float = 0.0
    low_soc_penalty_per_kwh_h: float = 0.0   # currency/(kWh*h) below floor

    def __post_init__(self) -> None:
        if self.capacity_kwh <= 0:
            raise ValueError("capacity_kwh must be > 0")
        if not (0.0 <= self.soc_min_kwh <= self.soc_max_kwh <= self.capacity_kwh):
            raise ValueError("require 0 <= soc_min <= soc_max <= capacity")
        if self.p_chg_max_kw < 0 or self.p_dis_max_kw < 0:
            raise ValueError("power limits must be >= 0")
        if not (0.0 < self.eta_chg <= 1.0) or not (0.0 < self.eta_dis <= 1.0):
            raise ValueError("efficiencies must be in (0, 1]")
        if self.cycle_cost_per_kwh < 0:
            raise ValueError("cycle_cost must be >= 0")
        if self.soc_health_kwh < 0 or self.soc_health_kwh > self.soc_max_kwh:
            raise ValueError("soc_health_kwh must be in [0, soc_max_kwh]")
        if self.low_soc_penalty_per_kwh_h < 0:
            raise ValueError("low_soc_penalty_per_kwh_h must be >= 0")


@dataclass(frozen=True)
class TariffSlot:
    """One discrete planning slot."""

    start: datetime
    duration_h: float
    price_buy: float       # currency/kWh, all-in (spot + surcharges + tax)
    price_sell: float      # currency/kWh, all-in
    feedin_allowed: bool = True

    def __post_init__(self) -> None:
        if self.duration_h <= 0:
            raise ValueError("duration_h must be > 0")


@dataclass(frozen=True)
class OptimizerInputs:
    """Bundle of everything the LP needs."""

    slots: Sequence[TariffSlot]
    pv_kw: Sequence[float]                # average PV power per slot (kW)
    load_kw: Sequence[float]              # average load per slot (kW)
    initial_soc_kwh: float
    battery: BatteryParams
    p_grid_imp_max_kw: float
    p_grid_exp_max_kw: float
    terminal_soc_kwh: float | None = None  # default: initial_soc_kwh
    # Optional EV charging extension. When all three are zero/None the
    # LP creates no EV variables and behaves identically to pre-EV
    # builds (regression no-op).
    ev: "EVParams | None" = None
    ev_target_kwh: float = 0.0
    ev_deadline_index: int | None = None  # exclusive; charging allowed in slots [start_index, deadline_index)
    ev_start_index: int = 0  # inclusive; first slot in which EV charging is permitted

    def __post_init__(self) -> None:
        n = len(self.slots)
        if n == 0:
            raise ValueError("at least one slot required")
        if len(self.pv_kw) != n or len(self.load_kw) != n:
            raise ValueError("pv_kw and load_kw must match slots length")
        if any(v < 0 for v in self.pv_kw) or any(v < 0 for v in self.load_kw):
            raise ValueError("pv_kw and load_kw must be >= 0")
        if not (self.battery.soc_min_kwh
                <= self.initial_soc_kwh
                <= self.battery.soc_max_kwh):
            raise ValueError("initial_soc out of [soc_min, soc_max]")
        if self.p_grid_imp_max_kw < 0 or self.p_grid_exp_max_kw < 0:
            raise ValueError("grid power limits must be >= 0")
        if self.terminal_soc_kwh is not None and not (
            self.battery.soc_min_kwh
            <= self.terminal_soc_kwh
            <= self.battery.soc_max_kwh
        ):
            raise ValueError("terminal_soc out of [soc_min, soc_max]")
        if self.ev_target_kwh < 0:
            raise ValueError("ev_target_kwh must be >= 0")
        if self.ev_target_kwh > 0 and self.ev is None:
            raise ValueError("ev_target_kwh > 0 requires ev params")
        if self.ev_deadline_index is not None and not (
                0 <= self.ev_deadline_index <= n):
            raise ValueError(
                f"ev_deadline_index must be in [0, {n}], got {self.ev_deadline_index}")
        if not (0 <= self.ev_start_index <= n):
            raise ValueError(
                f"ev_start_index must be in [0, {n}], got {self.ev_start_index}")
        if (self.ev_deadline_index is not None
                and self.ev_start_index > self.ev_deadline_index):
            raise ValueError(
                f"ev_start_index ({self.ev_start_index}) must be <= "
                f"ev_deadline_index ({self.ev_deadline_index})")


@dataclass(frozen=True)
class SlotPlan:
    """Optimal decisions for a single slot (kW for power, kWh for SoC).

    ``soc_physical_kwh`` is a planner-layer projection of the *actual* SoC
    the inverter is expected to reach at slot start, simulating passive
    self-consumption (PV→battery→export) for slots where the LP issues no
    forced setpoint. Left ``None`` by the optimizer; populated by the
    planner when wrapping the LP result.

    ``setpoint_w`` is the grid set-point the planner *would* write for
    this slot under the §8.1 active-vs-passive rules (positive = import,
    negative = export; ``0`` = passive / hand control to the EMS). Same
    lifecycle as ``soc_physical_kwh``: ``None`` from the optimizer,
    populated by the planner. Useful for chart overlays so the dashboard
    doesn't have to re-implement the predicate logic.
    """

    index: int
    start: datetime
    duration_h: float
    p_buy_kw: float
    p_sell_kw: float
    p_chg_kw: float
    p_dis_kw: float
    soc_start_kwh: float
    soc_physical_kwh: float | None = None
    setpoint_w: float | None = None
    p_ev_chg_kw: float = 0.0   # EV charging power planned by the LP (or 0)


@dataclass(frozen=True)
class OptimizerResult:
    """Result of solving the LP."""

    slots: Sequence[SlotPlan]
    total_cost: float               # in user-configured currency; negative = net profit
    passive_cost: float             # cost of doing nothing (battery idle)
    status: str                     # "Optimal", ...
    solve_time_s: float
    extras: dict = field(default_factory=dict)

    @property
    def savings(self) -> float:
        return self.passive_cost - self.total_cost


@dataclass(frozen=True)
class EVParams:
    """Static EV-charger parameters. Brand-agnostic.

    ``kw_per_amp`` is the only voltage/phase abstraction the optimizer
    uses: it's derived from the user-declared (power, current) pair at
    the charger's max-current setpoint and applied as a linear factor
    everywhere. No phase or voltage math.
    """

    max_charging_power_kw: float
    max_charging_current_a: float
    min_charging_current_a: float
    car_battery_kwh: float
    current_tolerance_a: float = 1.0
    session_done_power_w: float = 100.0
    session_done_seconds: float = 60.0
    buy_price_threshold: float = 0.0  # currency/kWh; reactive cheap-grid floor

    def __post_init__(self) -> None:
        if self.max_charging_power_kw <= 0:
            raise ValueError("max_charging_power_kw must be > 0")
        if self.max_charging_current_a <= 0:
            raise ValueError("max_charging_current_a must be > 0")
        if not (0 < self.min_charging_current_a <= self.max_charging_current_a):
            raise ValueError(
                "require 0 < min_charging_current_a <= max_charging_current_a")
        if self.car_battery_kwh <= 0:
            raise ValueError("car_battery_kwh must be > 0")
        if self.current_tolerance_a < 0:
            raise ValueError("current_tolerance_a must be >= 0")
        if self.session_done_power_w < 0:
            raise ValueError("session_done_power_w must be >= 0")
        if self.session_done_seconds < 0:
            raise ValueError("session_done_seconds must be >= 0")

    @property
    def kw_per_amp(self) -> float:
        return self.max_charging_power_kw / self.max_charging_current_a


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

    min_on_seconds: float = 300.0
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

        See ``docs/superpowers/specs/2026-09-14-ev-probe-min-current-dwell-design.md``
        ("Restart gating") for the full threshold-ordering derivation.
        """
        return self.soc_drop_kwh + 0.5


class OptimizerError(RuntimeError):
    """Raised when the LP is infeasible or the solver fails."""
