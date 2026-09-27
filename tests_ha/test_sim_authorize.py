"""End-to-end simulation af coordinatoren mod en falsk Zaptec-lader (rigtig HA-kerne).

Laderen LÅSER hvis den får to authorize uden at autorisationen er brugt (ladning)
eller nulstillet (deauthorize / kabel ud) imellem — brugerens observerede adfærd.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ev_charge_planner import coordinator as coord_mod
from custom_components.ev_charge_planner.const import CHOOSE_VEHICLE, DOMAIN, MODE_STANDARD
from custom_components.ev_charge_planner.coordinator import EvcpCoordinator
from custom_components.ev_charge_planner.models import RuntimeStore
from custom_components.ev_charge_planner.planner import PlanBlock, PlanResult

MODE = "sensor.zag_charger_mode"
POWER = "sensor.zag_charge_power"
ENERGY = "sensor.zag_session_energy"
AUTH = "button.zag_authorize_charging"
DEAUTH = "button.zag_deauthorize_charging"
RESUME = "button.zag_resume_charging"
STOP = "button.zag_stop_charging"
SOC = "sensor.modely_battery_level"

REQ, CHG, FIN, DISC = (
    "connected_requesting",
    "connected_charging",
    "connected_finished",
    "disconnected",
)

T_SLOT = datetime(2026, 9, 26, 11, 15, 0, tzinfo=timezone.utc)  # 13:15 dansk


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def set(self, t: datetime) -> None:
        self.t = t


class Charger:
    def __init__(self, hass) -> None:
        self.hass = hass
        self.mode = REQ
        self.pending_auth = False  # autoriseret, men ikke brugt
        self.locked = False
        self.drop_next_auth = False  # 26-09: authorize uden effekt
        self.deauth_transient = FIN  # hvad laderen viser lige efter deauthorize
        self.presses: list[str] = []
        self.violations: list[str] = []
        self.gate: asyncio.Event | None = None  # hold et kald "i luften"

    def publish(self) -> None:
        self.hass.states.async_set(MODE, self.mode)
        self.hass.states.async_set(POWER, "11.0" if self.mode == CHG else "0.0")

    async def press(self, call) -> None:
        eid = call.data["entity_id"]
        eid = eid[0] if isinstance(eid, list) else eid
        if self.gate is not None:
            await self.gate.wait()
        self.presses.append(eid)
        if eid == AUTH:
            if self.mode != REQ:
                return
            if self.pending_auth:
                self.violations.append("dobbelt authorize → LÅST")
                self.locked = True
            self.pending_auth = True
            if self.locked or self.drop_next_auth:
                self.drop_next_auth = False
                return
            self.mode = CHG
            self.pending_auth = False  # brugt
        elif eid == DEAUTH:
            self.pending_auth = False
            self.locked = False
            self.mode = self.deauth_transient
        elif eid == STOP and self.mode == CHG:
            self.mode = FIN
        elif eid == RESUME and self.mode == FIN:
            self.mode = REQ
        self.publish()

    def auths(self) -> int:
        return self.presses.count(AUTH)


@pytest.fixture
def expected_lingering_timers() -> bool:
    return True


@pytest.fixture
async def sim(hass, monkeypatch):
    clock = Clock(T_SLOT - timedelta(minutes=1))
    monkeypatch.setattr(coord_mod.dt_util, "utcnow", clock)

    charger = Charger(hass)
    charger.publish()
    hass.states.async_set(ENERGY, "0.0")
    hass.states.async_set(SOC, "58")
    for b in (AUTH, DEAUTH, RESUME, STOP):
        hass.states.async_set(b, "unknown")
    hass.services.async_register("button", "press", charger.press)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "price_sensor": "sensor.price",
            "charger_mode_sensor": MODE,
            "charge_power_sensor": POWER,
            "session_energy_sensor": ENERGY,
            "authorize_button": AUTH,
            "resume_button": RESUME,
            "stop_button": STOP,
        },
        options={
            "vehicles": [
                {"name": "Model Y", "capacity_kwh": 79, "soc_sensor": SOC, "soc_live": True}
            ]
        },
    )
    entry.add_to_hass(hass)
    store = RuntimeStore(hass, entry.entry_id)
    await store.load()
    c = EvcpCoordinator(hass, entry, store)
    c.async_request_refresh = AsyncMock()
    rt = c.runtime
    rt.active_vehicle = "Model Y"
    rt.enabled = True
    rt.observer_mode = False
    rt.mode = MODE_STANDARD
    rt.target_soc = 80
    c._prev_charger_mode = REQ
    c._mode_since = T_SLOT - timedelta(hours=23)
    c._set_plan(
        PlanResult(
            plan=[
                PlanBlock(
                    start_ms=ms(T_SLOT),
                    end_ms=ms(T_SLOT + timedelta(hours=1)),
                    avg_price=0.41,
                    energy_kwh=11,
                    cost=4.5,
                    duration_min=60,
                )
            ]
        )
    )

    async def tick(at: datetime):
        clock.set(at)
        d = await c._async_update_data()
        if charger.gate is None:
            await hass.async_block_till_done()
        else:
            for _ in range(5):
                await asyncio.sleep(0)
        return d

    yield c, charger, clock, tick
    c.async_close()


def S(sec: float) -> datetime:
    return T_SLOT + timedelta(seconds=sec)


async def test_normal_start_waits_45s_then_single_authorize(sim):
    c, ch, _, tick = sim
    await tick(S(6))
    assert ch.auths() == 0, "ingen authorize præcis på kvarteret"
    d = await tick(S(46))
    assert ch.auths() == 1
    assert ch.mode == CHG
    d = await tick(S(60))
    assert d.action == "charging"
    assert not c.runtime.authorize_done  # autorisationen er brugt
    assert ch.violations == []


async def test_2609_lost_authorize_is_rescued_without_double(sim):
    c, ch, _, tick = sim
    ch.drop_next_auth = True
    await tick(S(46))
    assert ch.auths() == 1 and ch.mode == REQ
    await tick(S(80))
    assert DEAUTH not in ch.presses, "ikke før 45 s efter kaldet"
    d = await tick(S(95))
    assert ch.presses.count(DEAUTH) == 1
    assert "genstarter autorisation" in d.reason
    # laderen viser finished kortvarigt → må ikke tolkes som noget
    await tick(S(97))
    ch.mode = REQ
    ch.publish()
    await tick(S(100))
    assert c.runtime.active_vehicle == "Model Y" and c.runtime.enabled
    assert ch.auths() == 1, "venter på stabil requesting"
    await tick(S(106))
    assert ch.auths() == 1
    await tick(S(111))
    assert ch.auths() == 2
    assert ch.mode == CHG
    await tick(S(120))
    assert ch.violations == []
    assert c.runtime.active_vehicle == "Model Y"


async def test_deauth_transient_disconnected_is_ignored(sim):
    c, ch, _, tick = sim
    ch.drop_next_auth = True
    ch.deauth_transient = DISC
    await tick(S(46))
    await tick(S(95))
    assert ch.presses.count(DEAUTH) == 1
    await tick(S(97))  # ser "disconnected"
    assert c.runtime.active_vehicle == "Model Y", "eget deauthorize ≠ kabel ud"
    ch.mode = REQ
    ch.publish()
    await tick(S(100))  # disconnected → requesting: ikke ny session
    assert c.runtime.active_vehicle == "Model Y" and c.runtime.enabled
    await tick(S(112))
    assert ch.auths() == 2 and ch.mode == CHG
    assert ch.violations == []


async def test_real_unplug_during_grace_resets_afterwards(sim):
    c, ch, _, tick = sim
    ch.drop_next_auth = True
    ch.deauth_transient = DISC
    await tick(S(46))
    await tick(S(95))
    await tick(S(97))
    assert c.runtime.active_vehicle == "Model Y"
    # kablet forbliver ude
    await tick(S(95 + 125))
    assert c.runtime.active_vehicle == CHOOSE_VEHICLE
    assert not c.runtime.enabled
    assert c.runtime.last_authorize_iso == ""


async def test_user_toggle_does_not_send_second_authorize(sim):
    """26-09 kl. 13:21: stop + automatik til igen gav et andet authorize direkte."""
    c, ch, clock, tick = sim
    ch.drop_next_auth = True
    await tick(S(46))
    assert ch.auths() == 1
    clock.set(S(360))
    await c.async_stop_charging()  # laderen står i requesting; stop er uden effekt dér
    ch.mode = FIN  # som 26-09: laderen gik til finished
    ch.publish()
    await tick(S(362))
    c.runtime.enabled = True
    c.on_user_restart()
    await tick(S(380))  # finished → resume
    assert RESUME in ch.presses and ch.mode == REQ
    await tick(S(381))
    assert ch.auths() == 1, "intet direkte authorize nr. 2"
    await tick(S(382))
    assert ch.presses.count(DEAUTH) == 1, "i stedet redning"
    ch.mode = REQ
    ch.publish()
    await tick(S(384))
    await tick(S(396))
    assert ch.auths() == 2 and ch.mode == CHG
    assert ch.violations == []


async def test_no_deauthorize_while_authorize_call_in_flight(sim):
    c, ch, _, tick = sim
    ch.drop_next_auth = True
    ch.gate = asyncio.Event()  # Zaptec svarer ikke (integrationen gentager kaldet)
    await tick(S(46))
    await asyncio.sleep(0)
    assert c._press_inflight == "authorize"
    for sec in (100, 150, 200):
        await tick(S(sec))
        assert DEAUTH not in ch.presses
        assert ch.auths() == 0  # kaldet er stadig ikke færdigt
    gate = ch.gate
    ch.gate = None
    gate.set()
    await c.hass.async_block_till_done()
    await tick(S(201))
    await asyncio.sleep(0)
    await tick(S(205))
    assert ch.auths() == 1
    assert DEAUTH not in ch.presses, "45 s regnes fra kaldet var færdigt"
    await tick(S(250))
    assert ch.presses.count(DEAUTH) == 1
    assert ch.violations == []


async def test_two_rounds_then_give_up(sim):
    c, ch, _, tick = sim
    ch.locked = True  # laderen er låst og reagerer aldrig
    ch.deauth_transient = REQ
    orig = ch.press

    async def press_stay_locked(call):
        await orig(call)
        ch.locked = True  # deauthorize hjælper ikke i denne test
        ch.mode = REQ
        ch.publish()

    c.hass.services.async_register("button", "press", press_stay_locked)
    t = 46
    await tick(S(t))
    for _ in range(40):
        t += 5
        await tick(S(t))
    assert ch.presses.count(DEAUTH) == 2, "højst 2 runder"
    # hver authorize kom efter et deauthorize (ellers dobbelt)
    seq = [p for p in ch.presses if p in (AUTH, DEAUTH)]
    assert seq == [AUTH, DEAUTH, AUTH, DEAUTH, AUTH]
    await tick(S(t + 400))
    assert c.runtime.start_failed_notified


async def test_second_slot_after_charging_may_authorize_again(sim):
    c, ch, _, tick = sim
    c._set_plan(
        PlanResult(
            plan=[
                PlanBlock(ms(T_SLOT), ms(T_SLOT + timedelta(minutes=15)), 0.4, 3, 1, 15),
                PlanBlock(
                    ms(T_SLOT + timedelta(hours=2)),
                    ms(T_SLOT + timedelta(hours=2, minutes=15)),
                    0.4,
                    3,
                    1,
                    15,
                ),
            ]
        )
    )
    await tick(S(46))
    await tick(S(60))
    assert ch.mode == CHG
    await tick(S(16 * 60))  # uden for slot → stop
    assert ch.mode == FIN
    await tick(S(7200 + 50))  # slot 2 → resume
    assert ch.mode == REQ
    await tick(S(7200 + 55))
    assert ch.auths() == 2 and ch.mode == CHG
    assert ch.violations == []


async def test_force_charge_is_immediate(sim):
    c, ch, _, tick = sim
    c._set_plan(None)
    c.runtime.force_charge = True
    await tick(S(3))
    assert ch.auths() == 1 and ch.mode == CHG
