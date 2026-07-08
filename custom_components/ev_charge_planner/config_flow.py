"""Config flow: opsætning + køretøjshåndtering."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_AUTHORIZE_BUTTON,
    CONF_CHARGE_POWER_SENSOR,
    CONF_CHARGER_MODE_SENSOR,
    CONF_MIN_BLOCK_MINUTES,
    CONF_NOTIFY_SERVICE,
    CONF_PRICE_SENSOR,
    CONF_RESUME_BUTTON,
    CONF_SESSION_ENERGY_SENSOR,
    CONF_NOTIFY_TARGETS,
    CONF_STOP_BUTTON,
    CONF_TOMORROW_SENSOR,
    CONF_VEHICLES,
    DOMAIN,
    NOTIFY_DEFAULTS,
)

_SENSOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
_BINARY = selector.EntitySelector(selector.EntitySelectorConfig(domain="binary_sensor"))
_BUTTON = selector.EntitySelector(selector.EntitySelectorConfig(domain="button"))
_TEXT = selector.TextSelector()


def _data_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_PRICE_SENSOR): _SENSOR,
            vol.Optional(CONF_TOMORROW_SENSOR): _BINARY,
            vol.Required(CONF_CHARGER_MODE_SENSOR): _SENSOR,
            vol.Required(CONF_CHARGE_POWER_SENSOR): _SENSOR,
            vol.Required(CONF_SESSION_ENERGY_SENSOR): _SENSOR,
            vol.Required(CONF_AUTHORIZE_BUTTON): _BUTTON,
            vol.Required(CONF_RESUME_BUTTON): _BUTTON,
            vol.Required(CONF_STOP_BUTTON): _BUTTON,
            vol.Optional(CONF_NOTIFY_SERVICE, default=""): _TEXT,
        }
    )


class EvcpConfigFlow(ConfigFlow, domain=DOMAIN):
    """Håndterer den indledende opsætning."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="EV Charge Planner", data=user_input, options={CONF_VEHICLES: []}
            )
        return self.async_show_form(step_id="user", data_schema=_data_schema())

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> "EvcpOptionsFlow":
        return EvcpOptionsFlow(entry)


class EvcpOptionsFlow(OptionsFlow):
    """Tilføj/fjern biler og justér indstillinger."""

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    def _vehicles(self) -> list[dict]:
        return list(self._entry.options.get(CONF_VEHICLES, []))

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["add_vehicle", "remove_vehicle", "notifications", "settings"],
        )

    async def async_step_notifications(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            changes = {CONF_NOTIFY_TARGETS: user_input.get(CONF_NOTIFY_TARGETS, [])}
            for key in NOTIFY_DEFAULTS:
                changes[key] = user_input.get(key, NOTIFY_DEFAULTS[key])
            return self._save(changes)

        services = self.hass.services.async_services().get("notify", {})
        notify_options = sorted(f"notify.{name}" for name in services)
        opts = self._entry.options

        schema_dict: dict = {
            vol.Optional(
                CONF_NOTIFY_TARGETS,
                default=opts.get(CONF_NOTIFY_TARGETS, []),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=notify_options,
                    multiple=True,
                    custom_value=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        }
        for key, default in NOTIFY_DEFAULTS.items():
            schema_dict[
                vol.Optional(key, default=opts.get(key, default))
            ] = selector.BooleanSelector()

        return self.async_show_form(
            step_id="notifications", data_schema=vol.Schema(schema_dict)
        )

    async def async_step_add_vehicle(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            vehicles = self._vehicles()
            vehicles.append(
                {
                    "name": user_input["name"],
                    "capacity_kwh": float(user_input["capacity_kwh"]),
                    "soc_sensor": user_input.get("soc_sensor") or None,
                }
            )
            return self._save({CONF_VEHICLES: vehicles})
        schema = vol.Schema(
            {
                vol.Required("name"): _TEXT,
                vol.Required("capacity_kwh", default=77): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=10, max=150, step=1, unit_of_measurement="kWh"
                    )
                ),
                vol.Optional("soc_sensor"): _SENSOR,
            }
        )
        return self.async_show_form(step_id="add_vehicle", data_schema=schema)

    async def async_step_remove_vehicle(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        vehicles = self._vehicles()
        names = [v["name"] for v in vehicles]
        if not names:
            return self.async_abort(reason="no_vehicles")
        if user_input is not None:
            remaining = [v for v in vehicles if v["name"] != user_input["name"]]
            return self._save({CONF_VEHICLES: remaining})
        schema = vol.Schema(
            {
                vol.Required("name"): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=names)
                )
            }
        )
        return self.async_show_form(step_id="remove_vehicle", data_schema=schema)

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(
                {CONF_MIN_BLOCK_MINUTES: int(user_input[CONF_MIN_BLOCK_MINUTES])}
            )
        current = self._entry.options.get(CONF_MIN_BLOCK_MINUTES, 0)
        schema = vol.Schema(
            {
                vol.Required(CONF_MIN_BLOCK_MINUTES, default=current): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=240, step=15, unit_of_measurement="min"
                    )
                )
            }
        )
        return self.async_show_form(step_id="settings", data_schema=schema)

    def _save(self, changes: dict) -> ConfigFlowResult:
        options = {**self._entry.options, **changes}
        return self.async_create_entry(title="", data=options)
