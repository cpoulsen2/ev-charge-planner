"""Button-entities: genberegn, lad straks, stop force."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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
    entities = [
        RecalculateButton(coordinator),
        ForceChargeButton(coordinator),
        StopForceButton(coordinator),
    ]
    for e in entities:
        e.entity_id = f"button.ev_charge_planner_{e._evcp_key}"
    async_add_entities(entities)


class RecalculateButton(EvcpEntity, ButtonEntity):
    _attr_translation_key = "recalculate"
    _attr_icon = "mdi:calculator-variant"

    def __init__(self, coordinator: EvcpCoordinator) -> None:
        super().__init__(coordinator, "recalculate")

    async def async_press(self) -> None:
        await self.coordinator.async_user_changed()


class ForceChargeButton(EvcpEntity, ButtonEntity):
    """Drop planen og lad straks."""

    _attr_translation_key = "force_charge"
    _attr_icon = "mdi:flash-alert"

    def __init__(self, coordinator: EvcpCoordinator) -> None:
        super().__init__(coordinator, "force_charge")

    async def async_press(self) -> None:
        self.runtime.force_charge = True
        self.runtime.enabled = True
        await self.coordinator.async_user_changed()


class StopForceButton(EvcpEntity, ButtonEntity):
    """Stop force-ladning (tilbage til plan)."""

    _attr_translation_key = "stop_force"
    _attr_icon = "mdi:flash-off"

    def __init__(self, coordinator: EvcpCoordinator) -> None:
        super().__init__(coordinator, "stop_force")

    async def async_press(self) -> None:
        self.runtime.force_charge = False
        await self.coordinator.async_user_changed()
