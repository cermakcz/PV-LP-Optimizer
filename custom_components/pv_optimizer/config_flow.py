"""UI configuration flow for pv_optimizer.

Imports ``homeassistant`` and is therefore not covered by unit tests in this
repository. Validation logic that does not depend on HA lives in
:mod:`planner` (parameter validation happens via ``BatteryParams`` /
``OptimizerInputs`` post-init checks).
"""
from __future__ import annotations

import copy
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from . import const as C

_DOMAIN = C.DOMAIN


def _sensor(domain: str = "sensor") -> EntitySelector:
    return EntitySelector(EntitySelectorConfig(domain=domain))


def _num(min_value: float, max_value: float, step: float = 0.01,
         unit: str | None = None) -> NumberSelector:
    # ``unit_of_measurement`` must be a string when present; passing ``None``
    # fails voluptuous validation, so omit the key entirely in that case.
    cfg: dict[str, Any] = {
        "min": min_value, "max": max_value, "step": step,
        "mode": NumberSelectorMode.BOX,
    }
    if unit is not None:
        cfg["unit_of_measurement"] = unit
    return NumberSelector(NumberSelectorConfig(**cfg))


_ENTITIES_SCHEMA = vol.Schema({
    vol.Required(C.CONF_LOAD_POWER): _sensor(),
    vol.Required(C.CONF_PV_POWER): _sensor(),
    vol.Required(C.CONF_GRID_POWER): _sensor(),
    vol.Required(C.CONF_BATTERY_SOC): _sensor(),
    vol.Required(C.CONF_BUY_PRICE_TODAY): _sensor(),
    vol.Required(C.CONF_SELL_PRICE_TODAY): _sensor(),
    # Multi-select to support e.g. Solcast HACS today + tomorrow sensors;
    # the planner reads each in order and merges per-hour entries.
    vol.Required(C.CONF_PV_FORECAST): EntitySelector(
        EntitySelectorConfig(domain="sensor", multiple=True)
    ),
    vol.Optional(C.CONF_BUY_PRICE_TOMORROW): _sensor(),
    vol.Optional(C.CONF_SELL_PRICE_TOMORROW): _sensor(),
    vol.Optional(C.CONF_LOAD_FORECAST): _sensor(),
    vol.Optional(C.CONF_FEEDIN_OVERRIDE): EntitySelector(
        EntitySelectorConfig(domain=["switch", "input_boolean", "binary_sensor"])
    ),
    vol.Optional(C.CONF_FORCE_PV_EXPORT): EntitySelector(
        EntitySelectorConfig(domain=["switch", "input_boolean", "binary_sensor"])
    ),
    vol.Optional(C.CONF_BATTERY_POWER): _sensor(),
    vol.Required(C.CONF_GRID_SETPOINT): EntitySelector(
        EntitySelectorConfig(domain=["number", "input_number"])
    ),
    vol.Required(C.CONF_FEEDIN_SWITCH): EntitySelector(
        EntitySelectorConfig(domain=["switch", "input_boolean"])
    ),
})


_BATTERY_SCHEMA = vol.Schema({
    # Free-text currency label (e.g. "EUR", "USD", "CZK"). The planner is
    # currency-agnostic; this string only feeds sensor units and form labels.
    vol.Required(C.CONF_CURRENCY, default=C.DEFAULT_CURRENCY): TextSelector(
        TextSelectorConfig(),
    ),
    vol.Required(C.CONF_BATTERY_CAPACITY_KWH, default=10.0): _num(0.1, 1000.0, 0.1, "kWh"),
    vol.Required(C.CONF_BATTERY_SOC_MIN_PCT, default=C.DEFAULT_SOC_MIN_PCT): _num(0.0, 100.0, 1.0, "%"),
    vol.Required(C.CONF_BATTERY_SOC_MAX_PCT, default=C.DEFAULT_SOC_MAX_PCT): _num(0.0, 100.0, 1.0, "%"),
    vol.Required(C.CONF_BATTERY_P_CHG_MAX_KW, default=5.0): _num(0.0, 100.0, 0.1, "kW"),
    vol.Required(C.CONF_BATTERY_P_DIS_MAX_KW, default=5.0): _num(0.0, 100.0, 0.1, "kW"),
    vol.Required(C.CONF_BATTERY_ETA_CHG, default=C.DEFAULT_ETA_CHG): _num(0.5, 1.0, 0.01),
    vol.Required(C.CONF_BATTERY_ETA_DIS, default=C.DEFAULT_ETA_DIS): _num(0.5, 1.0, 0.01),
    # The currency portion of the unit can't be substituted into a static
    # NumberSelector label at schema-build time, so we leave it as a bare
    # "/kWh" suffix and rely on the Currency field above for context.
    vol.Required(C.CONF_BATTERY_CYCLE_COST, default=C.DEFAULT_CYCLE_COST): _num(0.0, 10.0, 0.001, "/kWh"),
    # Soft "health" floor and its penalty rate (currency / kWh-hour). When
    # the floor is above ``soc_min`` AND the penalty is non-zero, the LP
    # incurs cost for letting projected SoC sit below the floor — same
    # mechanism as the cycle cost but charged on dwell-at-low-SoC instead
    # of on throughput. Default (floor == soc_min, penalty == 0) is a
    # backward-compatible no-op.
    vol.Required(C.CONF_BATTERY_SOC_HEALTH_PCT,
                 default=C.DEFAULT_SOC_HEALTH_PCT): _num(0.0, 100.0, 1.0, "%"),
    vol.Required(C.CONF_BATTERY_LOW_SOC_PENALTY,
                 default=C.DEFAULT_LOW_SOC_PENALTY): _num(0.0, 100.0, 0.01, "/kWh/h"),
})


_SOLVER_SCHEMA = vol.Schema({
    vol.Required(C.CONF_SLOT_MINUTES, default=C.DEFAULT_SLOT_MINUTES): _num(5, 240, 5, "min"),
    vol.Required(C.CONF_HORIZON_HOURS, default=C.DEFAULT_HORIZON_HOURS): _num(1, 48, 1, "h"),
    vol.Required(C.CONF_UPDATE_SECONDS, default=C.DEFAULT_UPDATE_SECONDS): _num(30, 3600, 30, "s"),
    vol.Required(C.CONF_GRID_IMPORT_MAX_KW, default=25.0): _num(0.1, 200.0, 0.5, "kW"),
    vol.Required(C.CONF_GRID_EXPORT_MAX_KW, default=25.0): _num(0.0, 200.0, 0.5, "kW"),
    vol.Required(C.CONF_SETPOINT_TOLERANCE_W, default=C.DEFAULT_SETPOINT_TOLERANCE_W): _num(0.0, 5000.0, 10.0, "W"),
    # Sell-price floor (currency/kWh). Same currency-suffix limitation as
    # the cycle-cost field above: bare "/kWh" + the Currency field on the
    # Battery screen for context.
    vol.Required(C.CONF_MIN_SELL_PRICE, default=C.DEFAULT_MIN_SELL_PRICE): _num(0.0, 100.0, 0.001, "/kWh"),
})


# cap_kw == 0 means "no cap" (matches how __init__.py interprets it).
_LOAD_FORECAST_SCHEMA = vol.Schema({
    vol.Required(C.CONF_LOAD_FORECAST_LOOKBACK_DAYS,
                 default=C.DEFAULT_LOAD_FORECAST_LOOKBACK_DAYS): _num(1, 60, 1, "days"),
    vol.Required(C.CONF_LOAD_FORECAST_CAP_KW,
                 default=C.DEFAULT_LOAD_FORECAST_CAP_KW): _num(0.0, 100.0, 0.1, "kW"),
    vol.Required(C.CONF_LOAD_FORECAST_WEEKDAY_AWARE,
                 default=C.DEFAULT_LOAD_FORECAST_WEEKDAY_AWARE): BooleanSelector(),
})


# All EV fields are optional. Leaving every input/output entity blank
# disables the EV feature entirely.
_EV_SCHEMA = vol.Schema({
    vol.Optional(C.CONF_EV_CHARGER_STATE): _sensor(),
    vol.Optional(C.CONF_EV_CHARGING_POWER): _sensor(),
    vol.Optional(C.CONF_EV_SESSION_ENERGY): _sensor(),
    vol.Optional(C.CONF_EV_MAX_CURRENT): EntitySelector(
        EntitySelectorConfig(domain=["number", "input_number"])
    ),
    vol.Optional(C.CONF_EV_START_SWITCH): EntitySelector(
        EntitySelectorConfig(domain=["switch", "input_boolean"])
    ),
    vol.Optional(C.CONF_EV_CHARGER_MODE): EntitySelector(
        EntitySelectorConfig(domain=["select", "input_select"])
    ),
    # Vendor-specific option strings the planner drives on charger_mode_entity.
    # Defaults match EVCS HACS; users with Zappi / Wallbox / etc. override these.
    vol.Optional(C.CONF_EV_CHARGER_MODE_OPTION_ACTIVE,
                 default=C.DEFAULT_EV_CHARGER_MODE_OPTION_ACTIVE): TextSelector(
        TextSelectorConfig(),
    ),
    vol.Optional(C.CONF_EV_CHARGER_MODE_OPTION_PASSIVE,
                 default=C.DEFAULT_EV_CHARGER_MODE_OPTION_PASSIVE): TextSelector(
        TextSelectorConfig(),
    ),
    vol.Optional(C.CONF_EV_MAX_CHARGING_POWER_KW): _num(0.0, 50.0, 0.1, "kW"),
    vol.Optional(C.CONF_EV_MAX_CHARGING_CURRENT_A): _num(0.0, 64.0, 1.0, "A"),
    vol.Optional(C.CONF_EV_MIN_CHARGING_CURRENT_A,
                 default=C.DEFAULT_EV_MIN_CHARGING_CURRENT_A): _num(0.0, 32.0, 1.0, "A"),
    vol.Optional(C.CONF_EV_BUY_PRICE_THRESHOLD,
                 default=C.DEFAULT_EV_BUY_PRICE_THRESHOLD): _num(-10.0, 10.0, 0.01, "/kWh"),
    vol.Optional(C.CONF_EV_CAR_BATTERY_KWH): _num(0.0, 200.0, 0.5, "kWh"),
    vol.Optional(C.CONF_EV_CURRENT_TOLERANCE_A,
                 default=C.DEFAULT_EV_CURRENT_TOLERANCE_A): _num(0.0, 5.0, 1.0, "A"),
    vol.Optional(C.CONF_EV_SESSION_DONE_POWER_W,
                 default=C.DEFAULT_EV_SESSION_DONE_POWER_W): _num(0.0, 1000.0, 10.0, "W"),
    vol.Optional(C.CONF_EV_SESSION_DONE_SECONDS,
                 default=C.DEFAULT_EV_SESSION_DONE_SECONDS): _num(0.0, 600.0, 5.0, "s"),
    vol.Optional(C.CONF_EV_PROBE_MIN_ON_SECONDS,
                 default=C.DEFAULT_EV_PROBE_MIN_ON_SECONDS): _num(30.0, 3600.0, 30.0, "s"),
    vol.Optional(C.CONF_EV_PROBE_RESTART_COOLDOWN_SECONDS,
                 default=C.DEFAULT_EV_PROBE_RESTART_COOLDOWN_SECONDS): _num(30.0, 3600.0, 30.0, "s"),
    vol.Optional(C.CONF_EV_PROBE_SOC_DROP_PCT,
                 default=C.DEFAULT_EV_PROBE_SOC_DROP_PCT): _num(0.5, 50.0, 0.5, "%"),
    # Lower bound sits above ev_controller.PROBE_IMPORT_CEILING_W (500 W): the
    # hard check runs first, so a hard threshold at or below the soft ceiling
    # would make every soft-tier import stop charging instantly, skipping the
    # PROBE_IMPORT_SUSTAIN_SECONDS grace that stops a household transient from
    # ending a charging session.
    vol.Optional(C.CONF_EV_PROBE_IMPORT_HARD_W,
                 default=C.DEFAULT_EV_PROBE_IMPORT_HARD_W): _num(600.0, 20000.0, 100.0, "W"),
})


class PvOptimizerConfigFlow(config_entries.ConfigFlow, domain=_DOMAIN):
    """Multi-step setup flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_battery()
        return self.async_show_form(step_id="user", data_schema=_ENTITIES_SCHEMA)

    async def async_step_battery(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_solver()
        return self.async_show_form(step_id="battery", data_schema=_BATTERY_SCHEMA)

    async def async_step_solver(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_load_forecast()
        return self.async_show_form(step_id="solver", data_schema=_SOLVER_SCHEMA)

    async def async_step_load_forecast(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_ev()
        return self.async_show_form(
            step_id="load_forecast", data_schema=_LOAD_FORECAST_SCHEMA,
        )

    async def async_step_ev(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="PV LP Optimizer", data=self._data)
        return self.async_show_form(step_id="ev", data_schema=_EV_SCHEMA)

    @staticmethod
    def async_get_options_flow(config_entry):
        return PvOptimizerOptionsFlow()


# Combined schema for the options flow — entities + battery + solver +
# load-forecast knobs in one screen, since options flow re-edits don't need
# the full multi-step UX. Including ``_ENTITIES_SCHEMA`` lets users rebind
# any sensor (e.g. add a feed-in override after initial setup) without
# deleting and re-adding the integration; ``__init__.py`` registers an
# update listener that reloads the entry when options change.
_OPTIONS_SCHEMA = _ENTITIES_SCHEMA.extend(_BATTERY_SCHEMA.schema).extend(
    _SOLVER_SCHEMA.schema
).extend(_LOAD_FORECAST_SCHEMA.schema).extend(_EV_SCHEMA.schema)


def _schema_with_suggestions(schema: vol.Schema, current: dict[str, Any]) -> vol.Schema:
    """Return a copy of ``schema`` with ``suggested_value`` set from ``current``.

    Without this, opening the options flow shows the static field defaults
    instead of the user's saved values, so saving silently resets everything.
    Uses ``copy.copy`` + mutation rather than reconstructing markers, since
    ``vol.Required(..., default=marker.default)`` double-wraps the default
    factory and breaks form validation.
    """
    new_keys: dict[Any, Any] = {}
    for marker, validator in schema.schema.items():
        key = getattr(marker, "schema", marker)
        if isinstance(marker, vol.Marker) and key in current:
            value = current[key]
            # CONF_PV_FORECAST became a multi-select list; legacy entries
            # saved before that change hold a bare string. Wrap it so the
            # form pre-fills correctly without requiring re-setup.
            if key == C.CONF_PV_FORECAST and isinstance(value, str):
                value = [value]
            new_marker = copy.copy(marker)
            new_marker.description = {"suggested_value": value}
            new_keys[new_marker] = validator
        else:
            new_keys[marker] = validator
    return vol.Schema(new_keys)


class PvOptimizerOptionsFlow(config_entries.OptionsFlow):
    # HA 2025.x makes ``OptionsFlow.config_entry`` a read-only property that the
    # framework populates automatically; assigning to it from ``__init__`` raises
    # ``AttributeError``. We don't need a custom init at all — the inherited one
    # is sufficient and the framework wires ``self.config_entry`` for us.

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=_schema_with_suggestions(_OPTIONS_SCHEMA, current),
        )
