"""Constants for the pv_optimizer integration."""
from __future__ import annotations

DOMAIN = "pv_optimizer"
PLATFORMS = ["sensor", "number", "select", "datetime", "switch"]

# HA surfaces a missing / pending / failed entity state as None / "" /
# "unknown" / "unavailable". All readers in this integration treat these
# the same way ("no data → fall back to default"). Centralised here so
# the planner core, the HA platforms, and the restore-state paths can
# share one definition.
BAD_STATES: frozenset[str | None] = frozenset({None, "", "unknown", "unavailable"})

# --- Configuration keys: input sensor entity ids ---
CONF_LOAD_POWER = "load_power_entity"
CONF_PV_POWER = "pv_power_entity"
CONF_GRID_POWER = "grid_power_entity"
CONF_BATTERY_SOC = "battery_soc_entity"
CONF_BUY_PRICE_TODAY = "buy_price_today_entity"
CONF_BUY_PRICE_TOMORROW = "buy_price_tomorrow_entity"
CONF_SELL_PRICE_TODAY = "sell_price_today_entity"
CONF_SELL_PRICE_TOMORROW = "sell_price_tomorrow_entity"
CONF_PV_FORECAST = "pv_forecast_entity"
CONF_LOAD_FORECAST = "load_forecast_entity"
CONF_FEEDIN_OVERRIDE = "feedin_override_entity"
CONF_FORCE_PV_EXPORT = "force_pv_export_entity"
CONF_BATTERY_POWER = "battery_power_entity"

# --- Configuration keys: control entity ids ---
CONF_GRID_SETPOINT = "grid_setpoint_entity"
CONF_FEEDIN_SWITCH = "feedin_switch_entity"
CONF_MAX_CHARGE_POWER_ENTITY = "max_charge_power_entity"
CONF_MAX_DISCHARGE_POWER_ENTITY = "max_discharge_power_entity"

# --- Battery parameters ---
CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
CONF_BATTERY_SOC_MIN_PCT = "battery_soc_min_pct"
CONF_BATTERY_SOC_MAX_PCT = "battery_soc_max_pct"
CONF_BATTERY_P_CHG_MAX_KW = "battery_p_chg_max_kw"
CONF_BATTERY_P_DIS_MAX_KW = "battery_p_dis_max_kw"
CONF_BATTERY_ETA_CHG = "battery_eta_chg"
CONF_BATTERY_ETA_DIS = "battery_eta_dis"
CONF_BATTERY_CYCLE_COST = "battery_cycle_cost_per_kwh"
# Soft "health" floor above the hard ``soc_min``. The optimizer adds a
# linear penalty for each kWh-hour the projected SoC spends below this
# value, so the LP self-corrects against long dwells at the bottom of
# the operating range without rigidly constraining short, high-value
# discharges. Disabled by default (``soc_health == soc_min`` AND
# penalty == 0). See PRD §8.5.
CONF_BATTERY_SOC_HEALTH_PCT = "battery_soc_health_pct"
CONF_BATTERY_LOW_SOC_PENALTY = "battery_low_soc_penalty_per_kwh_h"

# --- Solver / planning ---
CONF_SLOT_MINUTES = "slot_minutes"
CONF_HORIZON_HOURS = "horizon_hours"
CONF_UPDATE_SECONDS = "update_seconds"
CONF_GRID_IMPORT_MAX_KW = "grid_import_max_kw"
CONF_GRID_EXPORT_MAX_KW = "grid_export_max_kw"
CONF_SETPOINT_TOLERANCE_W = "setpoint_tolerance_w"
CONF_MIN_SELL_PRICE = "min_sell_price_per_kwh"

# --- Tariff surcharges (added to spot prices) ---
CONF_BUY_SURCHARGE = "buy_surcharge_per_kwh"
CONF_SELL_SURCHARGE = "sell_surcharge_per_kwh"

# --- Currency (display only; the planner is currency-agnostic and treats
# tariff prices as opaque per-kWh numbers in whatever unit the user's
# tariff sensors report). Used as the unit_of_measurement for cost/savings
# sensors and as a label suffix on currency-denominated config fields.
CONF_CURRENCY = "currency"

# --- Built-in load forecaster ---
CONF_LOAD_FORECAST_LOOKBACK_DAYS = "load_forecast_lookback_days"
CONF_LOAD_FORECAST_CAP_KW = "load_forecast_cap_kw"
CONF_LOAD_FORECAST_WEEKDAY_AWARE = "load_forecast_weekday_aware"

# --- Defaults ---
DEFAULT_SLOT_MINUTES = 60
DEFAULT_HORIZON_HOURS = 24
DEFAULT_UPDATE_SECONDS = 300
DEFAULT_SOC_MIN_PCT = 10.0
DEFAULT_SOC_MAX_PCT = 100.0
DEFAULT_ETA_CHG = 0.95
DEFAULT_ETA_DIS = 0.95
DEFAULT_CYCLE_COST = 0.05  # currency/kWh of throughput; tune to your currency
                           # (typical: ~0.05 EUR, ~0.06 USD, ~1.5 CZK, etc.)
# Health floor defaults to soc_min so the soft-floor penalty has zero
# bandwidth to act on out of the box; combined with a zero penalty rate
# this is a regression no-op until the user opts in.
DEFAULT_SOC_HEALTH_PCT = DEFAULT_SOC_MIN_PCT
DEFAULT_LOW_SOC_PENALTY = 0.0   # currency/(kWh*h) below the health floor
DEFAULT_SETPOINT_TOLERANCE_W = 50.0
DEFAULT_MIN_SELL_PRICE = 0.0   # currency/kWh; 0 = no floor (sell whenever LP wants to)
DEFAULT_LOAD_FORECAST_LOOKBACK_DAYS = 7
DEFAULT_LOAD_FORECAST_CAP_KW = 0.0   # 0 = no cap
DEFAULT_LOAD_FORECAST_WEEKDAY_AWARE = False
DEFAULT_CURRENCY = "EUR"

# --- EV charging (all optional; leaving blank disables the feature) ---
CONF_EV_CHARGER_STATE = "ev_charger_state_entity"
CONF_EV_CHARGING_POWER = "ev_charging_power_entity"
CONF_EV_SESSION_ENERGY = "ev_session_energy_entity"
CONF_EV_MAX_CURRENT = "ev_max_current_entity"
CONF_EV_START_SWITCH = "ev_start_switch_entity"
CONF_EV_CHARGER_MODE = "ev_charger_mode_entity"
# Per-charger option strings for the charger_mode select entity. Vocab
# varies between vendors (EVCS uses "Manual"/"Auto", Zappi uses
# "Eco+"/"Stopped", etc.), so the option text is configurable. "Active" =
# planner-controls-current (manual-style), "Passive" = charger-decides
# (auto-style).
CONF_EV_CHARGER_MODE_OPTION_ACTIVE = "ev_charger_mode_option_active"
CONF_EV_CHARGER_MODE_OPTION_PASSIVE = "ev_charger_mode_option_passive"

CONF_EV_MAX_CHARGING_POWER_KW = "ev_max_charging_power_kw"
CONF_EV_MAX_CHARGING_CURRENT_A = "ev_max_charging_current_a"
CONF_EV_MIN_CHARGING_CURRENT_A = "ev_min_charging_current_a"
CONF_EV_BUY_PRICE_THRESHOLD = "ev_buy_price_threshold"
CONF_EV_CAR_BATTERY_KWH = "ev_car_battery_kwh"
CONF_EV_CURRENT_TOLERANCE_A = "ev_current_tolerance_a"
CONF_EV_SESSION_DONE_POWER_W = "ev_session_done_power_w"
CONF_EV_SESSION_DONE_SECONDS = "ev_session_done_seconds"

# Curtailed-surplus probe: minimum-current dwell. At min charging current the
# probe has no down-step available, so the only move is to stop -- expensive
# (it cycles the car's connector and costs throughput). These govern it in
# place of the instantaneous discharge ceilings, which cannot tell "surplus
# arriving via a full battery" from "the battery draining into the car".
CONF_EV_PROBE_MIN_ON_SECONDS = "ev_probe_min_on_seconds"
CONF_EV_PROBE_RESTART_COOLDOWN_SECONDS = "ev_probe_restart_cooldown_seconds"
CONF_EV_PROBE_SOC_DROP_PCT = "ev_probe_soc_drop_pct"
CONF_EV_PROBE_IMPORT_HARD_W = "ev_probe_import_hard_w"

DEFAULT_EV_MIN_CHARGING_CURRENT_A = 6.0
DEFAULT_EV_BUY_PRICE_THRESHOLD = 0.0
DEFAULT_EV_CURRENT_TOLERANCE_A = 1.0
DEFAULT_EV_SESSION_DONE_POWER_W = 100.0
DEFAULT_EV_SESSION_DONE_SECONDS = 60.0

# 300 s rather than 600: at 600 the three-phase worst-case floor spend is
# 0.69 kWh, which exceeds a 6 % budget on any battery below ~11.5 kWh and would
# make the budget dead code (and fire the setup warning) on stock defaults.
DEFAULT_EV_PROBE_MIN_ON_SECONDS = 300.0
DEFAULT_EV_PROBE_RESTART_COOLDOWN_SECONDS = 600.0
# Percent of battery capacity, converted to kWh in __init__.py -- matches the
# other SoC fields on the form. 6 % of a 10-20 kWh battery is ~0.6-1.2 kWh,
# putting the stop threshold around 94 % of soc_max and the restart at ~98 %.
# Both are offsets below the configured soc_max, not absolute SoC.
DEFAULT_EV_PROBE_SOC_DROP_PCT = 6.0
DEFAULT_EV_PROBE_IMPORT_HARD_W = 2000.0
# EVCS HACS uses "Manual"/"Auto"; users with other chargers override
# these in the config flow.
DEFAULT_EV_CHARGER_MODE_OPTION_ACTIVE = "Manual"
DEFAULT_EV_CHARGER_MODE_OPTION_PASSIVE = "Auto"
