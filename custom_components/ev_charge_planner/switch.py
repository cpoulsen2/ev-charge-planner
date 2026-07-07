"""Switch-entities: master-aktivering og observatør-tilstand."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EvcpCoordinator
from .entity import EvcpEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: EvcpCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            EnabledSwitch(coordinator),
            ObserverModeSwitch(coordinator),
        ]
    )


class EnabledSwitch(EvcpEntity, SwitchEntity):
    _attr_translation_key = "enabled"
    _attr_icon = "mdi:power"

    def __init__(self, coordinator: EvcpCoordinator) -> None:
        super().__init__(coordinator, "enabled")

    @property
    def is_on(self) -> bool:
        return self.runtime.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.runtime.enabled = True
        await self.coordinator.async_user_changed()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.runtime.enabled = False
        await self.coordinator.async_user_changed()
        self.async_write_ha_state()


class ObserverModeSwitch(EvcpEntity, SwitchEntity):
    """Når TÆNDT beregner integrationen kun (rører ikke laderen).

    Slå FRA for at lade integrationen faktisk styre Zaptec.
    """

    _attr_translation_key = "observer_mode"
    _attr_icon = "mdi:eye"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: EvcpCoordinator) -> None:
        super().__init__(coordinator, "observer_mode")

    @property
    def is_on(self) -> bool:
        return self.runtime.observer_mode

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.runtime.observer_mode = True
        await self.coordinator.async_save()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.runtime.observer_mode = False
        await self.coordinator.async_save()
        self.async_write_ha_state()
