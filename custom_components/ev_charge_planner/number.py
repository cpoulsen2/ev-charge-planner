"""Number-entities: manuel SoC, mål, gæstekapacitet, ladeeffekt."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EvcpCoordinator
from .entity import EvcpEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EvcpCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        CurrentSocNumber(coordinator),
        TargetSocNumber(coordinator),
        GuestCapacityNumber(coordinator),
        ChargePowerNumber(coordinator),
    ]
    for e in entities:
        e.entity_id = f"number.ev_charge_planner_{e._evcp_key}"
    async_add_entities(entities)


class _BaseNumber(EvcpEntity, NumberEntity):
    _attr_mode = NumberMode.SLIDER

    async def _apply(self, value: float) -> None:
        await self.coordinator.async_user_changed()
        self.async_write_ha_state()


class CurrentSocNumber(_BaseNumber):
    _attr_translation_key = "current_soc"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:battery-charging"

    def __init__(self, coordinator: EvcpCoordinator) -> None:
        super().__init__(coordinator, "current_soc")

    @property
    def native_value(self) -> float:
        return self.runtime.current_soc

    async def async_set_native_value(self, value: float) -> None:
        self.runtime.current_soc = value
        # Nulstil baseline så live-SoC starter fra den nye værdi
        self.runtime.session_baseline_kwh = self.coordinator._session_energy()
        await self._apply(value)


class TargetSocNumber(_BaseNumber):
    _attr_translation_key = "target_soc"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:target"

    def __init__(self, coordinator: EvcpCoordinator) -> None:
        super().__init__(coordinator, "target_soc")

    @property
    def native_value(self) -> float:
        return self.runtime.target_soc

    async def async_set_native_value(self, value: float) -> None:
        self.runtime.target_soc = value
        await self._apply(value)


class GuestCapacityNumber(_BaseNumber):
    _attr_translation_key = "guest_capacity"
    _attr_native_min_value = 10
    _attr_native_max_value = 150
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:car-battery"

    def __init__(self, coordinator: EvcpCoordinator) -> None:
        super().__init__(coordinator, "guest_capacity")

    @property
    def native_value(self) -> float:
        return self.runtime.guest_capacity

    async def async_set_native_value(self, value: float) -> None:
        self.runtime.guest_capacity = value
        await self._apply(value)


class ChargePowerNumber(_BaseNumber):
    _attr_translation_key = "charge_power"
    _attr_native_min_value = 1
    _attr_native_max_value = 22
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_icon = "mdi:flash"
    _attr_entity_category = None

    def __init__(self, coordinator: EvcpCoordinator) -> None:
        super().__init__(coordinator, "charge_power")

    @property
    def native_value(self) -> float:
        return self.runtime.charge_power

    async def async_set_native_value(self, value: float) -> None:
        self.runtime.charge_power = value
        await self._apply(value)
