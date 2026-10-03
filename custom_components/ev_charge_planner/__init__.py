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

from . import guards
from .const import (
    CONF_CHARGE_CURRENT,
    CONF_CHARGE_POWER_SENSOR,
    CONF_CHARGER_MODE_SENSOR,
    CONF_CURRENT_ENTITY,
    DEFAULT_CHARGE_CURRENT,
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

# Kopi i config/www → serveres som /local, som frontend registrerer FØR
# webserveren starter (vores egen sti findes først når integrationen er oppe).
_LOCAL_DIR = DOMAIN
_LOCAL_URL = f"/local/{_LOCAL_DIR}/evcp-time-picker.js"


def _copy_card_to_www(src: str, www: str) -> None:
    """Kopiér kortet til config/www/<domain>/ (kun hvis indholdet er ændret)."""
    dest_dir = os.path.join(www, _LOCAL_DIR)
    dest = os.path.join(dest_dir, os.path.basename(src))
    with open(src, "rb") as f:
        data = f.read()
    try:
        with open(dest, "rb") as f:
            if f.read() == data:
                return
    except FileNotFoundError:
        pass
    os.makedirs(dest_dir, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(data)


async def _async_register_lovelace_resource(hass: "HomeAssistant", url: str) -> None:
    """Opret/opdatér kortet som dashboard-ressource (kun storage-mode)."""
    data = hass.data.get("lovelace")
    resources = getattr(data, "resources", None)
    if resources is None and isinstance(data, dict):
        resources = data.get("resources")
    if resources is None or not hasattr(resources, "async_create_item"):
        return  # YAML-mode: ressourcer kan ikke ændres herfra
    await resources.async_get_info()  # sikrer at samlingen er indlæst fra storage
    base = url.split("?", 1)[0]
    for item in resources.async_items():
        if str(item.get("url", "")).split("?", 1)[0] == base:
            if item.get("url") != url:
                await resources.async_update_item(item["id"], {"url": url})
            return
    await resources.async_create_item({"res_type": "module", "url": url})


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

    # Ekstra, opstarts-sikker indlæsning: sider hentet mens HA starter mangler
    # extra_js_url ovenfor → "Custom element doesn't exist". En dashboard-ressource
    # under /local kan hentes allerede dér. Kortet definerer sig kun én gang.
    try:
        await hass.async_add_executor_job(
            _copy_card_to_www, path, hass.config.path("www")
        )
        await _async_register_lovelace_resource(
            hass, f"{_LOCAL_URL}?v={version}" if version else _LOCAL_URL
        )
    except Exception:  # noqa: BLE001 — må aldrig vælte opsætningen
        _LOGGER.warning(
            "Kunne ikke registrere tidsvælger-kortet som dashboard-ressource",
            exc_info=True,
        )


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
    price_sensors = [
        e for e in (coordinator.price_sensor(), *coordinator.tomorrow_sensor_candidates()) if e
    ]
    if price_sensors:

        async def _on_price_change(_event) -> None:
            await coordinator.async_user_changed(urgent=False)

        entry.async_on_unload(
            async_track_state_change_event(hass, price_sensors, _on_price_change)
        )

    # Reagér straks når laderen skifter mode/effekt, eller når strøm-entiteten melder
    # en ny værdi (bekræftelse af vores kald / ændring udefra), i stedet for at vente
    # på næste 60-sekunders tick. Debounced request_refresh (leading-edge, ~10 s
    # cooldown), så en burst af ændringer ikke giver en beslutnings-storm.
    charger_signals = [
        entry.data.get(CONF_CHARGER_MODE_SENSOR),
        entry.data.get(CONF_CHARGE_POWER_SENSOR),
        coordinator.current_entity(),
        guards.charger_max_current_entity(entry.data.get(CONF_CHARGER_MODE_SENSOR)),
        entry.options.get(CONF_CURRENT_ENTITY) or entry.data.get(CONF_CURRENT_ENTITY),
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


def _restore_normal_current(hass: "HomeAssistant", entry: "ConfigEntry") -> None:
    """Sæt laderen tilbage til normal ladestrøm, så den virker uden planneren."""
    derived = guards.charger_max_current_entity(entry.data.get(CONF_CHARGER_MODE_SENSOR))
    if derived and hass.states.get(derived) is not None:
        entity = derived
    else:
        entity = entry.options.get(CONF_CURRENT_ENTITY) or entry.data.get(CONF_CURRENT_ENTITY)
    amps = entry.options.get(
        CONF_CHARGE_CURRENT, entry.data.get(CONF_CHARGE_CURRENT, DEFAULT_CHARGE_CURRENT)
    )
    if not entity or hass.states.get(entity) is None:
        return
    _LOGGER.info("Planneren er slået fra — sætter %s til %s A", entity, amps)
    hass.async_create_task(
        hass.services.async_call(
            "number", "set_value", {"entity_id": entity, "value": float(amps)}
        )
    )


async def async_unload_entry(hass: "HomeAssistant", entry: "ConfigEntry") -> bool:
    """Fjern en config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        if coordinator is not None:
            coordinator.async_close()
            # Slået fra af brugeren (ikke en genindlæsning): laderen skal virke normalt
            # igen — ellers ville den blive stående på 0 A. Ikke i observatør-tilstand,
            # hvor planneren aldrig har rørt strømmen.
            if entry.disabled_by is not None and not coordinator.runtime.observer_mode:
                _restore_normal_current(hass, entry)
    return unload_ok


async def async_remove_entry(hass: "HomeAssistant", entry: "ConfigEntry") -> None:
    """Integrationen slettes: laderen skal virke normalt igen."""
    from .models import RuntimeStore

    store = RuntimeStore(hass, entry.entry_id)
    runtime = await store.load()
    if not runtime.observer_mode:
        _restore_normal_current(hass, entry)
