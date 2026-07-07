"""EV Charge Planner — Home Assistant custom integration.

Smart EV-ladning ud fra elpriser (Strømligning) med Zaptec-styring.
Porteret fra en Node-RED-automatisering.

Fase 2: coordinator (minut-loop) + config flow + entities.
Starter i observatør-tilstand — beregner og logger, men rører ikke laderen,
før observatør-tilstand slås fra.

Bemærk: Home Assistant importeres bevidst *ikke* på modul-niveau (kun bag
TYPE_CHECKING / inde i funktioner), så ``planner.py`` kan importeres og
unit-testes uden en HA-installation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .const import CONF_PRICE_SENSOR, DOMAIN, PLATFORMS

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: "HomeAssistant", entry: "ConfigEntry") -> bool:
    """Sæt en config entry op."""
    from homeassistant.helpers.event import async_track_state_change_event

    from .coordinator import EvcpCoordinator
    from .models import RuntimeStore

    store = RuntimeStore(hass, entry.entry_id)
    await store.load()

    coordinator = EvcpCoordinator(hass, entry, store)
    coordinator.recalculate()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Genberegn plan når prisdata opdateres
    price_sensor = entry.data.get(CONF_PRICE_SENSOR)
    if price_sensor:

        async def _on_price_change(_event) -> None:
            await coordinator.async_user_changed()

        entry.async_on_unload(
            async_track_state_change_event(hass, [price_sensor], _on_price_change)
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: "HomeAssistant", entry: "ConfigEntry") -> None:
    """Genindlæs ved ændrede options (fx nye biler)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: "HomeAssistant", entry: "ConfigEntry") -> bool:
    """Fjern en config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
