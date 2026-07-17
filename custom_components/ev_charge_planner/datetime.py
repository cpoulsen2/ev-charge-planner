"""Datetime-entity: afrejsetid (Afgang-mode).

Dato + tid. Integrationen auto-holder den i fremtiden (næste kl. 07:00), så
den aldrig bliver forældet — men du kan overstyre til en anden fremtidig
dato/tid, som bruges indtil den passeres.
"""

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
    entity = DepartureDateTime(coordinator)
    entity.entity_id = f"datetime.ev_charge_planner_{entity._evcp_key}"
    async_add_entities([entity])


class DepartureDateTime(EvcpEntity, DateTimeEntity):
    _attr_translation_key = "departure"
    _attr_icon = "mdi:clock-end"

    def __init__(self, coordinator: EvcpCoordinator) -> None:
        super().__init__(coordinator, "departure")

    @property
    def native_value(self) -> datetime | None:
        # Sørg for at værdien er i fremtiden inden vi viser den
        self.coordinator.maintain_departure()
        iso = self.runtime.departure_iso
        if not iso:
            return None
        dep = dt_util.parse_datetime(iso)
        if dep is None:
            return None
        if dep.tzinfo is None:
            dep = dt_util.as_local(dep)
        return dt_util.as_utc(dep)

    async def async_set_value(self, value: datetime) -> None:
        self.runtime.departure_iso = value.isoformat()
        await self.coordinator.async_user_changed()
        self.async_write_ha_state()
