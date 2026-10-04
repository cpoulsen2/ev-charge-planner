"""Coordinator: minut-loop der beslutter om og hvordan der skal lades.

Laderen styres KUN via en strømgrænse (fx ``number.zag089363_available_current``):
ladestrøm i et slot eller ved "Lad straks", ellers 0 A. Hvert tick beregnes den
ønskede strøm og sammenlignes med den værdi laderen melder; afviger de, sendes
værdien igen (idempotent) med pauser, til laderen har den. Ingen authorize,
resume eller stop — autorisation skal være slået fra på laderen.

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
    CAR_SIDE_STOP_TICKS,
    CHARGE_POWER_THRESHOLD_KW,
    CHOOSE_VEHICLE,
    CM_CHARGING,
    CM_DISCONNECTED,
    CM_REQUESTING,
    CONF_CHARGE_CURRENT,
    CONF_CHARGE_POWER_SENSOR,
    CONF_CHARGER_MODE_SENSOR,
    CONF_CURRENT_ENTITY,
    CONF_NOTIFY_SERVICE,
    CONF_PRICE_SENSOR,
    CONF_SESSION_ENERGY_SENSOR,
    CONF_NOTIFY_TARGETS,
    CONF_TOMORROW_SENSOR,
    CONF_VEHICLES,
    CURRENT_CONFIRM_DELAY,
    CURRENT_MIN_CHANGE_INTERVAL,
    DEFAULT_CHARGE_CURRENT,
    DEFAULT_DEPARTURE_HOUR,
    EVENT_ACTION,
    NOTIFY_CLICK_PATH,
    NOTIFY_DEFAULTS,
    GUEST_VEHICLE,
    SESSION_SETTLE,
    SLOW_CALL_WARNING,
    START_FAILED_TIMEOUT,
    UPDATE_INTERVAL,
    USER_ACTION_URGENCY,
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
    desired_current: float = 0.0  # A planneren vil have på laderen
    actual_current: float | None = None  # A laderen melder (None = ukendt)
    current_note: str = ""  # fx "Sætter 16 A — Zaptec svarer ikke, prøver igen 03:07"
    charge_current_set: float | None = None  # A laderen faktisk bruger (ChargeCurrentSet)
    max_current_target: float = 0.0  # max current planneren holder (0 A / ladestrøm)
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
        # Et strøm-kald der stadig kører — Zaptec-integrationen kan selv gentage et
        # kald i op til ~100 s, så der sendes aldrig et nyt mens det kører.
        self._write_inflight = False
        # Ny session: send den ønskede strøm igen efter dette tidspunkt (Zaptec)
        self._session_send_at: datetime | None = None
        self._switch_inflight = False  # ladekontakt-kommando i gang
        # Brugerhandling (Stop, Lad straks, automatik til, bilvalg …) må ændre
        # strømmen med det samme — uden Zaptecs 15-minutters-anbefaling.
        self._urgent_until: datetime | None = None
        self._wake_unsub = None
        self._wake_at: datetime | None = None
        self._closed = False
        self._price_count = 0
        self.prices_until_ms: int | None = None  # sidste tidspunkt med kendte priser

    # ---------- persistens ----------

    async def async_save(self) -> None:
        await self.store.save()

    async def async_user_changed(self, urgent: bool = True) -> None:
        """Kaldes af kontrol-entities når brugeren ændrer en værdi.

        Bruger async_refresh() (øjeblikkelig) i stedet for async_request_refresh()
        (debounced), så dashboardet opdaterer straks ved fx bilskift. ``urgent``:
        en brugerhandling må ændre strømmen med det samme (ikke ved fx nye priser).
        """
        if urgent:
            self.mark_urgent()
        self.recalculate()
        await self.async_save()
        await self.async_refresh()

    async def async_stop_charging(self) -> None:
        """Stop/annullér ladning nu: 0 A og automatik fra.

        Automatikken slås fra så den ikke genstarter; brugeren aktiverer igen
        for at følge planen."""
        rt = self.runtime
        rt.force_charge = False
        rt.enabled = False
        self.mark_urgent()
        await self.async_save()
        await self.async_refresh()

    def mark_urgent(self) -> None:
        """Brugerhandling: strømmen må ændres med det samme."""
        self._urgent_until = dt_util.utcnow() + USER_ACTION_URGENCY

    def on_user_restart(self) -> None:
        """Brugeren slog automatik til / trykkede "Lad straks": ny venteperiode."""
        rt = self.runtime
        rt.start_wait_since_iso = ""
        rt.start_failed_notified = False
        self.mark_urgent()

    # ---------- aflæsning af eksterne sensorer ----------

    def _cfg(self, key: str) -> str | None:
        return self.entry.data.get(key)

    def _opt(self, key: str, default=None):
        """Indstilling fra options (Konfigurér), ellers fra den oprindelige opsætning."""
        if key in self.entry.options:
            return self.entry.options[key]
        return self.entry.data.get(key, default)

    def current_entity(self) -> str | None:
        """Strøm-entiteten laderen styres med.

        Laderens egen max-strøm (fx number.zag089363_charger_max_current, udledt af
        charger mode-sensoren) bruges altid, når den findes — den er bevist at virke
        (0 A = pause, 16 A = lader). Den valgte entitet er kun en reserve.
        """
        derived = guards.charger_max_current_entity(self._cfg(CONF_CHARGER_MODE_SENSOR))
        if derived and self.hass.states.get(derived) is not None:
            return derived
        return self._opt(CONF_CURRENT_ENTITY) or derived or None

    def charge_amps(self) -> float:
        """Ladestrøm i A — aldrig over hvad strøm-entiteten tillader."""
        amps = float(self._opt(CONF_CHARGE_CURRENT, DEFAULT_CHARGE_CURRENT))
        st = self.hass.states.get(self.current_entity() or "")
        if st is not None:
            try:
                amps = min(amps, float(st.attributes.get("max", amps)))
            except (TypeError, ValueError):
                pass
        return amps

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

    def tomorrow_sensor_candidates(self) -> list[str]:
        """Morgendags-sensorer i prioriteret rækkefølge: den valgte, derefter den der
        udledes af pris-sensorens navn. Har den valgte ingen priser (fx en forkert
        sensor fra opsætningen), bruges den udledte i stedet."""
        out: list[str] = []
        configured = self._opt(CONF_TOMORROW_SENSOR)
        if configured:
            out.append(configured)
        price = self.price_sensor() or ""
        if price.startswith("sensor."):
            derived = f"binary_sensor.{price.split('.', 1)[1]}_tomorrow"
            if derived not in out:
                out.append(derived)
        return out

    def price_sensor(self) -> str | None:
        return self._opt(CONF_PRICE_SENSOR) or None

    def _prices(self) -> tuple[list[dict], list[dict]]:
        self._price_count = 0
        st = self.hass.states.get(self.price_sensor() or "")
        if not st:
            return [], []
        attrs = st.attributes
        # Strømligning har flere formater: "adjusted"-sensorerne har prices_today /
        # prices_tomorrow, "current price"-sensoren har én samlet liste i "prices".
        raw_today = (
            attrs.get("prices_today") or attrs.get("raw_today") or attrs.get("prices") or []
        )
        raw_tomorrow = attrs.get("prices_tomorrow") or attrs.get("raw_tomorrow") or []

        # Fald tilbage til den separate "tomorrow"-sensor hvis hovedsensoren ikke
        # selv har morgendagens priser
        if not raw_tomorrow:
            for tmr_id in self.tomorrow_sensor_candidates():
                tmr = self.hass.states.get(tmr_id)
                if tmr and tmr.state == "on":
                    # adjusted-sensoren: prices_tomorrow; spotpris-sensoren: prices
                    raw_tomorrow = (
                        tmr.attributes.get("prices_tomorrow")
                        or tmr.attributes.get("raw_tomorrow")
                        or tmr.attributes.get("prices")
                        or []
                    )
                if raw_tomorrow:
                    break
        # Samme kvarter må ikke tælle to gange (fx hvis "prices" allerede har i morgen)
        seen = {planner.to_ms(e["start"]) for e in raw_today if "start" in e}
        raw_tomorrow = [
            e for e in raw_tomorrow if "start" in e and planner.to_ms(e["start"]) not in seen
        ]
        self._price_count = len(raw_today) + len(raw_tomorrow)
        slots = planner.expand_mixed(list(raw_today) + list(raw_tomorrow))
        self.prices_until_ms = (
            max(sl.time_ms for sl in slots) + planner.SLOT_MS if slots else None
        )
        return list(raw_today), list(raw_tomorrow)

    # ---------- deadline / live SoC ----------

    @staticmethod
    def _next_default_departure() -> datetime:
        """Næste kl. 07:00 (i morgen tidlig når bilen sættes i om aftenen)."""
        now = dt_util.now()  # lokal, aware
        nxt = now.replace(hour=DEFAULT_DEPARTURE_HOUR, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt = nxt + timedelta(days=1)
        return nxt

    def reset_departure(self) -> None:
        """Afgang = næste kl. 07:00 (sættes ved kabel ud; kan ændres når som helst)."""
        self.runtime.departure_iso = self._next_default_departure().isoformat()

    def maintain_departure(self) -> bool:
        """Hold afrejse-datoen i fremtiden. Er den tom eller passeret, sættes den til
        næste kl. 07:00 — som den gamle Standard-tilstand: bliver bilen siddende,
        gælder næste morgen kl. 07:00. Returnerer True hvis værdien blev ændret."""
        rt = self.runtime
        dep = dt_util.parse_datetime(rt.departure_iso) if rt.departure_iso else None
        if dep is not None and dep.tzinfo is None:
            dep = dt_util.as_local(dep)
        if dep is None or dep <= dt_util.now():
            self.reset_departure()
            return True
        return False

    def _window_start_ms(self) -> int | None:
        """Ladevindue: tidligst-start (kun i Afgang når slået til). None = ingen grænse."""
        rt = self.runtime
        if not rt.use_earliest_start or not rt.earliest_start_iso:
            return None
        dep = dt_util.parse_datetime(rt.earliest_start_iso)
        if dep is None:
            return None
        if dep.tzinfo is None:
            dep = dt_util.as_local(dep)
        return planner.to_ms(dep)

    def _deadline_ms(self) -> int | None:
        rt = self.runtime
        # Afgang: brug afrejse-dato+tid (næste kl. 07:00 når bilen tilsluttes)
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

        # Afrejsetid mangler (fx ny opsætning) → næste kl. 07:00
        if self.maintain_departure():
            await self.async_save()

        # Håndtér mode-overgange (kabel ud / ny isætning)
        await self._handle_mode_transition(self._prev_charger_mode, mode)
        if mode != self._prev_charger_mode:
            self._prev_charger_mode = mode
            rt.prev_charger_mode = mode  # persistér så genstart kender sidste mode
            await self.async_save()

        decision = self._decide(mode)
        self._warn_if_prices_end_before_departure(decision)

        # Hold laderens strøm på den ønskede værdi (gentag til den er sat)
        await self._reconcile_current(decision)

        # Advarsel hvis der skal lades, men ingen strøm kommer
        if not decision.observer:
            self._update_start_failure_timer(decision, mode)

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
        """Beslutningslogik → Decision med den ønskede strøm (``desired_current``).

        Rører ikke laderen selv — det gør ``_reconcile_current`` bagefter. Alle grene
        der ikke eksplicit lader, ønsker 0 A (hvile-tilstand: ingen ladning).
        """
        rt = self.runtime
        observer = rt.observer_mode
        power = self._charge_power()
        really_charging = mode == CM_CHARGING and power > CHARGE_POWER_THRESHOLD_KW
        amps = self.charge_amps()
        live_soc = 0.0  # opdateres når en bil er valgt

        def dec(action: str, reason: str, **kw) -> Decision:
            # Medtag altid live_soc så SoC vises korrekt uanset gren (også når slået fra)
            kw.setdefault("live_soc", live_soc)
            kw.setdefault("desired_current", 0.0)
            return Decision(
                action=action,
                reason=reason,
                charger_mode=mode,
                charge_power=power,
                target_soc=rt.target_soc,
                observer=observer,
                **kw,
            )

        # 1) Bil valgt? ("Lad straks" virker også uden — så kan man altid lade)
        if rt.active_vehicle == CHOOSE_VEHICLE:
            if rt.force_charge and rt.enabled and mode != CM_DISCONNECTED:
                return dec(
                    ACT_CHARGING if really_charging else ACT_START,
                    f"Lad straks (ingen bil valgt) — {amps:.0f} A",
                    desired_current=amps,
                )
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
        if not rt.force_charge:
            dl = self._deadline_ms()
            if dl is not None and now_ms >= dl:
                if rt.enabled:
                    rt.enabled = False
                    self.hass.async_create_task(self.async_save())
                return dec(
                    ACT_IDLE,
                    "Afrejsetid passeret — automatik slået fra",
                    live_soc=live_soc,
                )

        in_slot = bool(
            plan
            and plan.plan
            and any(b.start_ms <= now_ms < b.end_ms for b in plan.plan)
        )
        should_be_charging = rt.force_charge or in_slot

        # 5) Ramp-safe car-side stop tilstandsmaskine
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
                )
            return dec(
                ACT_WAITING,
                f"Allerede ved mål ({live_soc:.0f}%)",
                live_soc=live_soc,
            )

        # 7) Force charge
        if rt.force_charge:
            if not really_charging:
                return dec(
                    ACT_START,
                    f"Lad straks — {amps:.0f} A, venter på strøm",
                    live_soc=live_soc,
                    desired_current=amps,
                )
            return dec(
                ACT_CHARGING,
                f"Lad straks — lader {power:.1f} kW",
                live_soc=live_soc,
                desired_current=amps,
            )

        # 8) Flyder der strøm, men skal vi IKKE lade (uden for slot, ingen force)? → 0 A
        #    Køres FØR "ingen plan"/"afventer"-grenene, så en færdig eller tom plan
        #    også stopper en igangværende ladning.
        power_flowing = power > CHARGE_POWER_THRESHOLD_KW
        if power_flowing and not should_be_charging:
            return dec(
                ACT_PAUSE,
                "Uden for slot — stopper ladning (0 A)",
                live_soc=live_soc,
            )

        # 9) I slot?
        if in_slot:
            if not really_charging:
                return dec(
                    ACT_START,
                    f"I ladeslot — {amps:.0f} A, venter på strøm",
                    in_slot=True,
                    live_soc=live_soc,
                    desired_current=amps,
                )
            return dec(
                ACT_CHARGING,
                f"I ladeslot — lader {live_soc:.0f}%",
                in_slot=True,
                live_soc=live_soc,
                desired_current=amps,
            )

        # 10) Ikke i slot og ingen strøm → afventer
        if not plan or not plan.plan:
            reason = "Ingen ladeplan (afventer priser/valg)"
            if plan and plan.warning == planner.WARN_ALREADY_AT_TARGET:
                reason = "Allerede ved mål"
            elif plan and plan.warning == planner.WARN_NO_PRICES:
                reason = "Ingen prisdata i tidsvinduet"
                if not self._price_count:
                    reason = (
                        f"Ingen priser fra {self.price_sensor()} — vælg en anden "
                        "pris-sensor under Konfigurér → Indstillinger"
                    )
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

    def _warn_if_prices_end_before_departure(self, decision: Decision) -> None:
        """Planen kan kun bruge kendte priser. Ligger afgangen efter dem (fx fordi
        morgendagens priser mangler), siges det tydeligt i status."""
        rt = self.runtime
        if rt.active_vehicle == CHOOSE_VEHICLE or not rt.enabled or rt.force_charge:
            return
        if self.prices_until_ms is None:
            return
        deadline = self._deadline_ms()
        if deadline is None or deadline <= self.prices_until_ms:
            return
        until = dt_util.as_local(dt_util.utc_from_timestamp(self.prices_until_ms / 1000))
        decision.warning = "prices_end_before_departure"
        decision.reason += (
            f" · Priser kun til {until.strftime('%d/%m %H:%M')} — planen bruger ikke "
            "tiden derefter (mangler morgendagens priser?)"
        )

    # ---------- strømstyring ----------

    async def _reconcile_current(self, decision: Decision) -> None:
        """Sessionsstyring:

        - Kabel ud → max current 0 A, så næste bil ikke starter ved isætning.
        - Fra isætning til første ladeslot: 0 A ("waiting").
        - Første ladeslot / "Lad straks": max current = ladestrøm, ÉN gang ("switch").
        - Resten af sessionen styres KUN med ladekontakten (pause/genoptag);
          max current røres ikke, før kablet tages ud.
        """
        rt = self.runtime
        want_charge = decision.desired_current >= guards.MIN_CHARGE_AMPS
        entity = self.current_entity()
        actual = self._get_float(entity) if entity else None
        decision.actual_current = actual
        connected = decision.charger_mode not in (CM_DISCONNECTED, "unknown", "unavailable")

        if not entity:
            decision.current_note = (
                "Vælg strøm-entitet under Konfigurér → Indstillinger "
                "(fx number.zag089363_charger_max_current)"
            )
            return
        if decision.observer:
            decision.current_note = (
                f"Observatør: ville {'lade' if want_charge else 'ikke lade'} "
                f"(fase {rt.session_phase or 'ukendt'})"
            )
            return
        st = self.hass.states.get(entity)
        if st is None or st.state == "unavailable":
            # Kald til en utilgængelig entitet springes over af HA — sig det tydeligt
            decision.current_note = (
                f"Strøm-entiteten {entity} er utilgængelig — vælg en anden under "
                "Konfigurér → Indstillinger (fx number.zag089363_charger_max_current)"
            )
            return

        # Fase: ukendt (fx efter opgradering midt i en session) → aflæs laderen
        if rt.session_phase not in ("waiting", "switch"):
            in_session = connected and actual is not None and actual >= guards.MIN_CHARGE_AMPS
            rt.session_phase = "switch" if in_session else "waiting"
            await self.async_save()
        # Første ladeslot i sessionen → max current op, derefter kun ladekontakten
        if rt.session_phase == "waiting" and want_charge and connected:
            _LOGGER.info("Første ladeslot i sessionen — max current til ladestrøm")
            rt.session_phase = "switch"
            self._session_send_at = None
            await self.async_save()

        max_target = self.charge_amps() if rt.session_phase == "switch" else 0.0
        decision.max_current_target = max_target
        if not await self._reconcile_max(decision, entity, actual, max_target, connected):
            return
        if rt.session_phase != "switch":
            decision.current_note = (
                "0 A — venter på første ladeslot" if connected else "0 A — ingen bil tilsluttet"
            )
            return
        await self._reconcile_switch(decision, want_charge)

    async def _reconcile_max(
        self,
        decision: Decision,
        entity: str,
        actual: float | None,
        target: float,
        connected: bool,
    ) -> bool:
        """Sæt max current til ``target`` og bekræft. Returnerer True når den er på plads.

        I "switch"-fasen sendes den kun, til laderen har meldt værdien én gang —
        derefter røres den ikke resten af sessionen.
        """
        rt = self.runtime
        now = dt_util.utcnow()
        if rt.cur_target != target:
            rt.cur_target = target
            rt.cur_attempts = 0
            rt.cur_last_write_iso = ""
            rt.cur_confirmed = False
            rt.cur_last_error = ""
            await self.async_save()

        if rt.session_phase == "switch" and rt.cur_confirmed:
            return True  # max current røres ikke resten af sessionen

        session_resend = False
        if rt.session_phase == "waiting" and connected and self._session_send_at is not None:
            if now < self._session_send_at:
                self._wake_in((self._session_send_at - now).total_seconds() + 1)
                decision.current_note = f"Ny session — sender {target:.0f} A om lidt"
                return False
            session_resend = True

        setting_ok = actual is not None and abs(actual - target) < 0.5
        applied, why = setting_ok, ""
        if rt.session_phase == "waiting" and connected:
            # 0 A skal også være ANVENDT: ingen ladning og ChargeCurrentSet under 6 A
            charge_current_set = self._get_float(
                self._charger_entity("sensor", "allocated_charge_current")
            )
            decision.charge_current_set = charge_current_set
            applied, why = guards.current_applied(
                desired=target,
                setting=actual,
                charge_current_set=charge_current_set,
                power_flowing=decision.charge_power > CHARGE_POWER_THRESHOLD_KW,
            )
        if session_resend:
            applied, why = False, "ny session"

        if applied:
            if not rt.cur_confirmed:
                rt.cur_confirmed = True
                rt.cur_last_error = ""
                _LOGGER.info("Max current er %.0f A", target)
                await self.async_save()
            return True

        if rt.cur_confirmed and not self._write_inflight and not session_resend:
            if setting_ok:
                _LOGGER.warning(
                    "Laderen har ikke anvendt %.0f A (%s) — sender igen", target, why
                )
                rt.cur_last_error = f"ikke anvendt: {why}"
            else:
                _LOGGER.warning(
                    "Max current blev ændret udefra (%s A, ønsket %.0f A) — sætter den igen",
                    actual,
                    target,
                )
                rt.cur_last_error = f"ændret udefra til {actual} A"
            rt.cur_confirmed = False
            await self.async_save()

        def iso_ms(value: str) -> int | None:
            return planner.to_ms(value) if value else None

        action, wait_ms = guards.current_action(
            desired=target,
            # Gemt men ikke anvendt tæller som "ikke sat" → send igen (med pauser)
            actual=None if setting_ok else actual,
            write_inflight=self._write_inflight,
            attempts=rt.cur_attempts,
            last_write_ms=iso_ms(rt.cur_last_write_iso),
            last_change_ms=iso_ms(rt.cur_change_iso),
            # Zaptec: højst én ændring pr. 15 min — også gentagelser. Kun brugerens egne
            # handlinger (Stop, Lad straks, automatik til, bilvalg) sker med det samme.
            urgent=self._user_urgent(now),
            now_ms=planner.to_ms(now),
            confirm_ms=self._min_interval_ms(),
            retry_ms=(self._min_interval_ms(),),
            min_change_interval_ms=int(CURRENT_MIN_CHANGE_INTERVAL.total_seconds() * 1000),
        )
        if action == guards.CURRENT_WRITE:
            if session_resend:
                self._session_send_at = None
            if rt.cur_attempts == 0 and not setting_ok:
                rt.cur_change_iso = now.isoformat()
            rt.cur_attempts += 1
            rt.cur_last_write_iso = now.isoformat()
            await self.async_save()
            decision.actuated = True
            decision.current_note = self._current_note(target, actual, sending=True)
            self._write_inflight = True
            self.hass.async_create_task(self._write_current(entity, target))
            return False
        if wait_ms > 0:
            self._wake_in(wait_ms / 1000 + 1)
        decision.current_note = self._current_note(target, actual, wait_ms=wait_ms)
        return False

    async def _reconcile_switch(self, decision: Decision, want_charge: bool) -> None:
        """Pause/genoptag den igangværende session med Zaptecs ladekontakt."""
        rt = self.runtime
        now = dt_util.utcnow()
        sw_entity = self._charger_entity("switch", "charging")
        sw_state = self._get_state(sw_entity)
        charging = (
            decision.charger_mode == CM_CHARGING
            or decision.charge_power > CHARGE_POWER_THRESHOLD_KW
        )
        want = "on" if want_charge else "off"
        if rt.sw_want != want:
            rt.sw_want = want
            rt.sw_attempts = 0
            rt.sw_last_iso = ""
            await self.async_save()

        action, wait_ms = guards.switch_action(
            want_charge=want_charge,
            charging=charging,
            switch_state=sw_state,
            inflight=self._switch_inflight,
            attempts=rt.sw_attempts,
            last_cmd_ms=planner.to_ms(rt.sw_last_iso) if rt.sw_last_iso else None,
            now_ms=planner.to_ms(now),
            confirm_ms=self._min_interval_ms(),
            retry_ms=(self._min_interval_ms(),),
            urgent=self._user_urgent(now),
        )
        amps = f"{decision.max_current_target:.0f} A"
        if action in (guards.SWITCH_ON, guards.SWITCH_OFF):
            rt.sw_attempts += 1
            rt.sw_last_iso = now.isoformat()
            await self.async_save()
            decision.actuated = True
            verb = "Genoptager" if action == guards.SWITCH_ON else "Pauser"
            decision.current_note = f"{amps} · {verb} via ladekontakten (forsøg {rt.sw_attempts})"
            self._switch_inflight = True
            self.hass.async_create_task(self._switch_cmd(sw_entity, action))
            return
        if wait_ms > 0:
            self._wake_in(wait_ms / 1000 + 1)
        if action == guards.SWITCH_NONE:
            decision.current_note = f"{amps} · {'lader' if charging else 'pauset'}"
        elif self._switch_inflight:
            decision.current_note = f"{amps} · venter på Zaptec (ladekontakt)"
        elif want_charge and sw_state != "off":
            decision.current_note = f"{amps} · laderen kan ikke genoptages lige nu"
        else:
            decision.current_note = f"{amps} · {'genoptager' if want_charge else 'pauser'} snart"

    async def _switch_cmd(self, entity_id: str | None, action: str) -> None:
        """Send kontakt-kommandoen og VENT til kaldet er færdigt."""
        started = time.monotonic()
        service = "turn_on" if action == guards.SWITCH_ON else "turn_off"
        try:
            await self.hass.services.async_call(
                "switch", service, {"entity_id": entity_id}, blocking=True
            )
        except Exception as err:  # noqa: BLE001 — Zaptec-fejl må ikke vælte coordinatoren
            _LOGGER.warning("Ladekontakten (%s) fejlede: %s", service, err)
        finally:
            self._switch_inflight = False
        took = time.monotonic() - started
        if took > SLOW_CALL_WARNING.total_seconds():
            _LOGGER.warning(
                "Ladekontakten (%s) tog %.0f s — Zaptec svarer langsomt", service, took
            )
        else:
            _LOGGER.info("Ladekontakten: %s (%.1f s)", service, took)
        if not self._closed:
            self._wake_in(CURRENT_CONFIRM_DELAY.total_seconds() + 1)
            await self.async_request_refresh()

    def _current_note(
        self, desired: float, actual: float | None, *, sending: bool = False, wait_ms: int = 0
    ) -> str:
        rt = self.runtime
        have = "ukendt" if actual is None else f"{actual:.0f} A"
        if sending:
            note = f"Sætter {desired:.0f} A (laderen: {have}, forsøg {rt.cur_attempts})"
        elif self._write_inflight:
            note = f"Sætter {desired:.0f} A — venter på Zaptec"
        elif wait_ms > 0:
            at = dt_util.as_local(dt_util.utcnow() + timedelta(milliseconds=wait_ms))
            note = (
                f"Sætter {desired:.0f} A (laderen: {have}) — "
                f"næste forsøg {at.strftime('%H:%M')}"
            )
        else:
            note = f"Sætter {desired:.0f} A (laderen: {have})"
        if rt.cur_last_error:
            note += f" — seneste fejl: {rt.cur_last_error}"
        if self._get_state(self._charger_entity("binary_sensor", "online")) == "off":
            note = "Laderen er offline — " + note
        return note

    @staticmethod
    def _min_interval_ms() -> int:
        return int(CURRENT_MIN_CHANGE_INTERVAL.total_seconds() * 1000)

    def _user_urgent(self, now: datetime) -> bool:
        return self._urgent_until is not None and now < self._urgent_until

    def _charger_entity(self, domain: str, suffix: str) -> str | None:
        """Zaptec-entitet for samme lader (fx sensor.<lader>_allocated_charge_current)."""
        return guards.charger_entity(self._cfg(CONF_CHARGER_MODE_SENSOR), domain, suffix)

    async def _write_current(self, entity_id: str, value: float) -> None:
        """Send strømmen og VENT til kaldet er færdigt (inkl. Zaptecs egne genforsøg)."""
        rt = self.runtime
        started = time.monotonic()
        try:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": entity_id, "value": value},
                blocking=True,
            )
            rt.cur_last_error = ""
        except Exception as err:  # noqa: BLE001 — Zaptec-fejl må ikke vælte coordinatoren
            rt.cur_last_error = str(err)[:200] or type(err).__name__
            _LOGGER.warning("Kunne ikke sætte %s til %.0f A: %s", entity_id, value, err)
        finally:
            self._write_inflight = False
        took = time.monotonic() - started
        if took > SLOW_CALL_WARNING.total_seconds():
            _LOGGER.warning(
                "Zaptec-kaldet (%.0f A) tog %.0f s — Zaptec svarer langsomt", value, took
            )
        else:
            _LOGGER.info("Strøm sat til %.0f A (%.1f s)", value, took)
        await self.async_save()
        if not self._closed:
            self._wake_in(CURRENT_CONFIRM_DELAY.total_seconds() + 1)
            await self.async_request_refresh()

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

    def _update_start_failure_timer(self, decision: Decision, mode: str) -> None:
        """Notificér én gang hvis der skal lades, men der ikke kommer strøm."""
        rt = self.runtime
        now = dt_util.utcnow()
        wait_ms = (
            planner.to_ms(rt.start_wait_since_iso) if rt.start_wait_since_iso else None
        )
        new_wait, should_notify = guards.start_failure_state(
            should_be_charging=decision.desired_current > 0,
            power_flowing=decision.charge_power > CHARGE_POWER_THRESHOLD_KW,
            wait_since_ms=wait_ms,
            now_ms=planner.to_ms(now),
            timeout_ms=int(START_FAILED_TIMEOUT.total_seconds() * 1000),
            already_notified=rt.start_failed_notified,
        )
        if new_wait is None:
            if rt.start_wait_since_iso or rt.start_failed_notified:
                rt.start_wait_since_iso = ""
                rt.start_failed_notified = False
                self.hass.async_create_task(self.async_save())
            return
        if not rt.start_wait_since_iso:
            rt.start_wait_since_iso = now.isoformat()
            self.hass.async_create_task(self.async_save())
        if not should_notify:
            return
        rt.start_failed_notified = True
        amps = decision.desired_current
        if not rt.cur_confirmed:
            msg = (
                f"Zaptec har ikke bekræftet {amps:.0f} A endnu — planneren bliver "
                "ved med at prøve, til det lykkes."
            )
        elif mode == CM_REQUESTING:
            msg = (
                f"Laderen har {amps:.0f} A, men venter på autorisation. Er "
                "autorisation slået til i Zaptec?"
            )
        else:
            msg = (
                f"Laderen giver {amps:.0f} A, men bilen trækker ikke strøm. "
                "Sover bilen, eller er dens ladegrænse nået?"
            )
        _LOGGER.warning("Ingen strøm i ladeslot: %s", msg)
        self._notify("⚠️ Bilen lader ikke", msg, "notify_not_enough_time")
        self.hass.async_create_task(self.async_save())

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

    # ---------- mode-overgange (kabel ud / ny isætning) ----------

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
        rt.start_wait_since_iso = ""
        rt.start_failed_notified = False
        # Kabel ud → max current 0 A, så næste bil ikke starter; ny session venter
        # på første ladeslot, før der lades.
        rt.session_phase = "waiting"
        # Kabel ud er en handling fra brugeren: 0 A sendes straks (ikke efter
        # 15-minutters-reglen), så en ny bil ikke når at starte.
        self.mark_urgent()
        rt.sw_want = ""
        rt.sw_attempts = 0
        rt.sw_last_iso = ""
        # Kabel ud → et manuelt valgt afgangstidspunkt gælder ikke længere: næste
        # tilslutning lader til næste kl. 07:00, medmindre brugeren selv ændrer det
        # (også før stikket sættes i — derfor nulstilles her og ikke ved isætning).
        self.reset_departure()
        self._set_plan(None)
        await self.async_save()

    async def _handle_mode_transition(self, prev: str | None, now: str) -> None:
        """Ny session afgøres KUN af kabel ud → kabel i.

        Når strømmen skifter mellem 0 og ladestrøm, skifter laderen selv mellem fx
        requesting/charging/finished. Det må aldrig tolkes som et bilskifte, så der
        gættes ikke længere på det — Zaptec melder "disconnected" i realtid.
        """
        if prev == now:
            return
        rt = self.runtime
        # Frakoblet: laderen er taget ud af bilen → fravælg bilen og nulstil,
        # så en gammel valgt bil ikke hænger ved og udløser beregning/notifikationer.
        if now == CM_DISCONNECTED and prev not in (None, CM_DISCONNECTED):
            _LOGGER.info("Laderen frakoblet (%s → %s) — fravælger bil", prev, now)
            await self._reset_for_disconnect()
            return
        if prev in _NEW_SESSION_FROM and now not in (CM_DISCONNECTED, "unknown", "unavailable"):
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
            rt.start_wait_since_iso = ""
            rt.start_failed_notified = False
            # Zaptec læser MaxCurrent, når sessionen starter → send værdien igen bagefter
            self._session_send_at = dt_util.utcnow() + SESSION_SETTLE
            self._wake_in(SESSION_SETTLE.total_seconds() + 1)
            self._set_plan(None)
            self._notify(
                "🔌 Bil tilsluttet",
                "Vælg hvilken bil du vil lade",
                "notify_cable_connected",
            )
            await self.async_save()
