"""Time-entity: afrejse-klokkeslæt (Afgang-mode).

Et rent klokkeslæt (fx 07:00). Systemet sigter altid efter NÆSTE forekomst,
så det aldrig bliver "forældet" som en fast dato ville.
"""

from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EvcpCoordinator
from .entity import EvcpEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EvcpCoordinator = hass.data[DOMAIN][entry.entry_id]
    entity = DepartureTimeEntity(coordinator)
    entity.entity_id = f"time.ev_charge_planner_{entity._evcp_key}"
    async_add_entities([entity])


class DepartureTimeEntity(EvcpEntity, TimeEntity):
    _attr_translation_key = "departure"
    _attr_icon = "mdi:clock-end"

    def __init__(self, coordinator: EvcpCoordinator) -> None:
        super().__init__(coordinator, "departure")

    @property
    def native_value(self) -> time | None:
        try:
            return time.fromisoformat(self.runtime.departure_time)
        except (ValueError, TypeError):
            return time(7, 0)

    async def async_set_value(self, value: time) -> None:
        self.runtime.departure_time = value.isoformat()
        await self.coordinator.async_user_changed()
        self.async_write_ha_state()
