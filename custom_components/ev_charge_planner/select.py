"""Select-entity: aktiv bil."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CHOOSE_VEHICLE,
    CONF_VEHICLES,
    DOMAIN,
    GUEST_VEHICLE,
)
from .coordinator import EvcpCoordinator
from .entity import EvcpEntity
from .models import Vehicle


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EvcpCoordinator = hass.data[DOMAIN][entry.entry_id]
    # Lademodus (Standard/Afgang) er fjernet — Afgang er eneste tilstand.
    # Fjern den gamle entitet, så den ikke hænger som "utilgængelig".
    registry = er.async_get(hass)
    old = registry.async_get_entity_id("select", DOMAIN, f"{entry.entry_id}_mode")
    if old:
        registry.async_remove(old)
    entities = [ActiveVehicleSelect(coordinator)]
    for e in entities:
        e.entity_id = f"select.ev_charge_planner_{e._evcp_key}"
    async_add_entities(entities)


class ActiveVehicleSelect(EvcpEntity, SelectEntity):
    _attr_translation_key = "active_vehicle"
    _attr_icon = "mdi:car-electric"

    def __init__(self, coordinator: EvcpCoordinator) -> None:
        super().__init__(coordinator, "active_vehicle")

    @property
    def options(self) -> list[str]:
        vehicles = [
            Vehicle.from_dict(v).name
            for v in self.coordinator.entry.options.get(CONF_VEHICLES, [])
        ]
        # Ingen standard-biler: kun "Vælg bil" + Guest + brugerens egne
        return [CHOOSE_VEHICLE, GUEST_VEHICLE, *vehicles]

    @property
    def current_option(self) -> str:
        return self.runtime.active_vehicle

    async def async_select_option(self, option: str) -> None:
        # Kan ikke vælge en bil når laderen ikke er sat i (kun "Vælg bil" tilladt)
        if option != CHOOSE_VEHICLE and not self.coordinator.is_charger_connected():
            self.async_write_ha_state()  # gendan visningen (afvis valget)
            return
        if option != self.runtime.active_vehicle:
            self.runtime.active_vehicle = option
            # Nulstil session-anker så den nye bil starter rent (ingen arvet energi)
            self.coordinator.on_vehicle_changed()
        await self.coordinator.async_user_changed()
        self.async_write_ha_state()

