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
import os
from typing import TYPE_CHECKING

from .const import (
    CONF_CHARGE_POWER_SENSOR,
    CONF_CHARGER_MODE_SENSOR,
    CONF_PRICE_SENSOR,
    DOMAIN,
    PLATFORMS,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Frontend-kort der serveres direkte af integrationen (ingen manuel
# Lovelace-ressource nødvendig — loades automatisk efter genstart)
_CARD_FILE = "www/evcp-time-picker.js"


_CARD_URL = f"/{DOMAIN}/evcp-time-picker.js"


async def _async_register_frontend(hass: "HomeAssistant") -> None:
    """Registrér og auto-indlæs det medfølgende tidsvælger-kort.

    Kører kun én gang pr. HA-opstart (uanset antal config entries).

    STABIL fil-sti + versions-query (…evcp-time-picker.js?v=<version>):
    - Selv en forældet, service-worker-cachet app-skal peger på en URL der
      ALTID findes → aldrig 404 → aldrig "Configuration error" (i modsætning
      til en versioneret sti, hvor en gammel URL ikke længere serveres).
    - Query'en skifter ved hver opdatering, så friske skaller henter ny JS.
    - cache_headers=False, så indholdet revalideres.
    """
    if hass.data.get(f"{DOMAIN}_frontend"):
        return
    hass.data[f"{DOMAIN}_frontend"] = True

    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import StaticPathConfig
    from homeassistant.loader import async_get_integration

    version = ""
    try:
        integration = await async_get_integration(hass, DOMAIN)
        version = str(integration.version or "").replace("/", "_")
    except Exception:  # noqa: BLE001 — versionen er kun til cache-busting
        pass

    path = os.path.join(os.path.dirname(__file__), _CARD_FILE)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(_CARD_URL, path, False)]
    )
    add_extra_js_url(hass, f"{_CARD_URL}?v={version}" if version else _CARD_URL)


async def async_setup_entry(hass: "HomeAssistant", entry: "ConfigEntry") -> bool:
    """Sæt en config entry op."""
    from homeassistant.helpers.event import async_track_state_change_event

    from .coordinator import EvcpCoordinator
    from .models import RuntimeStore

    await _async_register_frontend(hass)

    store = RuntimeStore(hass, entry.entry_id)
    await store.load()

    coordinator = EvcpCoordinator(hass, entry, store)
    # Gendan gemt plan (så en HA-genstart midt i en ladning ikke mister ladeslots);
    # genberegn kun hvis der ingen gemt plan er
    coordinator.restore_plan()
    if coordinator.plan_result is None:
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

    # Reagér straks når laderen skifter mode/effekt i stedet for at vente på næste
    # 60-sekunders tick. Kritisk for start-latens: efter et resume-tryk skifter
    # laderen finished→requesting, hvor authorize først er muligt — uden denne lytter
    # ventede _decide() op til 60 s. Bruger den debouncede request_refresh (leading-edge,
    # ~10 s cooldown), så en burst af effekt-ændringer under ladning ikke giver en
    # beslutnings-storm. Ingen genberegning af planen — kun ny beslutning/aktuering.
    charger_signals = [
        entry.data.get(CONF_CHARGER_MODE_SENSOR),
        entry.data.get(CONF_CHARGE_POWER_SENSOR),
    ]
    charger_signals = [e for e in charger_signals if e]
    if charger_signals:

        async def _on_charger_change(_event) -> None:
            await coordinator.async_request_refresh()

        entry.async_on_unload(
            async_track_state_change_event(hass, charger_signals, _on_charger_change)
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
