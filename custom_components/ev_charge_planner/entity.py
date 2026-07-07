"""Fælles basisklasse for alle EV Charge Planner-entities."""

from __future__ import annotations

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EvcpCoordinator


class EvcpEntity(CoordinatorEntity[EvcpCoordinator]):
    """Basis: samler alt under én enhed og deler coordinator-opdateringer."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EvcpCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self.runtime = coordinator.runtime
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name="EV Charge Planner",
            manufacturer="cpoulsen2",
            model="Smart EV Charging",
        )
