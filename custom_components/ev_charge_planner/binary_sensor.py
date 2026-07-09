"""Binary sensors: lader nu + om SoC sættes manuelt."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ACT_CHARGING, DOMAIN
from .coordinator import EvcpCoordinator
from .entity import EvcpEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EvcpCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        ChargingBinarySensor(coordinator),
        ManualSocBinarySensor(coordinator),
        SocOverrideBinarySensor(coordinator),
    ]
    for e in entities:
        e.entity_id = f"binary_sensor.ev_charge_planner_{e._evcp_key}"
    async_add_entities(entities)


class ChargingBinarySensor(EvcpEntity, BinarySensorEntity):
    _attr_translation_key = "charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, coordinator: EvcpCoordinator) -> None:
        super().__init__(coordinator, "charging")

    @property
    def is_on(self) -> bool:
        d = self.coordinator.data
        return bool(d and d.action == ACT_CHARGING)


class ManualSocBinarySensor(EvcpEntity, BinarySensorEntity):
    """ON = SoC sættes manuelt (bilen har ingen sensor) → vis skyderen.

    OFF = SoC kommer fra bilens sensor → skjul skyderen.
    """

    _attr_translation_key = "manual_soc"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EvcpCoordinator) -> None:
        super().__init__(coordinator, "manual_soc")

    @property
    def is_on(self) -> bool:
        return not self.coordinator.active_vehicle_has_sensor()


class SocOverrideBinarySensor(EvcpEntity, BinarySensorEntity):
    """ON når bilen bruger sensor-anker (VW-hybrid) → vis valgfri overstyrings-skyder."""

    _attr_translation_key = "soc_override"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: EvcpCoordinator) -> None:
        super().__init__(coordinator, "soc_override")

    @property
    def is_on(self) -> bool:
        return self.coordinator.active_vehicle_uses_anchor()
