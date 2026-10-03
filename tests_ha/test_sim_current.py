"""End-to-end simulation af strømstyringen mod en falsk Zaptec-installation (rigtig HA-kerne).

Laderen leverer strøm når installationens strøm er ≥ 6 A og bilen vil trække.
Kald til ``number.set_value`` kan hænge, fejle eller blive meldt tilbage forsinket.
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
from custom_components.ev_charge_planner.const import CHOOSE_VEHICLE, DOMAIN, MODE_STANDARD
from custom_components.ev_charge_planner.coordinator import EvcpCoordinator
from custom_components.ev_charge_planner.models import RuntimeStore
from custom_components.ev_charge_planner.planner import PlanBlock, PlanResult

MODE = "sensor.zag_charger_mode"
POWER = "sensor.zag_charge_power"
ENERGY = "sensor.zag_session_energy"
CUR = "number.zag089363_available_current"
SOC = "sensor.modely_battery_level"

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
    def __init__(self, hass) -> None:
        self.hass = hass
        self.plugged = True
        self.car_draws = True
        self.current = 16.0  # brugerens "normale" værdi før planneren
        self.writes: list[float] = []
        self.fail_next = 0
        self.gate: asyncio.Event | None = None
        self.lag = False
        self.pending: float | None = None
        self.concurrent = 0
        self.max_concurrent = 0

    @property
    def mode(self) -> str:
        if not self.plugged:
            return DISC
        if self.current >= 6 and self.car_draws:
            return CHG
        if self.current >= 6:
            return FIN
        return REQ

    def publish(self) -> None:
        self.hass.states.async_set(MODE, self.mode)
        self.hass.states.async_set(POWER, "11.0" if self.mode == CHG else "0.0")
        self.hass.states.async_set(CUR, str(self.current), {"min": 0, "max": 32})

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


@pytest.fixture
async def sim(hass, monkeypatch):
    clock = Clock(S(-600))
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", clock)

    zap = Zaptec(hass)
    zap.publish()
    hass.states.async_set(ENERGY, "0.0")
    hass.states.async_set(SOC, "58")
    hass.services.async_register("number", "set_value", zap.set_value)

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
    rt.mode = MODE_STANDARD
    rt.target_soc = 80
    c._prev_charger_mode = zap.mode
    c._set_plan(
        PlanResult(
            plan=[
                PlanBlock(ms(T_SLOT), ms(T_SLOT + timedelta(minutes=30)), 0.4, 5.5, 2.2, 30)
            ]
        )
    )
    await c.async_save()

    async def tick(at: datetime, coordinator: EvcpCoordinator | None = None):
        clock.t = at
        d = await (coordinator or c)._async_update_data()
        if zap.gate is None:
            await hass.async_block_till_done()
        else:
            for _ in range(5):
                await asyncio.sleep(0)
        return d

    yield c, zap, clock, tick, make, entry
    c.async_close()


async def test_outside_slot_sets_0a_and_no_charging(sim):
    c, zap, _, tick, *_ = sim
    d = await tick(S(-600))
    assert zap.writes == [0.0]
    assert zap.current == 0 and zap.mode == REQ
    d = await tick(S(-540))
    assert d.desired_current == 0 and d.actual_current == 0
    assert zap.writes == [0.0], "ingen gentagne kald når laderen har værdien"


async def test_slot_sets_16a_charges_and_stops_after(sim):
    c, zap, _, tick, *_ = sim
    await tick(S(-1000))  # 0 A sat 16:40 før slot → 15-min-reglen er opfyldt ved slot
    await tick(S(0))
    assert zap.writes == [0.0, 16.0]
    d = await tick(S(60))
    assert d.action == "charging" and zap.mode == CHG
    await tick(S(1800))  # slot slut (30 min efter start)
    assert zap.writes[-1] == 0.0 and zap.mode == REQ
    assert c.runtime.active_vehicle == "Model Y" and c.runtime.enabled


async def test_mode_changes_from_current_never_reset_the_vehicle(sim):
    c, zap, _, tick, *_ = sim
    await tick(S(-1000))
    for sec in (0, 60, 1800, 1860):
        await tick(S(sec))
    # laderen har været igennem requesting → charging → requesting
    assert c.runtime.active_vehicle == "Model Y"
    assert c.runtime.enabled


async def test_night_of_2_oct_zaptec_fails_then_recovers(sim):
    """Zaptec-kald fejler i lang tid — planneren bliver ved, til det lykkes."""
    c, zap, _, tick, *_ = sim
    await tick(S(-1000))
    zap.fail_next = 3
    await tick(S(0))
    assert zap.writes[-1] == 16.0 and zap.current == 0  # forsøg 1 fejlede
    assert "seneste fejl" in (await tick(S(30))).current_note
    assert len(zap.writes) == 2, "venter 60 s før næste forsøg"
    await tick(S(61))
    assert len(zap.writes) == 3  # forsøg 2 (fejler)
    await tick(S(61 + 100))
    assert len(zap.writes) == 3, "2 min pause"
    await tick(S(61 + 121))
    assert len(zap.writes) == 4  # forsøg 3 (fejler)
    await tick(S(182 + 299))
    assert len(zap.writes) == 4, "5 min pause"
    await tick(S(182 + 301))
    assert len(zap.writes) == 5 and zap.current == 16 and zap.mode == CHG
    d = await tick(S(182 + 360))
    assert d.action == "charging"
    assert zap.max_concurrent == 1


async def test_never_two_calls_at_once_while_zaptec_hangs(sim):
    c, zap, _, tick, *_ = sim
    await tick(S(-1000))
    zap.gate = asyncio.Event()
    for sec in (0, 60, 120, 300, 600):
        await tick(S(sec))
    assert zap.writes == [0.0, 16.0], "kun ét kald mens det hænger"
    gate, zap.gate = zap.gate, None
    gate.set()
    await c.hass.async_block_till_done()
    d = await tick(S(660))
    assert zap.current == 16 and d.action == "charging"
    assert zap.max_concurrent == 1


async def test_delayed_readback_does_not_cause_extra_writes(sim):
    c, zap, _, tick, *_ = sim
    await tick(S(-1000))
    zap.lag = True
    await tick(S(0))
    await tick(S(30))
    assert zap.writes == [0.0, 16.0]
    zap.apply_pending()  # Zaptec melder værdien efter et poll
    await tick(S(70))
    assert zap.writes == [0.0, 16.0]
    assert c.runtime.cur_confirmed


async def test_planned_change_waits_15_min_after_user_stop(sim):
    c, zap, clock, tick, *_ = sim
    clock.t = S(-1000)
    await tick(S(-1000))
    # brugeren tænder "Lad straks" og stopper igen lige før slottet
    clock.t = S(-300)
    c.runtime.force_charge = True
    c.on_user_restart()
    await tick(S(-300))
    assert zap.writes[-1] == 16.0
    clock.t = S(-240)
    await c.async_stop_charging()
    await c.hass.async_block_till_done()
    assert zap.writes[-1] == 0.0, "Stop virker med det samme"
    c.runtime.enabled = True
    await tick(S(0))  # slot starter 4 min efter sidste ændring
    assert zap.writes[-1] == 0.0, "planlagt ændring venter på 15-min-reglen"
    await tick(S(-240 + 900 + 1))
    assert zap.writes[-1] == 16.0 and zap.mode == CHG


async def test_external_change_is_corrected(sim):
    c, zap, _, tick, *_ = sim
    await tick(S(-1000))
    assert zap.current == 0
    zap.current = 16.0  # nogen ændrer den i Zaptec-appen
    zap.publish()
    d = await tick(S(-900))
    assert zap.writes[-1] == 0.0 and zap.current == 0
    assert "udefra" in c.runtime.cur_last_error or d.current_note


async def test_unplug_resets_and_new_plugin_stays_at_0(sim):
    c, zap, _, tick, *_ = sim
    await tick(S(-1000))
    zap.plugged = False
    zap.publish()
    await tick(S(-900))
    assert c.runtime.active_vehicle == CHOOSE_VEHICLE and not c.runtime.enabled
    zap.plugged = True
    zap.publish()
    d = await tick(S(-800))
    assert zap.current == 0 and d.charge_power == 0, "isætning lader ikke"
    assert zap.writes == [0.0]


async def test_force_charge_without_vehicle(sim):
    c, zap, _, tick, *_ = sim
    await tick(S(-1000))
    c.runtime.active_vehicle = CHOOSE_VEHICLE
    c.runtime.force_charge = True
    c.on_user_restart()
    await tick(S(-990))
    assert zap.current == 16 and zap.mode == CHG


async def test_observer_never_writes(sim):
    c, zap, _, tick, *_ = sim
    c.runtime.observer_mode = True
    for sec in (-1000, 0, 60, 1800):
        d = await tick(S(sec))
    assert zap.writes == []
    assert "Observatør" in d.current_note


async def test_restart_mid_slot_does_not_rewrite(sim):
    c, zap, _, tick, make, _ = sim
    await tick(S(-1000))
    await tick(S(0))
    await tick(S(60))
    n = len(zap.writes)
    c.async_close()
    c2 = await make()
    c2._prev_charger_mode = zap.mode
    await tick(S(120), c2)
    assert len(zap.writes) == n, "efter genstart har laderen allerede 16 A"
    c2.async_close()


async def test_car_not_drawing_notifies_once(sim):
    c, zap, _, tick, *_ = sim
    sent = []
    c._notify = lambda title, msg, ntype: sent.append(msg)
    zap.car_draws = False
    await tick(S(-1000))
    for sec in range(0, 600, 60):
        await tick(S(sec))
    assert zap.current == 16
    assert len(sent) == 1 and "trækker ikke strøm" in sent[0]


async def test_disable_restores_16a_but_reload_does_not(sim, hass):
    c, zap, _, tick, *_ = sim
    entry = c.entry
    await tick(S(-1000))
    assert zap.current == 0
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = c
    await integration.async_unload_entry(hass, entry)  # genindlæsning
    await hass.async_block_till_done()
    assert zap.current == 0

    c2 = EvcpCoordinator(hass, entry, c.store)
    hass.data[DOMAIN][entry.entry_id] = c2
    entry._disabled_by = ConfigEntryDisabler.USER  # noqa: SLF001
    object.__setattr__(entry, "disabled_by", ConfigEntryDisabler.USER)
    await integration.async_unload_entry(hass, entry)
    await hass.async_block_till_done()
    assert zap.current == 16, "slået fra → laderen virker normalt igen"


# ---------- opsætning: priser, strøm-entitet, standard-tilstand ----------


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


async def test_new_runtime_defaults_to_departure_mode():
    from custom_components.ev_charge_planner.const import MODE_DEPARTURE
    from custom_components.ev_charge_planner.models import Runtime

    assert Runtime().mode == MODE_DEPARTURE


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
