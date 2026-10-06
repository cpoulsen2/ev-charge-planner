"""End-to-end simulation af laderstyringen mod en falsk Zaptec Go (rigtig HA-kerne).

Max-strømmen står fast på 16 A; ladningen styres med pause (stop_charging_final) og
genoptag (resume_charging). Kald kan hænge eller fejle.
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
ONLINE = "binary_sensor.zag_online"
STOP = "button.zag_stop_charging"  # stop_charging_final (pause)
RESUME = "button.zag_resume_charging"  # resume_charging (genoptag)

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
    """Falsk Zaptec Go: max-strøm + pause/genoptag (stop_charging_final/resume_charging).

    Gyldighed som i Zaptec-integrationen: resume kun når laderen er pauset
    (Connected_Finished + FinalStopActive=1), stop ikke når pauset eller frakoblet.
    Autorisation er slået fra, så en ikke-pauset lader lader, når bilen vil.
    """

    def __init__(self, hass) -> None:
        self.hass = hass
        self.plugged = True
        self.car = "draws"  # "draws" | "asleep" (requesting) | "full" (finished, ikke pauset)
        self.current = 16.0  # max-strøm
        self.paused = False  # FinalStopActive
        # Lige efter isætning forhandler bil og lader (requesting); en pause sendt dér
        # bliver tilsidesat, når laderen går i gang ~1 s senere.
        self.negotiating = False
        self.paused_while_negotiating = False
        self.writes: list[float] = []
        self.cmds: list[str] = []  # "stop"/"resume"
        self.fail_next = 0
        self.cmd_fail_next = 0
        self.gate: asyncio.Event | None = None
        self.cmd_gate: asyncio.Event | None = None
        self.online = True

    @property
    def car_draws(self) -> bool:
        return self.car == "draws"

    @property
    def mode(self) -> str:
        if not self.plugged:
            return DISC
        if self.paused:
            return FIN
        if self.negotiating:
            return REQ
        if self.current < 6:
            return REQ
        return {"draws": CHG, "asleep": REQ, "full": FIN}[self.car]

    @property
    def stop_valid(self) -> bool:
        return self.plugged and not self.paused

    @property
    def resume_valid(self) -> bool:
        return self.plugged and self.paused

    def publish(self) -> None:
        if not self.plugged:
            self.paused = False
        self.hass.states.async_set(MODE, self.mode)
        self.hass.states.async_set(POWER, "11.0" if self.mode == CHG else "0.0")
        self.hass.states.async_set(CUR, str(self.current), {"min": 0, "max": 32})
        self.hass.states.async_set(ONLINE, "on" if self.online else "off")
        self.hass.states.async_set(STOP, "unknown" if self.stop_valid else "unavailable")
        self.hass.states.async_set(RESUME, "unknown" if self.resume_valid else "unavailable")

    def unplug(self) -> None:
        self.plugged = False
        self.publish()

    def plug(self) -> None:
        self.plugged = True
        self.negotiating = self.car == "draws"
        self.publish()

    def finish_negotiation(self) -> None:
        """Bilen er færdig med at forhandle: laderen går i gang — også hvis den blev
        pauset under forhandlingen (det observerede problem)."""
        self.negotiating = False
        if self.paused_while_negotiating:
            self.paused = False
            self.paused_while_negotiating = False
        self.publish()

    async def set_value(self, call) -> None:
        self.writes.append(float(call.data["value"]))
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_next > 0:
            self.fail_next -= 1
            raise HomeAssistantError("Setting maxChargeCurrent failed")
        self.current = float(call.data["value"])
        self.publish()

    async def press(self, call) -> None:
        eid = call.data["entity_id"]
        eid = eid[0] if isinstance(eid, list) else eid
        kind = "stop" if eid == STOP else "resume"
        self.cmds.append(kind)
        if self.cmd_gate is not None:
            await self.cmd_gate.wait()
        if self.cmd_fail_next > 0:
            self.cmd_fail_next -= 1
            raise HomeAssistantError(f"Running command '{kind}' failed")
        if kind == "stop":
            if not self.stop_valid:
                raise HomeAssistantError("stop not valid")
            self.paused = True
            if self.negotiating:
                self.paused_while_negotiating = True
        else:
            if not self.resume_valid:
                raise HomeAssistantError("resume not valid")
            self.paused = False
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
    zap.paused = True  # bilen er sat i og pauset — venter på første ladeslot
    zap.publish()
    hass.states.async_set(ENERGY, "0.0")
    hass.states.async_set(SOC, "58")
    hass.services.async_register("number", "set_value", zap.set_value)
    hass.services.async_register("button", "press", zap.press)

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
    c._prev_charger_mode = zap.mode
    c._set_plan(
        PlanResult(
            plan=[
                PlanBlock(ms(T_SLOT), ms(T_SLOT + timedelta(minutes=30)), 0.4, 5.5, 2.2, 30),
                PlanBlock(ms(S(SLOT2)), ms(S(SLOT2 + 15 * 60)), 0.4, 2.75, 1.1, 15),
            ]
        )
    )
    await c.async_save()

    async def tick(at: datetime, coordinator: EvcpCoordinator | None = None):
        clock.t = at
        d = await (coordinator or c)._async_update_data()
        if zap.gate is None and zap.cmd_gate is None:
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
    zap.car = "full"  # laderen lader ikke lige nu
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
    zap.car = "full"
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


# ---------- Charger v2: max-strøm fast på 16 A, pause/genoptag styrer ladningen ----------


async def test_max_current_set_to_16_once_and_never_touched(sim):
    c, zap, _, tick, *_ = sim
    zap.current = 0.0  # som efter v0.15 (0 A ved kabel ud)
    zap.publish()
    await tick(S(-600))
    assert zap.writes == [16.0]
    await tick(S(-540))  # laderen melder 16 A → bekræftet
    zap.current = 10.0  # nogen ændrer den
    zap.publish()
    for sec in (-300, 0, 1800, SLOT2):
        await tick(S(sec))
    assert zap.writes == [16.0], "max-strømmen røres ikke igen"


async def test_paused_until_slot_then_resume_and_pause(sim):
    c, zap, _, tick, *_ = sim
    d = await tick(S(-60))
    assert zap.cmds == [] and zap.mode == FIN and d.paused
    assert "venter på ladeslot" in d.current_note
    await tick(S(0))
    assert zap.cmds == ["resume"] and zap.mode == CHG
    d = await tick(S(60))
    assert d.action == "charging" and d.current_note == "Lader"
    await tick(S(1800))
    assert zap.cmds == ["resume", "stop"] and zap.paused
    await tick(S(SLOT2))
    assert zap.cmds == ["resume", "stop", "resume"] and zap.mode == CHG
    await tick(S(SLOT2 + 15 * 60))
    assert zap.cmds[-1] == "stop" and zap.paused
    assert zap.writes == [], "max-strømmen røres aldrig"


async def test_plugin_waits_for_negotiation_then_pauses(sim):
    """Pausen sendes ikke midt i forhandlingen — den ville blive tilsidesat."""
    c, zap, _, tick, *_ = sim
    zap.unplug()
    await tick(S(-900))
    assert c.runtime.active_vehicle == CHOOSE_VEHICLE
    zap.plug()  # requesting: bil og lader forhandler
    assert zap.mode == REQ
    d = await tick(S(-899.98))
    assert zap.cmds == [], "ingen pause midt i forhandlingen"
    assert "forhandler" in d.current_note
    zap.finish_negotiation()  # laderen går i gang
    assert zap.mode == CHG
    await tick(S(-898))
    assert zap.cmds == ["stop"] and zap.paused, "pause når forhandlingen er færdig"
    zap.publish()
    await tick(S(-880))
    assert zap.cmds == ["stop"] and zap.paused, "pausen holder"
    assert zap.writes == []


async def test_plugin_while_car_asleep_is_paused_after_settle(sim):
    """requesting uden forhandling (bilen sover) pauses, når den har stået stille 15 s."""
    c, zap, _, tick, *_ = sim
    zap.unplug()
    await tick(S(-900))
    zap.car = "asleep"
    zap.plug()
    assert zap.mode == REQ
    await tick(S(-895))
    assert zap.cmds == [], "venter 15 s"
    await tick(S(-879))
    assert zap.cmds == ["stop"] and zap.paused


async def test_no_vehicle_chosen_stays_paused_in_slot(sim):
    c, zap, _, tick, *_ = sim
    c.runtime.active_vehicle = CHOOSE_VEHICLE
    for sec in (0, 600, 1200):
        await tick(S(sec))
    assert zap.cmds == [] and zap.paused


async def test_car_stopped_itself_is_not_resumed(sim):
    c, zap, _, tick, *_ = sim
    await tick(S(0))
    zap.car = "full"  # bilen holder selv op (ikke pauset)
    zap.publish()
    for sec in (60, 120, 300):
        d = await tick(S(sec))
    assert zap.cmds == ["resume"], "intet nyt — resume er ugyldig dér"
    assert "selv holdt op" in d.current_note


async def test_unplug_does_nothing_to_charger(sim):
    c, zap, _, tick, *_ = sim
    await tick(S(0))
    await tick(S(60))
    zap.unplug()
    d = await tick(S(120))
    assert zap.cmds == ["resume"] and zap.writes == []
    assert "Ingen bil tilsluttet" in d.current_note


async def test_command_fails_retries_1_2_5_10_never_gives_up(sim):
    c, zap, _, tick, *_ = sim
    zap.cmd_fail_next = 3
    await tick(S(0))
    assert zap.cmds == ["resume"] and zap.paused
    await tick(S(30))
    assert len(zap.cmds) == 1, "venter 1 min"
    await tick(S(61))
    assert len(zap.cmds) == 2
    await tick(S(61 + 100))
    assert len(zap.cmds) == 2, "2 min"
    await tick(S(61 + 121))
    assert len(zap.cmds) == 3
    await tick(S(182 + 299))
    assert len(zap.cmds) == 3, "5 min"
    d = await tick(S(182 + 301))
    assert len(zap.cmds) == 4 and zap.mode == CHG
    assert d.action in ("start", "charging")


async def test_never_two_commands_at_once(sim):
    c, zap, _, tick, *_ = sim
    zap.cmd_gate = asyncio.Event()
    for sec in (0, 61, 300, 900):
        await tick(S(sec))
    assert zap.cmds == ["resume"]
    gate, zap.cmd_gate = zap.cmd_gate, None
    gate.set()
    await c.hass.async_block_till_done()
    d = await tick(S(960))
    assert zap.mode == CHG and d.action == "charging"


async def test_lad_straks_resumes_and_stop_pauses(sim):
    c, zap, clock, tick, *_ = sim
    clock.t = S(-900)
    c.runtime.force_charge = True
    c.on_user_restart()
    await tick(S(-900))
    assert zap.cmds == ["resume"] and zap.mode == CHG
    clock.t = S(-880)  # Stop 20 s efter — brugerhandling, ingen ventetid
    await c.async_stop_charging()
    await c.hass.async_block_till_done()
    assert zap.cmds == ["resume", "stop"] and zap.paused
    assert zap.writes == []


async def test_force_charge_without_vehicle(sim):
    c, zap, clock, tick, *_ = sim
    clock.t = S(-990)
    c.runtime.active_vehicle = CHOOSE_VEHICLE
    c.runtime.force_charge = True
    c.on_user_restart()
    await tick(S(-990))
    assert zap.mode == CHG


async def test_restart_mid_slot_sends_nothing(sim):
    c, zap, _, tick, make, _ = sim
    await tick(S(0))
    await tick(S(60))
    c.async_close()
    c2 = await make()
    c2.restore_plan()
    c2._prev_charger_mode = zap.mode
    await tick(S(120), c2)
    assert zap.cmds == ["resume"] and zap.writes == []
    c2.async_close()


async def test_car_asleep_after_resume_notifies_once(sim):
    c, zap, _, tick, *_ = sim
    sent = []
    c._notify = lambda title, msg, ntype: sent.append(msg)
    zap.car = "asleep"
    for sec in range(0, 600, 60):
        await tick(S(sec))
    assert zap.cmds == ["resume"] and zap.mode == REQ
    assert len(sent) == 1 and "Sover bilen" in sent[0]


async def test_resume_failing_notifies_and_keeps_trying(sim):
    c, zap, _, tick, *_ = sim
    sent = []
    c._notify = lambda title, msg, ntype: sent.append(msg)
    zap.cmd_fail_next = 99
    for sec in range(0, 1800, 30):
        await tick(S(sec))
    assert len(sent) == 1 and "pause" in sent[0]
    assert len(zap.cmds) >= 5, "giver aldrig op"


async def test_pause_failing_notifies(sim):
    c, zap, _, tick, *_ = sim
    sent = []
    c._notify = lambda title, msg, ntype: sent.append(msg)
    await tick(S(0))
    zap.cmd_fail_next = 99
    for sec in range(1800, 2400, 30):
        await tick(S(sec))
    assert zap.mode == CHG
    assert any("kunne ikke pauses" in m or "lader stadig" in m for m in sent)


async def test_observer_never_touches_the_charger(sim):
    c, zap, _, tick, *_ = sim
    c.runtime.observer_mode = True
    zap.current = 0.0
    zap.publish()
    for sec in (-600, 0, 60, 1800):
        d = await tick(S(sec))
    assert zap.writes == [] and zap.cmds == []
    assert "Observatør" in d.current_note


async def test_disable_resumes_paused_charger(sim, hass):
    c, zap, _, tick, *_ = sim
    entry = c.entry
    await tick(S(-60))
    assert zap.paused
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = c
    await integration.async_unload_entry(hass, entry)  # genindlæsning: rør intet
    await hass.async_block_till_done()
    assert zap.paused and zap.cmds == []

    c2 = EvcpCoordinator(hass, entry, c.store)
    hass.data[DOMAIN][entry.entry_id] = c2
    object.__setattr__(entry, "disabled_by", ConfigEntryDisabler.USER)
    await integration.async_unload_entry(hass, entry)
    await hass.async_block_till_done()
    assert not zap.paused and zap.mode == CHG, "slået fra → normal lader igen"
