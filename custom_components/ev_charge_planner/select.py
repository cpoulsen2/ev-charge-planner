"""Select-entities: aktiv bil og lademodus."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CHARGE_MODES,
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
    async_add_entities(
        [
            ActiveVehicleSelect(coordinator),
            ModeSelect(coordinator),
        ]
    )


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
        self.runtime.active_vehicle = option
        await self.coordinator.async_user_changed()
        self.async_write_ha_state()


class ModeSelect(EvcpEntity, SelectEntity):
    _attr_translation_key = "mode"
    _attr_icon = "mdi:home-clock"
    _attr_options = CHARGE_MODES

    def __init__(self, coordinator: EvcpCoordinator) -> None:
        super().__init__(coordinator, "mode")

    @property
    def current_option(self) -> str:
        return self.runtime.mode

    async def async_select_option(self, option: str) -> None:
        self.runtime.mode = option
        await self.coordinator.async_user_changed()
        self.async_write_ha_state()
