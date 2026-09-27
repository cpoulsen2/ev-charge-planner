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

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import guards, planner
from .const import (
    ACT_BLOCKED,
    ACT_CHARGING,
    ACT_IDLE,
    ACT_PAUSE,
    ACT_START,
    ACT_TARGET_REACHED,
    ACT_WAITING,
    AUTHORIZE_CONFIRM_TIMEOUT,
    AUTHORIZE_MIN_INTERVAL,
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
    CONF_DEAUTHORIZE_BUTTON,
    CONF_NOTIFY_SERVICE,
    CONF_PRICE_SENSOR,
    CONF_RESUME_BUTTON,
    CONF_SESSION_ENERGY_SENSOR,
    CONF_NOTIFY_TARGETS,
    CONF_STOP_BUTTON,
    CONF_TOMORROW_SENSOR,
    CONF_VEHICLES,
    DEAUTHORIZE_GRACE_PERIOD,
    DEAUTHORIZE_MAX_WAIT,
    DEAUTHORIZE_SETTLE,
    EVENT_ACTION,
    NOTIFY_CLICK_PATH,
    NOTIFY_DEFAULTS,
    GUEST_VEHICLE,
    MODE_DEPARTURE,
    MODE_STANDARD,
    RESCUE_MAX_ROUNDS,
    SLOT_START_DELAY,
    SLOW_PRESS_WARNING,
    STANDARD_DEADLINE_HOUR,
    START_FAILED_TIMEOUT,
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


# En ny session (fresh plug-in) tælles KUN fra en reel frakobling.
# Transiente/opstarts-tilstande (unknown/unavailable/None) må IKKE nulstille en
# igangværende session — ellers glemmer en HA-genstart bil/plan midt i en ladning.
_NEW_SESSION_FROM = {CM_DISCONNECTED}


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
        self._prev_charger_mode: str | None = store.runtime.prev_charger_mode or None
        self._soc_cache_dirty = False
        self._last_soc_source = "manual"
        # Hvornår laderen gik ind i sin nuværende mode (til "stabil requesting").
        self._mode_since: datetime = dt_util.utcnow()
        # Zaptec-kald der stadig kører ("authorize"/"deauthorize") — integrationen
        # kan selv gentage et kald i op til ~100 s, så vi venter på at det er færdigt.
        self._press_inflight: str | None = None
        self._wake_unsub = None
        self._wake_at: datetime | None = None
        self._closed = False
        rt = self.runtime
        if rt.authorize_done and not rt.authorize_sent_iso and rt.last_authorize_iso:
            # Genindlæst mens et authorize-kald kørte: regn med Zaptecs fulde
            # gentagelses-vindue (~100 s), før en redning må sende deauthorize.
            last = dt_util.parse_datetime(rt.last_authorize_iso)
            if last is not None:
                rt.authorize_sent_iso = (last + timedelta(seconds=120)).isoformat()
        if rt.rescue_phase and not rt.rescue_deauth_done_iso and rt.rescue_deauth_iso:
            last = dt_util.parse_datetime(rt.rescue_deauth_iso)
            if last is not None:
                rt.rescue_deauth_done_iso = (last + timedelta(seconds=120)).isoformat()
        # Authorize-tilstand ligger nu i Runtime (persisteret), så en reload midt i
        # en requesting-episode ikke udløser et nyt authorize-tryk.

    # ---------- persistens ----------

    async def async_save(self) -> None:
        await self.store.save()

    async def async_user_changed(self) -> None:
        """Kaldes af kontrol-entities når brugeren ændrer en værdi.

        Bruger async_refresh() (øjeblikkelig) i stedet for async_request_refresh()
        (debounced), så dashboardet opdaterer straks ved fx bilskift.
        """
        self.recalculate()
        await self.async_save()
        await self.async_refresh()

    async def async_stop_charging(self) -> None:
        """Stop/annullér ladning nu: tryk Zaptec-stop og slå automatik fra.

        Automatikken slås fra så den ikke genstarter; brugeren aktiverer igen
        for at følge planen. Respekterer observatør-tilstand (rører kun laderen
        når observatør er slået fra)."""
        rt = self.runtime
        rt.force_charge = False
        rt.enabled = False
        if not rt.observer_mode:
            await self._press(CONF_STOP_BUTTON)
        await self.async_save()
        await self.async_refresh()

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

    def active_vehicle_has_sensor(self) -> bool:
        """True hvis den valgte bil har en SoC-sensor (så skyderen er unødvendig)."""
        return bool(self._soc_sensor_for(self.runtime.active_vehicle))

    def _soc_live_for(self, name: str) -> bool:
        """Opdaterer bilens SoC-sensor under ladning? (False = anker + beregning)."""
        for v in self._vehicles():
            if v.name == name:
                return v.soc_live
        return True

    def active_vehicle_uses_anchor(self) -> bool:
        """True hvis bilen har en sensor der IKKE opdaterer under ladning (VW-hybrid).

        Så bruges sensoren som anker + tilført energi, og batteri-skyderen kan
        vises som valgfri overstyring.
        """
        v = self.runtime.active_vehicle
        return bool(self._soc_sensor_for(v)) and not self._soc_live_for(v)

    def _has_charged_this_session(self) -> bool:
        """Har vi faktisk ladet i denne session? Bruges så 'mål nået' ikke
        fejludløses blot fordi man vælger en bil der allerede er fuld."""
        return (
            self.runtime.charge_state == "charging"
            or self._session_energy() > self.runtime.session_baseline_kwh + 0.01
        )

    def on_vehicle_changed(self) -> None:
        """Nulstil session-anker når brugeren skifter bil, så den nye bil starter
        rent: ingen arvet tilført energi (korrekt live-SoC) og ingen falsk 'mål nået'."""
        rt = self.runtime
        rt.session_baseline_kwh = self._session_energy()
        rt.charge_state = "idle"
        rt.zero_power_ticks = 0

    def _tomorrow_sensor_id(self) -> str | None:
        """Sensor med morgendagens priser — eksplicit konfigureret eller auto-udledt.

        Strømligning lægger morgendagens priser på en separat sensor
        (fx sensor.stromligning_adjusted_price_vat →
         binary_sensor.stromligning_adjusted_price_vat_tomorrow).
        """
        configured = self._cfg(CONF_TOMORROW_SENSOR)
        if configured:
            return configured
        price = self._cfg(CONF_PRICE_SENSOR) or ""
        if price.startswith("sensor."):
            obj = price.split(".", 1)[1]
            return f"binary_sensor.{obj}_tomorrow"
        return None

    def _prices(self) -> tuple[list[dict], list[dict]]:
        st = self.hass.states.get(self._cfg(CONF_PRICE_SENSOR) or "")
        if not st:
            return [], []
        attrs = st.attributes
        raw_today = attrs.get("prices_today") or attrs.get("raw_today") or []
        raw_tomorrow = attrs.get("prices_tomorrow") or attrs.get("raw_tomorrow") or []

        # Fald tilbage til den separate "tomorrow"-sensor hvis hovedsensoren ikke
        # selv har morgendagens priser
        if not raw_tomorrow:
            tmr_id = self._tomorrow_sensor_id()
            if tmr_id:
                tmr = self.hass.states.get(tmr_id)
                if tmr and tmr.state == "on":
                    raw_tomorrow = (
                        tmr.attributes.get("prices_tomorrow")
                        or tmr.attributes.get("raw_tomorrow")
                        or []
                    )
        return list(raw_today), list(raw_tomorrow)

    # ---------- deadline / live SoC ----------

    def maintain_departure(self) -> bool:
        """Hold afrejse-datoen i fremtiden. Er den tom eller passeret, sættes den
        til NÆSTE kl. 07:00 (dvs. i morgen tidlig når man sætter den om aftenen).
        Returnerer True hvis værdien blev ændret."""
        now = dt_util.now()  # lokal, aware
        dep = dt_util.parse_datetime(self.runtime.departure_iso) if self.runtime.departure_iso else None
        if dep is not None and dep.tzinfo is None:
            dep = dt_util.as_local(dep)
        if dep is None or dep <= now:
            nxt = now.replace(hour=7, minute=0, second=0, microsecond=0)
            if nxt <= now:
                nxt = nxt + timedelta(days=1)
            self.runtime.departure_iso = nxt.isoformat()
            return True
        return False

    def _window_start_ms(self) -> int | None:
        """Ladevindue: tidligst-start (kun i Afgang når slået til). None = ingen grænse."""
        rt = self.runtime
        if rt.mode != MODE_DEPARTURE or not rt.use_earliest_start or not rt.earliest_start_iso:
            return None
        dep = dt_util.parse_datetime(rt.earliest_start_iso)
        if dep is None:
            return None
        if dep.tzinfo is None:
            dep = dt_util.as_local(dep)
        return planner.to_ms(dep)

    def _deadline_ms(self) -> int | None:
        rt = self.runtime
        now = dt_util.now()  # lokal, aware
        if rt.mode == MODE_STANDARD:
            deadline = now.replace(
                hour=STANDARD_DEADLINE_HOUR, minute=0, second=0, microsecond=0
            )
            if deadline <= now:
                deadline = deadline + timedelta(days=1)
            return planner.to_ms(deadline)
        # Afgang: brug den (auto-vedligeholdte) afrejse-dato+tid
        self.maintain_departure()
        dep = dt_util.parse_datetime(rt.departure_iso) if rt.departure_iso else None
        if dep is None:
            return None
        if dep.tzinfo is None:
            dep = dt_util.as_local(dep)
        return planner.to_ms(dep)

    def next_slot_start(self) -> datetime | None:
        """Starttidspunkt for næste kommende ladeblok (uanset om vi er i et slot nu)."""
        pr = self.plan_result
        if not pr or not pr.plan:
            return None
        now_ms = planner.to_ms(dt_util.utcnow())
        nxt = next((b for b in pr.plan if b.start_ms > now_ms), None)
        return nxt.start_dt if nxt else None

    def charge_time_minutes(self) -> int | None:
        """Hvor lang tid (min) bilen skal lade for at nå målet ved nuværende effekt."""
        rt = self.runtime
        if rt.active_vehicle == CHOOSE_VEHICLE:
            return None
        capacity = self._capacity_for(rt.active_vehicle)
        power = rt.charge_power or 11
        energy_needed = capacity * max(0.0, rt.target_soc - self._live_soc()) / 100
        if energy_needed <= 0 or power <= 0:
            return 0
        return round(energy_needed / power * 60)

    def current_slot_end(self) -> datetime | None:
        """Sluttidspunkt for det slot vi er i lige nu (None hvis ikke i et slot)."""
        pr = self.plan_result
        if not pr or not pr.plan:
            return None
        now_ms = planner.to_ms(dt_util.utcnow())
        cur = next(
            (b for b in pr.plan if b.start_ms <= now_ms < b.end_ms), None
        )
        return cur.end_dt if cur else None

    def _live_soc(self) -> float:
        rt = self.runtime
        sensor = self._soc_sensor_for(rt.active_vehicle)
        if sensor:
            val = self._get_float(sensor)
            if self._soc_live_for(rt.active_vehicle):
                # Live-sensor (Tesla): brug direkte, med cache-fallback ved dvale
                if val is not None:
                    if rt.soc_cache.get(sensor) != val:
                        rt.soc_cache[sensor] = val
                        self._soc_cache_dirty = True
                    self._last_soc_source = f"sensor:{sensor}"
                    return round(val)
                cached = rt.soc_cache.get(sensor)
                if cached is not None:
                    self._last_soc_source = f"sensor-cached:{sensor}"
                    return round(cached)
                # ingen aflæsning endnu → fald til manuel nedenfor
            else:
                # Hybrid (VW): sensoren opdaterer kun ved kørsel. Gen-ankér når den
                # giver en ny (frisk) værdi; ellers anker + tilført energi.
                if val is not None and rt.soc_cache.get(sensor) != val:
                    rt.soc_cache[sensor] = val
                    self._soc_cache_dirty = True
                    rt.current_soc = val  # nyt anker
                    rt.session_baseline_kwh = self._session_energy()
                capacity = self._capacity_for(rt.active_vehicle)
                session = max(0.0, self._session_energy() - rt.session_baseline_kwh)
                self._last_soc_source = f"sensor-anchor:{sensor}"
                return min(100, round(rt.current_soc + (session / capacity * 100)))
        # Manuel (ingen sensor) — eller live-sensor uden aflæsning endnu
        capacity = self._capacity_for(rt.active_vehicle)
        session = max(0.0, self._session_energy() - rt.session_baseline_kwh)
        self._last_soc_source = "manual"
        return min(100, round(rt.current_soc + (session / capacity * 100)))

    # ---------- planberegning ----------

    def _set_plan(self, pr: planner.PlanResult | None) -> None:
        """Sæt planen og hold den persisterede kopi i sync."""
        self.plan_result = pr
        self.runtime.plan_data = planner.plan_result_to_dict(pr) if pr else {}

    def restore_plan(self) -> None:
        """Genskab planen fra gemte data (ved opstart)."""
        data = self.runtime.plan_data
        if data and data.get("plan"):
            self.plan_result = planner.plan_result_from_dict(data)
            _LOGGER.debug("Plan gendannet fra lager: %s blokke", len(self.plan_result.plan))

    def is_charger_connected(self) -> bool:
        """True når laderen er sat i en bil (uanset hvilken connected-tilstand)."""
        return self._charger_mode() != CM_DISCONNECTED

    def recalculate(self) -> None:
        """Genberegn ladeplanen ud fra nuværende kontroller og priser."""
        rt = self.runtime
        if rt.active_vehicle == CHOOSE_VEHICLE:
            self._set_plan(None)
            return
        # Laderen ikke sat i nogen bil → ingen plan og ingen notifikationer.
        # (Ellers ville et bilvalg udløse "ikke nok tid" selvom intet er tilsluttet.)
        if not self.is_charger_connected():
            self._set_plan(None)
            return
        # "Lad straks": vis en plan der starter NU (så grafen ikke hænger på det gamle
        # skema), i stedet for prisoptimerede fremtidige slots.
        if rt.force_charge:
            self._set_plan(planner.compute_force_plan(
                now_ms=planner.to_ms(dt_util.utcnow()),
                target_pct=rt.target_soc,
                current_soc=self._live_soc(),
                capacity_kwh=self._capacity_for(rt.active_vehicle),
                power_kw=rt.charge_power,
            ))
            return
        deadline_ms = self._deadline_ms()
        if deadline_ms is None:
            self._set_plan(None)
            _LOGGER.debug("Ingen gyldig deadline — springer planberegning over")
            return
        raw_today, raw_tomorrow = self._prices()
        self._set_plan(planner.compute_plan(
            now_ms=planner.to_ms(dt_util.utcnow()),
            deadline_ms=deadline_ms,
            target_pct=rt.target_soc,
            current_soc=self._live_soc(),
            capacity_kwh=self._capacity_for(rt.active_vehicle),
            power_kw=rt.charge_power,
            raw_today=raw_today,
            raw_tomorrow=raw_tomorrow,
            min_block_mins=int(self.entry.options.get("min_block_minutes", 0)),
            window_start_ms=self._window_start_ms(),
        ))
        _LOGGER.debug(
            "Plan genberegnet: %s blokke, advarsel=%s",
            len(self.plan_result.plan),
            self.plan_result.warning,
        )
        self._post_recalc_notifications()

    def _post_recalc_notifications(self) -> None:
        """Notifikationer der udløses af en ny plan (ikke-nok-tid / ny plan)."""
        pr = self.plan_result
        rt = self.runtime
        if not pr:
            return
        # Ikke nok tid — notificér én gang indtil advarslen forsvinder igen
        if pr.warning == planner.WARN_NOT_ENOUGH_TIME:
            if not rt.not_enough_time_notified:
                rt.not_enough_time_notified = True
                self._notify(
                    "⚠️ Ikke nok tid",
                    f"Kan ikke nå {rt.target_soc:.0f}% inden deadline",
                    "notify_not_enough_time",
                )
        else:
            rt.not_enough_time_notified = False
        # Ny plan — notificér når blokkene faktisk ændrer sig
        sig = ";".join(f"{b.start_ms}-{b.end_ms}" for b in pr.plan)
        if sig and sig != rt.last_plan_signature:
            rt.last_plan_signature = sig
            first = pr.plan[0]
            start_local = dt_util.as_local(first.start_dt).strftime("%H:%M")
            self._notify(
                "📅 Ny ladeplan",
                f"Start kl. {start_local} · ~{pr.estimated_cost:.0f} kr",
                "notify_new_plan",
            )

    # ---------- hoved-loop ----------

    async def _async_update_data(self) -> Decision:
        rt = self.runtime
        mode = self._charger_mode()

        # Hold afrejse-datoen i fremtiden (auto til næste kl. 07:00)
        if self.maintain_departure():
            await self.async_save()

        # Et "frakoblet" set lige efter vores eget deauthorize blev ignoreret. Er
        # grace-perioden ovre og laderen STADIG frakoblet, var det et ægte kabel-ud.
        if rt.ignored_disconnect and not self._in_deauth_grace():
            rt.ignored_disconnect = False
            if mode == CM_DISCONNECTED:
                _LOGGER.info("Stadig frakoblet efter grace-periode — nulstiller session")
                await self._reset_for_disconnect()
            else:
                await self.async_save()

        # Håndtér mode-overgange (ny session / bilskift)
        await self._handle_mode_transition(self._prev_charger_mode, mode)
        if mode != self._prev_charger_mode:
            self._prev_charger_mode = mode
            self._mode_since = dt_util.utcnow()
            rt.prev_charger_mode = mode  # persistér så genstart kender sidste mode
            await self.async_save()

        # Laderen lader → autorisationen er brugt. Først NU må et senere slot (efter
        # stop/resume) autorisere igen. Ellers genarmeres authorize kun af kabel-ud
        # eller vores eget deauthorize — aldrig af tid, til/fra eller et gættet bilskifte.
        if mode == CM_CHARGING and (rt.authorize_done or rt.rescue_round or rt.rescue_phase):
            rt.authorize_done = False
            rt.rescue_round = 0
            rt.rescue_phase = ""
            await self.async_save()

        decision = self._decide(mode)

        # Notifikation: ladning faktisk startet
        await self._maybe_notify_charge_start()

        # Persistér SoC-cachen hvis der er set en ny gyldig aflæsning
        if self._soc_cache_dirty:
            self._soc_cache_dirty = False
            await self.async_save()

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
        live_soc = 0.0  # opdateres når en bil er valgt

        def dec(action: str, reason: str, **kw) -> Decision:
            # Medtag altid live_soc så SoC vises korrekt uanset gren (også når slået fra)
            kw.setdefault("live_soc", live_soc)
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

        # Beregn live-SoC nu hvor vi har en bil (bruges i alle grene nedenfor)
        live_soc = self._live_soc()
        target = rt.target_soc

        # 2) Master-kontakt?
        if not rt.enabled:
            return dec(ACT_IDLE, "Automatik slået fra (aktivér for at lade)")

        # 3) Frakoblet?
        if mode == CM_DISCONNECTED:
            return dec(ACT_IDLE, "Laderen er frakoblet")

        # 4) Plan + slot
        plan = self.plan_result
        now_ms = planner.to_ms(dt_util.utcnow())

        # Afgang: afrejsetid passeret → session slut, sluk automatik (og stop ladning)
        if rt.mode == MODE_DEPARTURE and not rt.force_charge:
            dl = self._deadline_ms()
            if dl is not None and now_ms >= dl:
                if power > CHARGE_POWER_THRESHOLD_KW:
                    self._do_stop(observer)
                if rt.enabled:
                    rt.enabled = False
                    self.hass.async_create_task(self.async_save())
                return dec(
                    ACT_IDLE,
                    "Afrejsetid passeret — automatik slået fra",
                    live_soc=live_soc,
                    actuated=not observer,
                )

        in_slot = bool(
            plan
            and plan.plan
            and any(b.start_ms <= now_ms < b.end_ms for b in plan.plan)
        )
        should_be_charging = rt.force_charge or in_slot

        # Giv-op-timer (udfaldsdrevet): venter vi forgæves på strøm i et aktivt slot?
        # Kun når vi faktisk styrer laderen (ikke observatør).
        if not observer:
            self._update_start_failure_timer(should_be_charging, power)

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

        # 6) Mål nået? KUN hvis vi faktisk har ladet i denne session — ellers har
        #    brugeren bare valgt en bil der allerede er ved/over målet (rør ikke laderen).
        if live_soc >= target:
            if self._has_charged_this_session():
                self._on_target_reached(target, car_side=False)
                return dec(
                    ACT_TARGET_REACHED,
                    f"Mål nået ({live_soc:.0f}% ≥ {target:.0f}%)",
                    live_soc=live_soc,
                    actuated=not observer,
                )
            return dec(
                ACT_WAITING,
                f"Allerede ved mål ({live_soc:.0f}%)",
                live_soc=live_soc,
            )

        # 7) Force charge
        if rt.force_charge:
            if not really_charging:
                outcome = self._do_start(mode, observer)
                return dec(
                    ACT_WAITING if outcome == "gave_up" else ACT_START,
                    self._start_reason("Lad straks (force)", outcome),
                    live_soc=live_soc,
                    actuated=outcome in ("authorized", "resumed"),
                )
            return dec(
                ACT_CHARGING,
                f"Lad straks (force) — lader {power:.1f} kW",
                live_soc=live_soc,
            )

        # 8) Flyder der strøm, men skal vi IKKE lade (uden for slot, ingen force)? → stop
        #    Køres FØR "ingen plan"/"afventer"-grenene, så en færdig eller tom plan
        #    også stopper en igangværende ladning. Baseret på faktisk effekt, så det
        #    også fanges hvis Zaptec skifter mode (fx connected_finished) mens der lades.
        power_flowing = power > CHARGE_POWER_THRESHOLD_KW
        if power_flowing and not should_be_charging:
            self._do_stop(observer)
            return dec(
                ACT_PAUSE,
                "Uden for slot — stopper ladning",
                live_soc=live_soc,
                actuated=not observer,
            )

        # 9) I slot?
        if in_slot:
            if not really_charging:
                # Første start i slottet: ikke præcis på kvarteret (se SLOT_START_DELAY)
                if not rt.authorize_done and not rt.rescue_phase and not observer:
                    blk = next(b for b in plan.plan if b.start_ms <= now_ms < b.end_ms)
                    wait_ms = guards.start_delay_remaining_ms(
                        now_ms=now_ms,
                        block_start_ms=blk.start_ms,
                        delay_ms=int(SLOT_START_DELAY.total_seconds() * 1000),
                    )
                    if wait_ms > 0:
                        self._wake_in(wait_ms / 1000 + 0.5)
                        return dec(
                            ACT_WAITING,
                            f"I ladeslot — starter om {wait_ms / 1000:.0f} s",
                            in_slot=True,
                            live_soc=live_soc,
                        )
                outcome = self._do_start(mode, observer)
                return dec(
                    ACT_WAITING if outcome == "gave_up" else ACT_START,
                    self._start_reason("I ladeslot", outcome),
                    in_slot=True,
                    live_soc=live_soc,
                    actuated=outcome in ("authorized", "resumed"),
                )
            return dec(
                ACT_CHARGING,
                f"I ladeslot — lader {live_soc:.0f}%",
                in_slot=True,
                live_soc=live_soc,
            )

        # 10) Ikke i slot og ingen strøm → afventer
        if not plan or not plan.plan:
            reason = "Ingen ladeplan (afventer priser/valg)"
            if plan and plan.warning == planner.WARN_ALREADY_AT_TARGET:
                reason = "Allerede ved mål"
            elif plan and plan.warning == planner.WARN_NO_PRICES:
                reason = "Ingen prisdata i tidsvinduet"
            return dec(ACT_WAITING, reason, live_soc=live_soc)

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

    def _do_start(self, mode: str, observer: bool) -> str:
        """Forsøg at starte ladning. Returnerer udfaldet (til status/UI):
        ``authorized`` | ``resumed`` | ``suppressed`` | ``gave_up``.
        """
        if observer:
            _LOGGER.info("[OBSERVER] Ville starte ladning (mode=%s)", mode)
            return "suppressed"
        if self._closed:
            return "suppressed"
        rt = self.runtime
        now = dt_util.utcnow()
        now_ms = planner.to_ms(now)
        # Husk seneste kommanderede start (persisteret) — så laderens efterfølgende
        # finished→requesting ikke fejltolkes som et bilskifte.
        rt.start_commanded_iso = now.isoformat()
        is_requesting = mode == CM_REQUESTING

        def iso_ms(value: str) -> int | None:
            return planner.to_ms(value) if value else None

        deauth_entity = self._deauthorize_entity()
        step = guards.rescue_step(
            is_requesting=is_requesting,
            power_flowing=self._charge_power() > CHARGE_POWER_THRESHOLD_KW,
            authorize_done=rt.authorize_done,
            press_inflight=self._press_inflight is not None,
            gave_up=rt.start_failed_notified,
            phase=rt.rescue_phase,
            rounds_used=rt.rescue_round,
            max_rounds=RESCUE_MAX_ROUNDS,
            has_deauthorize=deauth_entity is not None,
            authorize_sent_ms=iso_ms(rt.authorize_sent_iso),
            deauth_done_ms=iso_ms(rt.rescue_deauth_done_iso),
            mode_since_ms=planner.to_ms(self._mode_since),
            now_ms=now_ms,
            confirm_ms=int(AUTHORIZE_CONFIRM_TIMEOUT.total_seconds() * 1000),
            settle_ms=int(DEAUTHORIZE_SETTLE.total_seconds() * 1000),
            deauth_max_wait_ms=int(DEAUTHORIZE_MAX_WAIT.total_seconds() * 1000),
        )

        if step == guards.RESCUE_DEAUTHORIZE:
            rt.rescue_round += 1
            rt.rescue_phase = "deauthorized"
            rt.rescue_deauth_iso = now.isoformat()
            rt.rescue_deauth_done_iso = ""
            # Deauthorize nulstiller autorisationen → præcis ét nyt authorize bagefter
            rt.authorize_done = False
            _LOGGER.warning(
                "Ingen strøm efter authorize — redning %s/%s: deauthorize (%s)",
                rt.rescue_round,
                RESCUE_MAX_ROUNDS,
                deauth_entity,
            )
            self.hass.async_create_task(self.async_save())
            self.hass.async_create_task(self._press_tracked(deauth_entity, "deauthorize"))
            self._wake_in(5)
            return "rescuing"

        if step == guards.RESCUE_AUTHORIZE:
            rt.rescue_phase = ""
            _LOGGER.warning(
                "Redning %s/%s: laderen er klar igen — ét authorize",
                rt.rescue_round,
                RESCUE_MAX_ROUNDS,
            )
            self._send_authorize(now)
            return "authorized"

        if step == guards.RESCUE_WAIT:
            self._wake_in(5)
            return "rescuing" if rt.rescue_phase else "suppressed"

        if step == guards.RESCUE_ABORT:
            _LOGGER.warning(
                "Laderen kom ikke tilbage i requesting efter deauthorize (mode=%s)"
                " — opgiver redning",
                mode,
            )
            rt.rescue_phase = ""
            rt.rescue_round = RESCUE_MAX_ROUNDS
            self.hass.async_create_task(self.async_save())

        if guards.may_authorize(
            is_requesting=is_requesting,
            authorize_done=rt.authorize_done,
            gave_up=rt.start_failed_notified,
            last_authorize_ms=iso_ms(rt.last_authorize_iso),
            now_ms=now_ms,
            min_interval_ms=int(AUTHORIZE_MIN_INTERVAL.total_seconds() * 1000),
        ):
            self._send_authorize(now)
            return "authorized"
        if not is_requesting:
            # resume er kun gyldig/tilgængelig når vi IKKE står i requesting
            self.hass.async_create_task(self._press(CONF_RESUME_BUTTON))
            return "resumed"
        # requesting, men authorize undertrykt (allerede sendt / backstop / giv-op)
        if rt.authorize_done and rt.authorize_sent_iso and not rt.start_failed_notified:
            sent = dt_util.parse_datetime(rt.authorize_sent_iso)
            if sent is not None:
                left = (sent + AUTHORIZE_CONFIRM_TIMEOUT - now).total_seconds()
                self._wake_in(max(1.0, left + 1))
        return "gave_up" if rt.start_failed_notified else "suppressed"

    def _send_authorize(self, now: datetime) -> None:
        rt = self.runtime
        rt.authorize_done = True
        rt.last_authorize_iso = now.isoformat()
        rt.authorize_sent_iso = ""  # sættes når kaldet til Zaptec er færdigt
        self.hass.async_create_task(self.async_save())
        self.hass.async_create_task(
            self._press_tracked(self._cfg(CONF_AUTHORIZE_BUTTON), "authorize")
        )
        _LOGGER.info("Authorize sendt")

    def _deauthorize_entity(self) -> str | None:
        """Deauthorize-knappen: konfigureret, eller udledt af authorize-knappens id."""
        configured = self._cfg(CONF_DEAUTHORIZE_BUTTON)
        if configured:
            return configured
        auth = self._cfg(CONF_AUTHORIZE_BUTTON) or ""
        if "_authorize" in auth:
            candidate = auth.replace("_authorize", "_deauthorize", 1)
            if self.hass.states.get(candidate) is not None:
                return candidate
        return None

    def _in_deauth_grace(self) -> bool:
        rt = self.runtime
        if not rt.rescue_deauth_iso:
            return False
        started = dt_util.parse_datetime(rt.rescue_deauth_iso)
        return started is not None and dt_util.utcnow() - started < DEAUTHORIZE_GRACE_PERIOD

    def _wake_in(self, seconds: float) -> None:
        """Planlæg en ekstra beslutning om ``seconds`` (beholder den tidligste)."""
        if self._closed:
            return
        at = dt_util.utcnow() + timedelta(seconds=seconds)
        if self._wake_unsub is not None and self._wake_at is not None and self._wake_at <= at:
            return
        if self._wake_unsub is not None:
            self._wake_unsub()

        @callback
        def _fire(_now) -> None:
            self._wake_unsub = None
            self._wake_at = None
            if not self._closed:
                self.hass.async_create_task(self.async_refresh())

        self._wake_at = at
        self._wake_unsub = async_call_later(self.hass, seconds, _fire)

    @callback
    def async_close(self) -> None:
        """Ved unload/reload: ingen forsinkede beslutninger fra denne instans."""
        self._closed = True
        if self._wake_unsub is not None:
            self._wake_unsub()
            self._wake_unsub = None

    def _start_reason(self, prefix: str, outcome: str) -> str:
        rt = self.runtime
        return {
            "authorized": f"{prefix} — authorize sendt, afventer laderen",
            "resumed": f"{prefix} — genoptager ladning",
            "suppressed": f"{prefix} — afventer laderen",
            "rescuing": (
                f"{prefix} — laderen reagerede ikke, genstarter autorisation "
                f"({rt.rescue_round}/{RESCUE_MAX_ROUNDS})"
            ),
            "gave_up": f"{prefix} — kunne ikke starte, tjek laderen",
        }.get(outcome, prefix)

    def _update_start_failure_timer(self, should_be_charging: bool, power: float) -> None:
        """Udfaldsdrevet giv-op: notificér én gang hvis vi venter forgæves på strøm."""
        rt = self.runtime
        now = dt_util.utcnow()
        wait_ms = (
            planner.to_ms(rt.start_wait_since_iso) if rt.start_wait_since_iso else None
        )
        new_wait, should_notify = guards.start_failure_state(
            should_be_charging=should_be_charging,
            power_flowing=power > CHARGE_POWER_THRESHOLD_KW,
            wait_since_ms=wait_ms,
            now_ms=planner.to_ms(now),
            timeout_ms=int(START_FAILED_TIMEOUT.total_seconds() * 1000),
            already_notified=rt.start_failed_notified,
        )
        if new_wait is None:
            # Lader nu, eller ikke i et aktivt slot → nulstil timer + giv-op-flag
            if rt.start_wait_since_iso or rt.start_failed_notified:
                rt.start_wait_since_iso = ""
                rt.start_failed_notified = False
                self.hass.async_create_task(self.async_save())
            return
        if not rt.start_wait_since_iso:
            rt.start_wait_since_iso = now.isoformat()
            self.hass.async_create_task(self.async_save())
        # Ikke opgive midt i en redning eller mens et Zaptec-kald stadig kører
        if should_notify and (rt.rescue_phase or self._press_inflight):
            return
        if should_notify:
            rt.start_failed_notified = True
            tried = (
                f" — heller ikke efter {rt.rescue_round} × deauthorize + authorize"
                if rt.rescue_round
                else ""
            )
            self._notify(
                "⚠️ Kunne ikke starte ladning",
                f"Laderen svarer ikke{tried}. Tag stikket ud og sæt det i igen.",
                "notify_not_enough_time",
            )
            self.hass.async_create_task(self.async_save())

    def on_user_restart(self) -> None:
        """Eksplicit brugerhandling (Lad straks / master-kontakt slået til).

        Genarmerer IKKE authorize: er der allerede sendt authorize mens kablet har
        siddet i, må der ikke sendes et nyt direkte (det låser laderen). Står laderen
        stadig og venter, tager redningen over (deauthorize → vent → ét authorize).
        """
        rt = self.runtime
        rt.start_wait_since_iso = ""
        rt.start_failed_notified = False
        if not rt.rescue_phase:
            rt.rescue_round = 0

    def _do_stop(self, observer: bool) -> None:
        if observer:
            _LOGGER.info("[OBSERVER] Ville stoppe/pause ladning")
            return
        self.hass.async_create_task(self._press(CONF_STOP_BUTTON))

    async def _press(self, conf_key: str) -> None:
        entity_id = self._cfg(conf_key)
        if not entity_id:
            return
        st = self.hass.states.get(entity_id)
        # Drop KUN hvis entiteten reelt er utilgængelig. IKKE på "unknown" — en
        # aldrig-trykket knap er "unknown" men fuldt brugbar (fx stop-knappen).
        if st is None or st.state == "unavailable":
            _LOGGER.debug("Springer tryk over — %s utilgængelig", entity_id)
            return
        await self.hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=False
        )

    async def _press_tracked(self, entity_id: str | None, kind: str) -> None:
        """Tryk authorize/deauthorize og VENT til Zaptec-kaldet er helt færdigt.

        Zaptec-integrationen gentager selv et kald ved timeout (op til ~100 s), og et
        authorize der når frem to gange låser laderen. Så længe kaldet kører, gør vi
        intet nyt (``_press_inflight``), og et langsomt kald logges som advarsel.
        """
        rt = self.runtime
        st = self.hass.states.get(entity_id) if entity_id else None
        if st is None or st.state == "unavailable":
            _LOGGER.warning("%s ikke sendt — %s er utilgængelig", kind, entity_id)
            if kind == "authorize":
                rt.authorize_done = False  # intet sendt → må prøves igen (efter backstop)
            else:
                # Deauthorize ikke sendt → vores authorize gælder stadig: intet nyt authorize
                rt.authorize_done = True
                rt.rescue_phase = ""
                rt.rescue_round = RESCUE_MAX_ROUNDS
            await self.async_save()
            return

        self._press_inflight = kind
        started = time.monotonic()
        failed = False
        try:
            await self.hass.services.async_call(
                "button", "press", {"entity_id": entity_id}, blocking=True
            )
        except Exception as err:  # noqa: BLE001 — Zaptec-fejl må ikke vælte coordinatoren
            failed = True
            _LOGGER.warning("%s-kaldet fejlede: %s", kind, err)
        finally:
            self._press_inflight = None
        took = time.monotonic() - started
        if took > SLOW_PRESS_WARNING.total_seconds():
            _LOGGER.warning(
                "%s-kaldet til Zaptec tog %.0f s — Zaptec gentog sandsynligvis kaldet",
                kind,
                took,
            )
        else:
            _LOGGER.info("%s-kaldet til Zaptec tog %.1f s", kind, took)

        done = dt_util.utcnow().isoformat()
        if kind == "authorize":
            # Også ved fejl: kaldet kan være nået frem → behandl som sendt
            rt.authorize_sent_iso = done
            self._wake_in(AUTHORIZE_CONFIRM_TIMEOUT.total_seconds() + 1)
        elif failed:
            # Uvist om deauthorize nåede frem → intet authorize, opgiv redningen
            rt.authorize_done = True
            rt.rescue_phase = ""
            rt.rescue_round = RESCUE_MAX_ROUNDS
        else:
            rt.rescue_deauth_done_iso = done
            self._wake_in(DEAUTHORIZE_SETTLE.total_seconds() + 1)
        await self.async_save()
        if not self._closed:
            await self.async_request_refresh()

    def _on_target_reached(self, target: float, car_side: bool) -> None:
        rt = self.runtime
        # Faktisk SoC nu — beregnes FØR vi nulstiller anker/baseline nedenfor
        actual = self._live_soc()
        rt.session_complete = True
        rt.force_charge = False
        # Sensoren er sandheden når den findes — brænd kun målet ind for manuelle biler
        if not self.active_vehicle_has_sensor():
            rt.current_soc = target
        rt.session_baseline_kwh = self._session_energy()
        rt.enabled = False
        if not rt.observer_mode:
            self.hass.async_create_task(self._press(CONF_STOP_BUTTON))
        self._notify(
            "🔋 Ladning færdig",
            ("Bilen stoppede selv — " if car_side else "Klar — ") + f"{actual:.0f}%",
            "notify_car_side_stop" if car_side else "notify_target_reached",
        )
        # Fravælg bilen så dashboardet viser overblikket (begge biler) igen.
        # Ny ladning kræver at man vælger en bil på ny.
        rt.active_vehicle = CHOOSE_VEHICLE
        self._set_plan(None)
        self.hass.async_create_task(self.async_save())

    # ---------- notifikation ----------

    async def _maybe_notify_charge_start(self) -> None:
        rt = self.runtime
        power = self._charge_power()
        if power > CHARGE_POWER_THRESHOLD_KW and not rt.charge_start_notified:
            rt.charge_start_notified = True
            self._notify(
                "⚡ Ladning startet",
                f"{rt.active_vehicle} lader nu — {self._live_soc():.0f}% ({power:.1f} kW)",
                "notify_charging_started",
            )
            await self.async_save()

    def _notify_targets(self) -> list[str]:
        targets = self.entry.options.get(CONF_NOTIFY_TARGETS)
        if targets:
            return list(targets)
        # Bagudkompatibilitet: gammelt enkelt-felt fra opsætningen
        single = self._cfg(CONF_NOTIFY_SERVICE)
        return [single] if single else []

    def _notify_enabled(self, ntype: str) -> bool:
        return bool(self.entry.options.get(ntype, NOTIFY_DEFAULTS.get(ntype, False)))

    def _notify(self, title: str, message: str, ntype: str) -> None:
        """Send notifikation af en given type til alle valgte modtagere.

        Uafhængig af observatør-tilstand — kun laderstyring gates af observatør.
        """
        if not self._notify_enabled(ntype):
            return
        for service in self._notify_targets():
            if "." not in service:
                continue
            domain, name = service.split(".", 1)
            self.hass.async_create_task(
                self.hass.services.async_call(
                    domain,
                    name,
                    {
                        "title": title,
                        "message": message,
                        # Tryk på beskeden åbner opladningssiden. iOS læser "url",
                        # Android/Fire læser "clickAction" — derfor sættes begge.
                        # Notify-tjenester der ikke kender nøglerne ignorerer dem.
                        "data": {
                            "url": NOTIFY_CLICK_PATH,
                            "clickAction": NOTIFY_CLICK_PATH,
                        },
                    },
                    blocking=False,
                )
            )

    # ---------- mode-overgange (ny session / bilskift) ----------

    async def _reset_for_disconnect(self) -> None:
        rt = self.runtime
        rt.session_complete = False
        rt.force_charge = False
        rt.charge_state = "idle"
        rt.zero_power_ticks = 0
        rt.charge_start_notified = False
        rt.not_enough_time_notified = False
        rt.last_plan_signature = ""
        rt.active_vehicle = CHOOSE_VEHICLE
        rt.enabled = False
        # Kabel ude er den ENESTE betingelse der oplåser Zaptec'en → nulstil ALT
        # authorize-relateret, inkl. den hårde backstop og redningen.
        rt.authorize_done = False
        rt.last_authorize_iso = ""
        rt.authorize_sent_iso = ""
        rt.start_commanded_iso = ""
        rt.start_wait_since_iso = ""
        rt.start_failed_notified = False
        rt.rescue_round = 0
        rt.rescue_phase = ""
        rt.rescue_deauth_iso = ""
        rt.rescue_deauth_done_iso = ""
        rt.ignored_disconnect = False
        self._set_plan(None)
        await self.async_save()

    async def _handle_mode_transition(self, prev: str | None, now: str) -> None:
        if prev == now:
            return
        rt = self.runtime
        # Lige efter vores EGET deauthorize kan laderen skifte mode kortvarigt. Det
        # må ikke tolkes som kabel ud / ny session / bilskifte (det ville fravælge
        # bilen midt i redningen). Et ægte kabel-ud fanges når grace-perioden er ovre.
        if self._in_deauth_grace():
            if now == CM_DISCONNECTED:
                _LOGGER.info("Ignorerer %s → %s: eget deauthorize (grace)", prev, now)
                rt.ignored_disconnect = True
                await self.async_save()
                return
            if prev in _NEW_SESSION_FROM or (prev == CM_FINISHED and now == CM_REQUESTING):
                _LOGGER.info("Ignorerer %s → %s: eget deauthorize (grace)", prev, now)
                if prev == CM_DISCONNECTED and rt.ignored_disconnect:
                    rt.ignored_disconnect = False
                    await self.async_save()
                return
        # Frakoblet: laderen er taget ud af bilen → fravælg bilen og nulstil,
        # så en gammel valgt bil ikke hænger ved og udløser beregning/notifikationer.
        if now == CM_DISCONNECTED and prev not in (None, CM_DISCONNECTED):
            _LOGGER.info("Laderen frakoblet (%s → %s) — fravælger bil", prev, now)
            await self._reset_for_disconnect()
            return
        is_fresh_plugin = prev in _NEW_SESSION_FROM and now in (
            CM_REQUESTING,
            CM_CHARGING,
        )
        is_car_swap = prev == CM_FINISHED and now == CM_REQUESTING
        # Undertryk hvis vi selv lige har kommanderet en start: laderen vågner så i
        # requesting af sig selv. Et ægte bilskifte kræver at nogen rører kablet.
        # (start_commanded_iso er persisteret, så en reload ikke fejltolker en flap.)
        if is_car_swap and rt.start_commanded_iso:
            last = dt_util.parse_datetime(rt.start_commanded_iso)
            if last is not None:
                since = (dt_util.utcnow() - last).total_seconds()
                if since < 300:
                    _LOGGER.debug(
                        "Ignorerer finished→requesting: egen start for %ss siden",
                        int(since),
                    )
                    is_car_swap = False
        if is_fresh_plugin or is_car_swap:
            _LOGGER.info("Ny session (%s → %s) — nulstiller", prev, now)
            rt.session_complete = False
            rt.force_charge = False
            rt.charge_state = "idle"
            rt.zero_power_ticks = 0
            rt.charge_start_notified = False
            rt.not_enough_time_notified = False
            rt.last_plan_signature = ""
            rt.session_baseline_kwh = self._session_energy()
            rt.active_vehicle = CHOOSE_VEHICLE
            rt.enabled = False
            # Genarmér IKKE authorize: uden et set "frakoblet" ved vi ikke at kablet
            # har været ude, og et andet authorize kan låse laderen. Frisk isætning
            # (fra frakoblet) er allerede nulstillet af _reset_for_disconnect; hænger
            # et gammelt authorize, klarer redningen det (deauthorize → ét authorize).
            rt.start_wait_since_iso = ""
            rt.start_failed_notified = False
            self._set_plan(None)
            self._notify(
                "🔌 Bil tilsluttet",
                "Vælg hvilken bil du vil lade",
                "notify_cable_connected",
            )
            await self.async_save()
