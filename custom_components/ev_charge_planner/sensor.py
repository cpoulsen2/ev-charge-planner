"""Sensor-entities: status-diagnose, live SoC, næste slot, estimeret pris."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
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
        StatusSensor(coordinator),
        LiveSocSensor(coordinator),
        NextSlotSensor(coordinator),
        CurrentSlotEndSensor(coordinator),
        EstimatedCostSensor(coordinator),
    ]
    for e in entities:
        e.entity_id = f"sensor.ev_charge_planner_{e._evcp_key}"
    async_add_entities(entities)


class StatusSensor(EvcpEntity, SensorEntity):
    """"Hvorfor lader den (ikke)?" — den centrale fejlsøgnings-entity."""

    _attr_translation_key = "status"
    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator: EvcpCoordinator) -> None:
        super().__init__(coordinator, "status")

    @property
    def native_value(self) -> str | None:
        d = self.coordinator.data
        return d.action if d else None

    @property
    def extra_state_attributes(self) -> dict:
        d = self.coordinator.data
        if not d:
            return {}
        plan = self.coordinator.plan_result
        blocks = []
        if plan:
            blocks = [
                {
                    "start": b.start_dt.isoformat(),
                    "end": b.end_dt.isoformat(),
                    "avg_price": round(b.avg_price, 3),
                    "energy_kwh": round(b.energy_kwh, 2),
                    "duration_min": b.duration_min,
                }
                for b in plan.plan
            ]
        return {
            "reason": d.reason,
            "in_slot": d.in_slot,
            "charger_mode": d.charger_mode,
            "charge_power_kw": round(d.charge_power, 2),
            "live_soc": d.live_soc,
            "soc_source": self.coordinator._last_soc_source,
            "target_soc": d.target_soc,
            "warning": d.warning,
            "next_slot": d.next_slot.isoformat() if d.next_slot else None,
            "observer_mode": d.observer,
            "actuated": d.actuated,
            "vehicle": self.runtime.active_vehicle,
            "mode": self.runtime.mode,
            "session_complete": self.runtime.session_complete,
            "plan": blocks,
            "updated": d.timestamp.isoformat(),
        }


class LiveSocSensor(EvcpEntity, SensorEntity):
    _attr_translation_key = "live_soc"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: EvcpCoordinator) -> None:
        super().__init__(coordinator, "live_soc")

    @property
    def native_value(self) -> float | None:
        d = self.coordinator.data
        return d.live_soc if d else None


class NextSlotSensor(EvcpEntity, SensorEntity):
    _attr_translation_key = "next_slot"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-start"

    def __init__(self, coordinator: EvcpCoordinator) -> None:
        super().__init__(coordinator, "next_slot")

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.next_slot_start()


class CurrentSlotEndSensor(EvcpEntity, SensorEntity):
    """Hvornår det nuværende ladeslot slutter (tomt hvis vi ikke er i et slot)."""

    _attr_translation_key = "current_slot_end"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-end"

    def __init__(self, coordinator: EvcpCoordinator) -> None:
        super().__init__(coordinator, "current_slot_end")

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.current_slot_end()


class EstimatedCostSensor(EvcpEntity, SensorEntity):
    _attr_translation_key = "estimated_cost"
    _attr_native_unit_of_measurement = "DKK"
    _attr_icon = "mdi:cash"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: EvcpCoordinator) -> None:
        super().__init__(coordinator, "estimated_cost")

    @property
    def native_value(self) -> float | None:
        plan = self.coordinator.plan_result
        return round(plan.estimated_cost, 2) if plan else None
