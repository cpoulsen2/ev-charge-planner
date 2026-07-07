"""Datetime-entity: afrejsetid (Afgang-mode)."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import EvcpCoordinator
from .entity import EvcpEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EvcpCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DepartureDateTime(coordinator)])


class DepartureDateTime(EvcpEntity, DateTimeEntity):
    _attr_translation_key = "departure"
    _attr_icon = "mdi:clock-end"

    def __init__(self, coordinator: EvcpCoordinator) -> None:
        super().__init__(coordinator, "departure")

    @property
    def native_value(self) -> datetime | None:
        dep = self.runtime.departure
        if dep is None:
            return None
        # DateTimeEntity kræver tz-aware (UTC)
        if dep.tzinfo is None:
            dep = dt_util.as_local(dep)
        return dt_util.as_utc(dep)

    async def async_set_value(self, value: datetime) -> None:
        self.runtime.departure = value
        await self.coordinator.async_user_changed()
        self.async_write_ha_state()
