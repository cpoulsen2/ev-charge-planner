"""Coordinator: minut-loop der beslutter om og hvordan der skal lades.

Porteret fra Node-RED-noden "🔍 Scheduler: Skal vi lade nu?" inkl.:
  - ramp-safe car-side stop detection (tilstandsmaskine)
  - bilskifte-reset (connected_finished → connected_requesting uden om disconnected)
  - autorisation baseret på charger_mode == connected_requesting
  - "ladning startet"-notifikation

Kører som standard i OBSERVATØR-tilstand: beslutning beregnes og logges,
men laderen røres ikke, før brugeren slår observatør-tilstand fra.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import planner
from .const import (
    ACT_BLOCKED,
    ACT_CHARGING,
    ACT_IDLE,
    ACT_PAUSE,
    ACT_START,
    ACT_TARGET_REACHED,
    ACT_WAITING,
    CAR_SIDE_STOP_TICKS,
    CHARGE_POWER_THRESHOLD_KW,
    CHOOSE_VEHICLE,
    CM_CHARGING,
    CM_DISCONNECTED,
    CM_FINISHED,
    CM_REQUESTING,
    CONF_AUTHORIZE_BUTTON,
    CONF_CHARGE_POWER_SENSOR,
    CONF_CHARGER_MODE_SENSOR,
    CONF_NOTIFY_SERVICE,
    CONF_PRICE_SENSOR,
    CONF_RESUME_BUTTON,
    CONF_SESSION_ENERGY_SENSOR,
    CONF_STOP_BUTTON,
    CONF_VEHICLES,
    EVENT_ACTION,
    GUEST_VEHICLE,
    MODE_STANDARD,
    STANDARD_DEADLINE_HOUR,
    UPDATE_INTERVAL,
)
from .models import Runtime, RuntimeStore, Vehicle

_LOGGER = logging.getLogger(__name__)


@dataclass
class Decision:
    """Resultatet af én scheduler-kørsel — føder status-sensoren."""

    action: str
    reason: str
    in_slot: bool = False
    charger_mode: str = "unknown"
    charge_power: float = 0.0
    live_soc: float = 0.0
    target_soc: float = 0.0
    warning: str = "none"
    next_slot: datetime | None = None
    actuated: bool = False
    observer: bool = True
    timestamp: datetime = field(default_factory=dt_util.utcnow)


# Tilstande hvor en tidligere session er "død" og en ny kan begynde
_NEW_SESSION_FROM = {CM_DISCONNECTED, "unavailable", "unknown", None}


class EvcpCoordinator(DataUpdateCoordinator[Decision]):
    """Styrer planberegning og lade-beslutninger."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        store: RuntimeStore,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="EV Charge Planner",
            update_interval=UPDATE_INTERVAL,
        )
        self.entry = entry
        self.store = store
        self.runtime: Runtime = store.runtime
        self.plan_result: planner.PlanResult | None = None
        self._prev_charger_mode: str | None = None

    # ---------- persistens ----------

    async def async_save(self) -> None:
        await self.store.save()

    async def async_user_changed(self) -> None:
        """Kaldes af kontrol-entities når brugeren ændrer en værdi."""
        self.recalculate()
        await self.async_save()
        await self.async_request_refresh()

    # ---------- aflæsning af eksterne sensorer ----------

    def _cfg(self, key: str) -> str | None:
        return self.entry.data.get(key)

    def _get_state(self, entity_id: str | None) -> str | None:
        if not entity_id:
            return None
        st = self.hass.states.get(entity_id)
        return st.state if st else None

    def _get_float(self, entity_id: str | None) -> float | None:
        val = self._get_state(entity_id)
        if val in (None, "unknown", "unavailable", ""):
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    def _charger_mode(self) -> str:
        return self._get_state(self._cfg(CONF_CHARGER_MODE_SENSOR)) or "unknown"

    def _charge_power(self) -> float:
        return self._get_float(self._cfg(CONF_CHARGE_POWER_SENSOR)) or 0.0

    def _session_energy(self) -> float:
        return self._get_float(self._cfg(CONF_SESSION_ENERGY_SENSOR)) or 0.0

    def _vehicles(self) -> list[Vehicle]:
        raw = self.entry.options.get(CONF_VEHICLES, [])
        return [Vehicle.from_dict(v) for v in raw]

    def _capacity_for(self, name: str) -> float:
        if name == GUEST_VEHICLE:
            return self.runtime.guest_capacity or 60.0
        for v in self._vehicles():
            if v.name == name:
                return v.capacity_kwh
        return 77.0  # fallback

    def _soc_sensor_for(self, name: str) -> str | None:
        for v in self._vehicles():
            if v.name == name:
                return v.soc_sensor
        return None

    def _prices(self) -> tuple[list[dict], list[dict]]:
        st = self.hass.states.get(self._cfg(CONF_PRICE_SENSOR) or "")
        if not st:
            return [], []
        attrs = st.attributes
        raw_today = attrs.get("prices_today") or attrs.get("raw_today") or []
        raw_tomorrow = attrs.get("prices_tomorrow") or attrs.get("raw_tomorrow") or []
        return list(raw_today), list(raw_tomorrow)

    # ---------- deadline / live SoC ----------

    def _deadline_ms(self) -> int | None:
        rt = self.runtime
        if rt.mode == MODE_STANDARD:
            now = dt_util.now()  # lokal, aware
            deadline = now.replace(
                hour=STANDARD_DEADLINE_HOUR, minute=0, second=0, microsecond=0
            )
            if deadline <= now:
                deadline = deadline + timedelta(days=1)
            return planner.to_ms(deadline)
        dep = rt.departure
        if dep is None:
            return None
        if dep.tzinfo is None:
            dep = dt_util.as_local(dep)
        return planner.to_ms(dep)

    def _live_soc(self) -> float:
        rt = self.runtime
        # Hvis bilen har en SoC-sensor og den giver en gyldig værdi, brug den
        sensor = self._soc_sensor_for(rt.active_vehicle)
        if sensor:
            val = self._get_float(sensor)
            if val is not None:
                return round(val)
        capacity = self._capacity_for(rt.active_vehicle)
        session = max(0.0, self._session_energy() - rt.session_baseline_kwh)
        return min(100, round(rt.current_soc + (session / capacity * 100)))

    # ---------- planberegning ----------

    def recalculate(self) -> None:
        """Genberegn ladeplanen ud fra nuværende kontroller og priser."""
        rt = self.runtime
        if rt.active_vehicle == CHOOSE_VEHICLE:
            self.plan_result = None
            return
        deadline_ms = self._deadline_ms()
        if deadline_ms is None:
            self.plan_result = None
            _LOGGER.debug("Ingen gyldig deadline — springer planberegning over")
            return
        raw_today, raw_tomorrow = self._prices()
        self.plan_result = planner.compute_plan(
            now_ms=planner.to_ms(dt_util.utcnow()),
            deadline_ms=deadline_ms,
            target_pct=rt.target_soc,
            current_soc=self._live_soc(),
            capacity_kwh=self._capacity_for(rt.active_vehicle),
            power_kw=rt.charge_power,
            raw_today=raw_today,
            raw_tomorrow=raw_tomorrow,
            min_block_mins=int(self.entry.options.get("min_block_minutes", 0)),
        )
        _LOGGER.debug(
            "Plan genberegnet: %s blokke, advarsel=%s",
            len(self.plan_result.plan),
            self.plan_result.warning,
        )

    # ---------- hoved-loop ----------

    async def _async_update_data(self) -> Decision:
        rt = self.runtime
        mode = self._charger_mode()

        # Håndtér mode-overgange (ny session / bilskift)
        await self._handle_mode_transition(self._prev_charger_mode, mode)
        self._prev_charger_mode = mode

        decision = self._decide(mode)

        # Notifikation: ladning faktisk startet
        await self._maybe_notify_charge_start()

        # Log altid beslutningen (fejlsøgning)
        self.hass.bus.async_fire(
            EVENT_ACTION,
            {
                "action": decision.action,
                "reason": decision.reason,
                "observer": decision.observer,
                "actuated": decision.actuated,
                "charger_mode": decision.charger_mode,
                "live_soc": decision.live_soc,
            },
        )
        return decision

    def _decide(self, mode: str) -> Decision:
        """Ren beslutningslogik (uden aktuering) → returnerer Decision.

        Aktuering sker via de metoder decision peger på; her sætter vi kun
        beslutningen og kalder aktuering hvor relevant (respekterer observatør).
        """
        rt = self.runtime
        observer = rt.observer_mode
        power = self._charge_power()
        really_charging = mode == CM_CHARGING and power > CHARGE_POWER_THRESHOLD_KW

        def dec(action: str, reason: str, **kw) -> Decision:
            return Decision(
                action=action,
                reason=reason,
                charger_mode=mode,
                charge_power=power,
                target_soc=rt.target_soc,
                observer=observer,
                **kw,
            )

        # 1) Bil valgt?
        if rt.active_vehicle == CHOOSE_VEHICLE:
            return dec(ACT_BLOCKED, "Vælg en bil i menuen")

        # 2) Master-kontakt?
        if not rt.enabled:
            return dec(ACT_IDLE, "Automatik slået fra (aktivér for at lade)")

        # 3) Frakoblet?
        if mode == CM_DISCONNECTED:
            return dec(ACT_IDLE, "Laderen er frakoblet")

        live_soc = self._live_soc()
        target = rt.target_soc

        # 4) Plan + slot
        plan = self.plan_result
        now_ms = planner.to_ms(dt_util.utcnow())
        in_slot = bool(
            plan
            and plan.plan
            and any(b.start_ms <= now_ms < b.end_ms for b in plan.plan)
        )
        should_be_charging = rt.force_charge or in_slot

        # 5) Ramp-safe car-side stop tilstandsmaskine
        no_power = mode != CM_DISCONNECTED and power < CHARGE_POWER_THRESHOLD_KW
        if not should_be_charging:
            rt.charge_state = "idle"
            rt.zero_power_ticks = 0
        elif really_charging:
            rt.charge_state = "charging"
            rt.zero_power_ticks = 0
        elif rt.charge_state == "charging":
            rt.zero_power_ticks += 1
        else:
            rt.charge_state = "ramping"
            rt.zero_power_ticks = 0

        if rt.zero_power_ticks >= CAR_SIDE_STOP_TICKS:
            rt.zero_power_ticks = 0
            rt.charge_state = "idle"
            self._on_target_reached(target, car_side=True)
            return dec(
                ACT_TARGET_REACHED,
                f"Bilen stoppede selv ved {target:.0f}%",
                live_soc=target,
                actuated=not observer,
            )

        # 6) Mål nået?
        if live_soc >= target:
            self._on_target_reached(target, car_side=False)
            return dec(
                ACT_TARGET_REACHED,
                f"Mål nået ({live_soc:.0f}% ≥ {target:.0f}%)",
                live_soc=live_soc,
                actuated=not observer,
            )

        # 7) Force charge
        if rt.force_charge:
            if not really_charging:
                self._do_start(mode, observer)
                return dec(
                    ACT_START,
                    "Lad straks (force) — starter ladning",
                    live_soc=live_soc,
                    actuated=not observer,
                )
            return dec(
                ACT_CHARGING,
                f"Lad straks (force) — lader {power:.1f} kW",
                live_soc=live_soc,
            )

        # 8) Ingen plan?
        if not plan or not plan.plan:
            reason = "Ingen ladeplan (afventer priser/valg)"
            if plan and plan.warning == planner.WARN_ALREADY_AT_TARGET:
                reason = "Allerede ved mål"
            elif plan and plan.warning == planner.WARN_NO_PRICES:
                reason = "Ingen prisdata i tidsvinduet"
            return dec(ACT_WAITING, reason, live_soc=live_soc)

        # 9) I slot?
        if in_slot:
            if not really_charging:
                self._do_start(mode, observer)
                return dec(
                    ACT_START,
                    "I ladeslot — starter ladning",
                    in_slot=True,
                    live_soc=live_soc,
                    actuated=not observer,
                )
            return dec(
                ACT_CHARGING,
                f"I ladeslot — lader {live_soc:.0f}%",
                in_slot=True,
                live_soc=live_soc,
            )

        # 10) Uden for slot
        if really_charging:
            self._do_stop(observer)
            return dec(
                ACT_PAUSE,
                "Uden for slot — pauser ladning",
                live_soc=live_soc,
                actuated=not observer,
            )

        nxt = next((b for b in plan.plan if b.start_ms > now_ms), None)
        if nxt:
            mins = round((nxt.start_ms - now_ms) / 60000)
            return dec(
                ACT_WAITING,
                f"Næste slot om {mins} min",
                live_soc=live_soc,
                next_slot=nxt.start_dt,
            )
        return dec(ACT_WAITING, "Alle slots er færdige", live_soc=live_soc)

    # ---------- aktuering ----------

    def _do_start(self, mode: str, observer: bool) -> None:
        if observer:
            _LOGGER.info("[OBSERVER] Ville starte ladning (mode=%s)", mode)
            return
        self.hass.async_create_task(self._start_charging(mode))

    async def _start_charging(self, mode: str) -> None:
        # Autorisér kun hvis laderen beder om det
        if mode == CM_REQUESTING:
            await self._press(CONF_AUTHORIZE_BUTTON)
            await asyncio.sleep(5)
        await self._press(CONF_RESUME_BUTTON)

    def _do_stop(self, observer: bool) -> None:
        if observer:
            _LOGGER.info("[OBSERVER] Ville stoppe/pause ladning")
            return
        self.hass.async_create_task(self._press(CONF_STOP_BUTTON))

    async def _press(self, conf_key: str) -> None:
        entity_id = self._cfg(conf_key)
        if not entity_id:
            return
        await self.hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=False
        )

    def _on_target_reached(self, target: float, car_side: bool) -> None:
        rt = self.runtime
        rt.session_complete = True
        rt.force_charge = False
        rt.current_soc = target
        rt.session_baseline_kwh = self._session_energy()
        rt.enabled = False
        if not rt.observer_mode:
            self.hass.async_create_task(self._press(CONF_STOP_BUTTON))
            self._notify(
                "🔋 Ladning færdig",
                ("Bilen stoppede selv — " if car_side else "Klar — ")
                + f"{target:.0f}%",
            )
        self.hass.async_create_task(self.async_save())

    # ---------- notifikation ----------

    async def _maybe_notify_charge_start(self) -> None:
        rt = self.runtime
        power = self._charge_power()
        if power > CHARGE_POWER_THRESHOLD_KW and not rt.charge_start_notified:
            rt.charge_start_notified = True
            if not rt.observer_mode:
                self._notify(
                    "⚡ Ladning startet",
                    f"{rt.active_vehicle} lader nu — {self._live_soc():.0f}% ({power:.1f} kW)",
                )
            await self.async_save()

    def _notify(self, title: str, message: str) -> None:
        service = self._cfg(CONF_NOTIFY_SERVICE)
        if not service or "." not in service:
            return
        domain, name = service.split(".", 1)
        self.hass.async_create_task(
            self.hass.services.async_call(
                domain, name, {"title": title, "message": message}, blocking=False
            )
        )

    # ---------- mode-overgange (ny session / bilskift) ----------

    async def _handle_mode_transition(self, prev: str | None, now: str) -> None:
        if prev == now:
            return
        rt = self.runtime
        is_fresh_plugin = prev in _NEW_SESSION_FROM and now in (
            CM_REQUESTING,
            CM_CHARGING,
        )
        is_car_swap = prev == CM_FINISHED and now == CM_REQUESTING
        if is_fresh_plugin or is_car_swap:
            _LOGGER.info("Ny session (%s → %s) — nulstiller", prev, now)
            rt.session_complete = False
            rt.force_charge = False
            rt.charge_state = "idle"
            rt.zero_power_ticks = 0
            rt.charge_start_notified = False
            rt.session_baseline_kwh = self._session_energy()
            rt.active_vehicle = CHOOSE_VEHICLE
            rt.enabled = False
            self.plan_result = None
            await self.async_save()
