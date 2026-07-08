"""Datamodeller og persistent runtime-tilstand for EV Charge Planner."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CHOOSE_VEHICLE,
    DEFAULT_GUEST_CAPACITY_KWH,
    DEFAULT_POWER_KW,
    DEFAULT_TARGET_SOC,
    DOMAIN,
    MODE_STANDARD,
)

STORAGE_VERSION = 1


@dataclass
class Vehicle:
    """En bruger-tilføjet bil."""

    name: str
    capacity_kwh: float
    soc_sensor: str | None = None  # valgfri: aflæs SoC automatisk hvis sat

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Vehicle":
        return cls(
            name=data["name"],
            capacity_kwh=float(data["capacity_kwh"]),
            soc_sensor=data.get("soc_sensor") or None,
        )


@dataclass
class Runtime:
    """Al foranderlig tilstand — brugerkontroller + intern scheduler-tilstand.

    Persisteres via Home Assistants Store, så den overlever genstart.
    """

    # --- Brugerkontroller ---
    active_vehicle: str = CHOOSE_VEHICLE
    mode: str = MODE_STANDARD
    current_soc: float = 0.0
    target_soc: float = DEFAULT_TARGET_SOC
    guest_capacity: float = DEFAULT_GUEST_CAPACITY_KWH
    charge_power: float = DEFAULT_POWER_KW
    departure_iso: str | None = None  # afrejsetid (Afgang-mode), ISO-streng
    enabled: bool = False  # master-kontakt (svarer til charger_switch)
    observer_mode: bool = True  # True = beregn+log men rør IKKE laderen
    force_charge: bool = False  # "lad straks" — ignorér plan

    # --- Intern scheduler-tilstand ---
    session_baseline_kwh: float = 0.0
    charge_state: str = "idle"  # idle | ramping | charging
    zero_power_ticks: int = 0
    session_complete: bool = False
    charge_start_notified: bool = False
    not_enough_time_notified: bool = False
    last_plan_signature: str = ""
    # Sidste gyldige SoC-aflæsning pr. sensor (bruges når bilen sover og
    # sensoren bliver "unavailable" — sidste kendte værdi er stadig korrekt)
    soc_cache: dict = field(default_factory=dict)

    @property
    def departure(self) -> datetime | None:
        if not self.departure_iso:
            return None
        try:
            return dt_util.parse_datetime(self.departure_iso)
        except (ValueError, TypeError):
            return None

    @departure.setter
    def departure(self, value: datetime | None) -> None:
        self.departure_iso = value.isoformat() if value else None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Runtime":
        rt = cls()
        for key, value in data.items():
            if hasattr(rt, key):
                setattr(rt, key, value)
        return rt


class RuntimeStore:
    """Indpakning omkring HA's Store for at gemme/hente Runtime."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry_id}")
        self.runtime = Runtime()

    async def load(self) -> Runtime:
        data = await self._store.async_load()
        if data:
            self.runtime = Runtime.from_dict(data)
        return self.runtime

    async def save(self) -> None:
        await self._store.async_save(self.runtime.to_dict())
