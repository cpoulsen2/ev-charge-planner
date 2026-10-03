"""End-to-end simulation af sessionsstyringen mod en falsk Zaptec-lader (rigtig HA-kerne).

Max current 0 A fra kabel ud til første ladeslot, derefter 16 A én gang, og resten af
sessionen styres med ladekontakten (pause/genoptag). Kald kan hænge, fejle eller blive
meldt tilbage forsinket.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.ev_charge_planner as integration
from custom_components.ev_charge_planner import coordinator as coord_mod
from custom_components.ev_charge_planner.const import CHOOSE_VEHICLE, DOMAIN
from custom_components.ev_charge_planner.coordinator import EvcpCoordinator
from custom_components.ev_charge_planner.models import RuntimeStore
from custom_components.ev_charge_planner.planner import PlanBlock, PlanResult

MODE = "sensor.zag_charger_mode"
POWER = "sensor.zag_charge_power"
ENERGY = "sensor.zag_session_energy"
CUR = "number.zag089363_available_current"
SOC = "sensor.modely_battery_level"
CCS = "sensor.zag_allocated_charge_current"  # Zaptec ChargeCurrentSet
ONLINE = "binary_sensor.zag_online"
SW = "switch.zag_charging"  # Zaptecs ladekontakt

REQ, CHG, FIN, DISC = (
    "connected_requesting",
    "connected_charging",
    "connected_finished",
    "disconnected",
)

T_SLOT = datetime(2026, 10, 3, 1, 0, 0, tzinfo=timezone.utc)  # 03:00 dansk


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def S(sec: float) -> datetime:
    return T_SLOT + timedelta(seconds=sec)


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


class Zaptec:
    """Falsk Zaptec-lader: max current + ladekontakt (pause/genoptag via FinalStopActive)."""

    def __init__(self, hass) -> None:
        self.hass = hass
        self.plugged = True
        self.car_draws = True
        self.current = 0.0  # max current (indstillingen)
        self.final_stop = False  # pauset med stop_charging_final
        self.writes: list[float] = []
        self.cmds: list[str] = []  # "on"/"off" på ladekontakten
        self.fail_next = 0
        self.switch_fail_next = 0
        self.switch_gate: asyncio.Event | None = None
        self.gate: asyncio.Event | None = None
        self.lag = False
        self.pending: float | None = None
        self.concurrent = 0
        self.max_concurrent = 0
        # Strøm laderen faktisk bruger (ChargeCurrentSet). None = følger indstillingen;
        # sat til et tal = laderen har IKKE fået nye værdier (fx offline).
        self.frozen_ccs: float | None = None
        self.online = True

    @property
    def ccs(self) -> float:
        return self.current if self.frozen_ccs is None else self.frozen_ccs

    @property
    def mode(self) -> str:
        if not self.plugged:
            return DISC
        if self.ccs < 6:
            return REQ
        if self.final_stop:
            return FIN
        return CHG if self.car_draws else FIN

    @property
    def switch_state(self) -> str:
        if self.mode == CHG:
            return "on"
        if self.mode == FIN and self.final_stop:
            return "off"  # pauset → resume er gyldig
        return "unavailable"

    def publish(self) -> None:
        self.hass.states.async_set(MODE, self.mode)
        self.hass.states.async_set(POWER, "11.0" if self.mode == CHG else "0.0")
        self.hass.states.async_set(CUR, str(self.current), {"min": 0, "max": 32})
        self.hass.states.async_set(CCS, str(self.ccs))
        self.hass.states.async_set(ONLINE, "on" if self.online else "off")
        self.hass.states.async_set(SW, self.switch_state)

    def unplug(self) -> None:
        self.plugged = False
        self.final_stop = False
        self.publish()

    def plug(self) -> None:
        self.plugged = True
        self.publish()

    async def set_value(self, call) -> None:
        value = float(call.data["value"])
        self.writes.append(value)
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.fail_next > 0:
                self.fail_next -= 1
                raise HomeAssistantError("Set current limit failed (timeout)")
            if self.lag:
                self.pending = value
                return
            self.current = value
            self.publish()
        finally:
            self.concurrent -= 1

    async def switch_on(self, call) -> None:
        self.cmds.append("on")
        if self.switch_fail_next > 0:
            self.switch_fail_next -= 1
            raise HomeAssistantError("Resuming charging failed")
        if self.mode == FIN and self.final_stop:
            self.final_stop = False
        self.publish()

    async def switch_off(self, call) -> None:
        self.cmds.append("off")
        if self.switch_gate is not None:
            await self.switch_gate.wait()
        if self.switch_fail_next > 0:
            self.switch_fail_next -= 1
            raise HomeAssistantError("Stop/pausing charging failed")
        if self.mode == CHG:
            self.final_stop = True
        self.publish()

    def apply_pending(self) -> None:
        if self.pending is not None:
            self.current = self.pending
            self.pending = None
            self.publish()


@pytest.fixture
def expected_lingering_timers() -> bool:
    return True


def _entry(**opts) -> MockConfigEntry:
    options = {
        "vehicles": [
            {"name": "Model Y", "capacity_kwh": 79, "soc_sensor": SOC, "soc_live": True}
        ],
        "current_entity": CUR,
        "charge_current": 16,
    }
    options.update(opts)
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "price_sensor": "sensor.price",
            "charger_mode_sensor": MODE,
            "charge_power_sensor": POWER,
            "session_energy_sensor": ENERGY,
        },
        options=options,
    )


SLOT2 = 2 * 3600  # andet ladeslot starter 2 t efter det første


@pytest.fixture
async def sim(hass, monkeypatch):
    clock = Clock(S(-600))
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", clock)
    tz = coord_mod.dt_util.DEFAULT_TIME_ZONE
    monkeypatch.setattr(
        coord_mod.dt_util, "now", lambda time_zone=None: clock.t.astimezone(time_zone or tz)
    )

    zap = Zaptec(hass)
    zap.publish()
    hass.states.async_set(ENERGY, "0.0")
    hass.states.async_set(SOC, "58")
    hass.services.async_register("number", "set_value", zap.set_value)
    hass.services.async_register("switch", "turn_on", zap.switch_on)
    hass.services.async_register("switch", "turn_off", zap.switch_off)

    entry = _entry()
    entry.add_to_hass(hass)

    async def make() -> EvcpCoordinator:
        store = RuntimeStore(hass, entry.entry_id)
        await store.load()
        c = EvcpCoordinator(hass, entry, store)
        c.async_request_refresh = AsyncMock()
        return c

    c = await make()
    rt = c.runtime
    rt.active_vehicle = "Model Y"
    rt.enabled = True
    rt.observer_mode = False
    rt.departure_iso = S(6 * 3600).isoformat()
    rt.target_soc = 80
    rt.session_phase = "waiting"  # bilen er sat i; 0 A indtil første ladeslot
    c._prev_charger_mode = zap.mode
    c._set_plan(
        PlanResult(
            plan=[
                PlanBlock(ms(T_SLOT), ms(T_SLOT + timedelta(minutes=30)), 0.4, 5.5, 2.2, 30),
                PlanBlock(
                    ms(S(SLOT2)), ms(S(SLOT2 + 15 * 60)), 0.4, 2.75, 1.1, 15
                ),
            ]
        )
    )
    await c.async_save()

    async def tick(at: datetime, coordinator: EvcpCoordinator | None = None):
        clock.t = at
        d = await (coordinator or c)._async_update_data()
        if zap.gate is None and zap.switch_gate is None:
            await hass.async_block_till_done()
        else:
            for _ in range(5):
                await asyncio.sleep(0)
        return d

    yield c, zap, clock, tick, make, entry
    c.async_close()


def _quarters(start: datetime, n: int, price: float) -> list[dict]:
    return [
        {
            "start": (start + timedelta(minutes=15 * i)).isoformat(),
            "end": (start + timedelta(minutes=15 * (i + 1))).isoformat(),
            "price": price,
        }
        for i in range(n)
    ]


async def test_current_price_sensor_format_without_duplicates(sim, hass):
    """sensor.stromligning_current_price_vat har kun attributten "prices"."""
    c, zap, clock, tick, *_ = sim
    clock.t = S(-3600)
    hass.states.async_set("sensor.price", "1.0", {"prices": _quarters(S(-3600), 96, 1.0)})
    # samme kvarterer også på morgendags-sensoren → må ikke tælle dobbelt
    hass.states.async_set(
        "binary_sensor.price_tomorrow", "on", {"prices_tomorrow": _quarters(S(0), 8, 1.0)}
    )
    today, tomorrow = c._prices()
    assert len(today) == 96 and tomorrow == []
    c.runtime.departure_iso = (S(4 * 3600)).isoformat()
    c.recalculate()
    assert c.plan_result is not None and c.plan_result.plan
    starts = [b.start_ms for b in c.plan_result.plan]
    assert len(starts) == len(set(starts))


async def test_missing_prices_gives_clear_reason(sim, hass):
    c, zap, clock, tick, *_ = sim
    clock.t = S(-3600)
    zap.car_draws = False  # laderen lader ikke lige nu
    zap.publish()
    hass.states.async_set("sensor.price", "1.0", {})
    c.runtime.departure_iso = (S(4 * 3600)).isoformat()
    c.recalculate()
    d = await tick(S(-3600))
    assert "Ingen priser fra sensor.price" in d.reason


async def test_unavailable_current_entity_is_explained_and_not_written(sim, hass):
    c, zap, _, tick, *_ = sim
    hass.states.async_set(CUR, "unavailable")
    d = await tick(S(-1000))
    assert zap.writes == []
    assert "utilgængelig" in d.current_note


def _is_next_7(dep_iso: str, now: datetime) -> bool:
    from custom_components.ev_charge_planner.coordinator import dt_util

    dep = dt_util.parse_datetime(dep_iso)
    local = dt_util.as_local(dep)
    return (
        local.hour == 7
        and local.minute == 0
        and dep > now
        and dep - now <= timedelta(hours=24)
    )


async def test_manual_departure_holds_until_unplug_then_7(sim):
    """Brugerens forløb: sæt i morgen kl. 17 + aktivér; kabel ud → næste kl. 07."""
    c, zap, clock, tick, *_ = sim
    manual = S(36 * 3600).isoformat()  # "i morgen kl. 17"
    c.runtime.departure_iso = manual
    c.runtime.enabled = True
    for sec in (-1000, 0, 600, 3600):
        await tick(S(sec))
        assert c.runtime.departure_iso == manual, "manuelt valg røres ikke"
    zap.plugged = False
    zap.publish()
    await tick(S(4000))
    assert _is_next_7(c.runtime.departure_iso, S(4000)), "kabel ud → næste kl. 07"
    zap.plugged = True
    zap.publish()
    await tick(S(4100))
    assert _is_next_7(c.runtime.departure_iso, S(4100))


async def test_manual_departure_set_before_plugin_survives(sim):
    c, zap, clock, tick, *_ = sim
    zap.plugged = False
    zap.publish()
    await tick(S(-900))
    manual = S(30 * 3600).isoformat()
    c.runtime.departure_iso = manual  # valgt mens bilen ikke er sat i
    zap.plugged = True
    zap.publish()
    await tick(S(-800))
    assert c.runtime.departure_iso == manual


async def test_passed_departure_rolls_to_next_morning(sim):
    """Som den gamle Standard: bliver bilen siddende, gælder næste kl. 07:00."""
    c, zap, clock, tick, *_ = sim
    c.runtime.departure_iso = S(-60).isoformat()
    d = await tick(S(0))
    assert _is_next_7(c.runtime.departure_iso, S(0))
    assert c.runtime.enabled, "automatikken slår ikke sig selv fra"
    assert d.action != "idle"


async def test_runtime_has_no_charge_mode():
    from custom_components.ev_charge_planner.models import Runtime

    assert not hasattr(Runtime(), "mode")


async def test_old_mode_select_is_removed(hass):
    from homeassistant.helpers import entity_registry as er

    from custom_components.ev_charge_planner import select as select_platform

    entry = _entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "select", DOMAIN, f"{entry.entry_id}_mode", config_entry=entry
    )
    assert registry.async_get_entity_id("select", DOMAIN, f"{entry.entry_id}_mode")

    class _C:
        pass

    coordinator = _C()
    coordinator.entry = entry
    coordinator.runtime = None
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    added = []
    try:
        await select_platform.async_setup_entry(hass, entry, added.extend)
    except Exception:  # noqa: BLE001 — kun fjernelsen testes her
        pass
    assert registry.async_get_entity_id("select", DOMAIN, f"{entry.entry_id}_mode") is None


async def test_settings_step_can_change_price_and_current_entity(hass):
    from custom_components.ev_charge_planner.config_flow import EvcpOptionsFlow

    entry = _entry()
    entry.add_to_hass(hass)
    flow = EvcpOptionsFlow(entry)
    flow.hass = hass
    form = await flow.async_step_settings()
    assert form["step_id"] == "settings"
    fields = [str(k) for k in form["data_schema"].schema]
    assert {"price_sensor", "tomorrow_sensor", "current_entity", "charge_current"} <= set(fields)
    result = await flow.async_step_settings(
        {
            "price_sensor": "sensor.stromligning_adjusted_price_vat",
            "current_entity": "number.zag089363_charger_max_current",
            "charge_current": 16,
            "min_block_minutes": 0,
        }
    )
    assert result["type"] == "create_entry"
    data = result["data"]
    assert data["price_sensor"] == "sensor.stromligning_adjusted_price_vat"
    assert data["current_entity"] == "number.zag089363_charger_max_current"
    assert data["tomorrow_sensor"] is None
    assert data["vehicles"], "bilerne bevares"


async def test_wrong_tomorrow_sensor_falls_back_and_plans_tomorrow(sim, hass):
    """03-10: afgang i morgen kl. 17, men planen lå i aften — morgendagens priser manglede."""
    c, zap, clock, tick, make, entry = sim
    now = S(-3600)
    clock.t = now
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "tomorrow_sensor": "binary_sensor.wrong_tomorrow"}
    )
    hass.states.async_set("binary_sensor.wrong_tomorrow", "off", {})
    hass.states.async_set("sensor.price", "2.4", {"prices_today": _quarters(now, 96, 2.4)})
    tomorrow = _quarters(now + timedelta(hours=24), 96, 2.0)
    for q in tomorrow[40:64]:  # 6 timer billig strøm i morgen
        q["price"] = 0.9
    hass.states.async_set("binary_sensor.price_tomorrow", "on", {"prices_tomorrow": tomorrow})
    c.runtime.departure_iso = (now + timedelta(hours=40)).isoformat()
    c.recalculate()
    blocks = c.plan_result.plan
    assert blocks, "der skal være en plan"
    cheap_start = planner_ms(now + timedelta(hours=34))
    assert all(b.start_ms >= cheap_start for b in blocks), "planen skal ligge i de billige timer i morgen"
    d = await tick(now)
    assert "Priser kun til" not in d.reason


async def test_missing_tomorrow_prices_are_explained(sim, hass):
    c, zap, clock, tick, *_ = sim
    now = S(-3600)
    clock.t = now
    zap.car_draws = False
    zap.publish()
    hass.states.async_set("sensor.price", "2.4", {"prices_today": _quarters(now, 20, 2.4)})
    c.runtime.departure_iso = (now + timedelta(hours=40)).isoformat()
    c.recalculate()
    d = await tick(now)
    assert "Priser kun til" in d.reason
    assert d.warning == "prices_end_before_departure"


def planner_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


async def test_tomorrow_sensor_with_prices_attribute(sim, hass):
    """binary_sensor.stromligning_tomorrow_spotprice_vat har priserne under "prices"."""
    c, zap, clock, tick, make, entry = sim
    now = S(-3600)
    clock.t = now
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "tomorrow_sensor": "binary_sensor.spot_tomorrow"}
    )
    hass.states.async_set("sensor.price", "2.4", {"prices": _quarters(now, 96, 2.4)})
    tomorrow = _quarters(now + timedelta(hours=24), 96, 2.0)
    for q in tomorrow[40:64]:
        q["price"] = 0.6
    hass.states.async_set("binary_sensor.spot_tomorrow", "on", {"prices": tomorrow})
    today, tmr = c._prices()
    assert len(tmr) == 96
    c.runtime.departure_iso = (now + timedelta(hours=40)).isoformat()
    c.recalculate()
    cheap_start = planner_ms(now + timedelta(hours=34))
    assert c.plan_result.plan
    assert all(b.start_ms >= cheap_start for b in c.plan_result.plan)


async def test_charger_max_current_is_used_even_if_other_entity_configured(sim, hass):
    """03-10: available_current var utilgængelig — laderens max-strøm skal bruges."""
    c, zap, clock, tick, *_ = sim
    max_cur = "number.zag_charger_max_current"  # udledt af sensor.zag_charger_mode
    writes: list[tuple[str, float]] = []

    async def set_value(call):
        writes.append((call.data["entity_id"], float(call.data["value"])))
        if call.data["entity_id"] == max_cur:
            zap.current = float(call.data["value"])
            zap.publish()
            hass.states.async_set(max_cur, str(zap.current), {"min": 0, "max": 20})

    hass.services.async_register("number", "set_value", set_value)
    hass.states.async_set(CUR, "unavailable")
    hass.states.async_set(max_cur, "0.0", {"min": 0, "max": 20})
    zap.current = 0.0
    zap.publish()
    hass.states.async_set(CUR, "unavailable")
    hass.states.async_set(max_cur, "0.0", {"min": 0, "max": 20})
    assert c.current_entity() == max_cur
    await tick(S(-1000))
    d = await tick(S(0))
    assert writes == [(max_cur, 16.0)], "ingen kald til den utilgængelige entitet"
    assert zap.mode == CHG and d.desired_current == 16



# ---------- Zaptec-anbefalinger: session, ChargeCurrentSet, offline ----------


# ---------- sessionsstyring: 0 A → 16 A ved første slot → ladekontakten ----------


async def test_zero_amps_until_first_slot_then_16a(sim):
    c, zap, _, tick, *_ = sim
    for sec in (-600, -300, -60):
        d = await tick(S(sec))
        assert d.charge_power == 0 and zap.mode == REQ
    assert zap.writes == [] and zap.cmds == []
    assert "venter på første ladeslot" in d.current_note
    await tick(S(0))
    assert zap.writes == [16.0] and zap.mode == CHG
    assert c.runtime.session_phase == "switch"
    d = await tick(S(60))
    assert d.action == "charging" and zap.cmds == []


async def test_rest_of_session_uses_switch_and_never_touches_max(sim):
    c, zap, _, tick, *_ = sim
    await tick(S(0))
    await tick(S(60))
    await tick(S(1800))  # slot 1 slut → pause med kontakten
    assert zap.cmds == ["off"] and zap.mode == FIN and zap.final_stop
    await tick(S(1900))
    assert zap.cmds == ["off"], "ingen gentagelse når den er pauset"
    await tick(S(SLOT2))  # slot 2 → genoptag med kontakten
    assert zap.cmds == ["off", "on"] and zap.mode == CHG
    await tick(S(SLOT2 + 15 * 60))  # slot 2 slut
    assert zap.cmds == ["off", "on", "off"]
    assert zap.writes == [16.0], "max current røres ikke i sessionen"
    assert c.runtime.active_vehicle == "Model Y" and c.runtime.enabled


async def test_unplug_sets_0a_and_next_car_does_not_start(sim):
    c, zap, _, tick, *_ = sim
    await tick(S(0))
    await tick(S(60))
    assert zap.current == 16
    zap.unplug()
    await tick(S(300))
    assert zap.writes == [16.0, 0.0], "kabel ud → 0 A (også uden session)"
    assert c.runtime.session_phase == "waiting"
    zap.plug()
    d = await tick(S(400))
    assert zap.mode == REQ and d.charge_power == 0, "næste bil starter ikke"
    await tick(S(421))
    assert zap.writes == [16.0, 0.0, 0.0], "0 A sendes igen, når sessionen findes"
    assert zap.cmds == []


async def test_lad_straks_sets_16a_and_stop_pauses_with_switch(sim):
    c, zap, clock, tick, *_ = sim
    clock.t = S(-900)
    c.runtime.force_charge = True
    c.on_user_restart()
    await tick(S(-900))
    assert zap.writes == [16.0] and zap.mode == CHG
    clock.t = S(-800)
    await c.async_stop_charging()
    await c.hass.async_block_till_done()
    assert zap.cmds == ["off"] and zap.mode == FIN
    assert zap.writes == [16.0], "Stop pauser med kontakten — max current bliver"


async def test_max_current_not_touched_even_if_changed_during_session(sim):
    c, zap, _, tick, *_ = sim
    await tick(S(0))
    await tick(S(60))
    zap.current = 10.0  # nogen ændrer den
    zap.publish()
    for sec in (120, 600, 1200):
        await tick(S(sec))
    assert zap.writes == [16.0]


async def test_first_slot_write_fails_then_recovers(sim):
    """Natten til 2/10: Zaptec svarer ikke — planneren bliver ved."""
    c, zap, _, tick, *_ = sim
    zap.fail_next = 2
    await tick(S(0))
    assert zap.writes == [16.0] and zap.current == 0
    await tick(S(30))
    assert len(zap.writes) == 1, "venter 60 s"
    await tick(S(61))
    assert len(zap.writes) == 2  # fejler igen
    await tick(S(61 + 121))
    assert len(zap.writes) == 3 and zap.current == 16 and zap.mode == CHG
    assert zap.max_concurrent == 1


async def test_never_two_max_calls_at_once(sim):
    c, zap, _, tick, *_ = sim
    zap.gate = asyncio.Event()
    for sec in (0, 60, 300, 600):
        await tick(S(sec))
    assert zap.writes == [16.0]
    gate, zap.gate = zap.gate, None
    gate.set()
    await c.hass.async_block_till_done()
    d = await tick(S(660))
    assert zap.current == 16 and d.action == "charging"


async def test_switch_command_fails_then_retries_with_backoff(sim):
    c, zap, _, tick, *_ = sim
    await tick(S(0))
    await tick(S(60))
    zap.switch_fail_next = 1
    await tick(S(1800))
    assert zap.cmds == ["off"] and zap.mode == CHG  # fejlede
    await tick(S(1830))
    assert zap.cmds == ["off"], "venter 60 s"
    await tick(S(1861))
    assert zap.cmds == ["off", "off"] and zap.mode == FIN


async def test_switch_unavailable_sends_nothing(sim):
    """Bilen er selv holdt op (ikke pauset) → resume er ugyldig → ingen kommando."""
    c, zap, _, tick, *_ = sim
    await tick(S(0))
    zap.car_draws = False
    zap.publish()
    assert zap.switch_state == "unavailable"
    for sec in (60, 120, 300):
        d = await tick(S(sec))
    assert zap.cmds == []
    assert "kan ikke genoptages" in d.current_note


async def test_upgrade_mid_session_keeps_16a(sim):
    c, zap, _, tick, *_ = sim
    zap.current = 16.0
    zap.publish()
    c.runtime.session_phase = ""  # ukendt (fx lige opgraderet)
    await tick(S(60))
    assert c.runtime.session_phase == "switch"
    assert zap.writes == []


async def test_waiting_phase_charging_at_0a_is_resent(sim):
    """Indstillingen siger 0 A, men laderen lader alligevel → send 0 A igen."""
    c, zap, _, tick, *_ = sim
    await tick(S(-900))
    zap.frozen_ccs = 16.0
    zap.publish()
    c.hass.states.async_set(CCS, "unavailable")  # kun effekten afslører det
    await tick(S(-800))
    assert zap.writes == [0.0]


async def test_offline_charger_is_reported(sim):
    c, zap, _, tick, *_ = sim
    zap.online = False
    zap.fail_next = 1
    zap.publish()
    await tick(S(0))
    d = await tick(S(30))
    assert d.current_note.startswith("Laderen er offline")


async def test_force_charge_without_vehicle(sim):
    c, zap, _, tick, *_ = sim
    c.runtime.active_vehicle = CHOOSE_VEHICLE
    c.runtime.force_charge = True
    c.on_user_restart()
    await tick(S(-990))
    assert zap.current == 16 and zap.mode == CHG


async def test_observer_never_touches_the_charger(sim):
    c, zap, _, tick, *_ = sim
    c.runtime.observer_mode = True
    for sec in (-600, 0, 60, 1800):
        d = await tick(S(sec))
    assert zap.writes == [] and zap.cmds == []
    assert "Observatør" in d.current_note


async def test_restart_mid_session_does_not_rewrite(sim):
    c, zap, _, tick, make, _ = sim
    await tick(S(0))
    await tick(S(60))
    c.async_close()
    c2 = await make()
    c2.restore_plan()  # som async_setup_entry gør ved opstart
    c2._prev_charger_mode = zap.mode
    await tick(S(120), c2)
    assert zap.writes == [16.0] and zap.cmds == []
    c2.async_close()


async def test_car_not_drawing_notifies_once(sim):
    c, zap, _, tick, *_ = sim
    sent = []
    c._notify = lambda title, msg, ntype: sent.append(msg)
    zap.car_draws = False
    for sec in range(0, 600, 60):
        await tick(S(sec))
    assert zap.current == 16
    assert len(sent) == 1 and "trækker ikke strøm" in sent[0]


async def test_disable_restores_16a_and_resumes(sim, hass):
    c, zap, _, tick, *_ = sim
    entry = c.entry
    await tick(S(0))
    await tick(S(60))
    await tick(S(1800))  # pauset med kontakten
    assert zap.mode == FIN
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = c
    await integration.async_unload_entry(hass, entry)  # genindlæsning: rør intet
    await hass.async_block_till_done()
    assert zap.mode == FIN and zap.cmds == ["off"]

    c2 = EvcpCoordinator(hass, entry, c.store)
    hass.data[DOMAIN][entry.entry_id] = c2
    object.__setattr__(entry, "disabled_by", ConfigEntryDisabler.USER)
    await integration.async_unload_entry(hass, entry)
    await hass.async_block_till_done()
    assert zap.current == 16 and zap.mode == CHG, "slået fra → normal lader igen"


async def test_never_two_switch_commands_at_once(sim):
    c, zap, _, tick, *_ = sim
    await tick(S(0))
    await tick(S(60))
    zap.switch_gate = asyncio.Event()  # Zaptec svarer ikke på pause-kommandoen
    for sec in (1800, 1861, 2000, 2400):
        await tick(S(sec))
    assert zap.cmds == ["off"], "kun én kommando mens den hænger"
    gate, zap.switch_gate = zap.switch_gate, None
    gate.set()
    await c.hass.async_block_till_done()
    await tick(S(2500))
    assert zap.mode == FIN and zap.cmds == ["off"]
