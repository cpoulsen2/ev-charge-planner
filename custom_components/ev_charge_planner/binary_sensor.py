"""Binary sensor: lader bilen faktisk lige nu."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ACT_CHARGING, DOMAIN
from .coordinator import EvcpCoordinator
from .entity import EvcpEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EvcpCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ChargingBinarySensor(coordinator)])


class ChargingBinarySensor(EvcpEntity, BinarySensorEntity):
    _attr_translation_key = "charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, coordinator: EvcpCoordinator) -> None:
        super().__init__(coordinator, "charging")

    @property
    def is_on(self) -> bool:
        d = self.coordinator.data
        return bool(d and d.action == ACT_CHARGING)
