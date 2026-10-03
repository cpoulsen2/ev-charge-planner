"""Tests for den rene strømstyring og advarsels-timer (kan testes uden HA)."""

from __future__ import annotations

from custom_components.ev_charge_planner.guards import (
    CURRENT_IN_SYNC,
    CURRENT_WAIT,
    CURRENT_WRITE,
    charger_entity,
    charger_max_current_entity,
    current_action,
    current_applied,
    start_failure_state,
)

S = 1000
MIN = 60_000  # ét minut i ms
FAIL_TIMEOUT = 5 * MIN
T = 100_000_000


def _cur(**kw):
    defaults = dict(
        desired=16.0,
        actual=0.0,
        write_inflight=False,
        attempts=0,
        last_write_ms=None,
        last_change_ms=None,
        urgent=False,
        now_ms=T,
        confirm_ms=60 * S,
        retry_ms=(2 * MIN, 5 * MIN, 10 * MIN),
        min_change_interval_ms=15 * MIN,
    )
    defaults.update(kw)
    return current_action(**defaults)


# ---------- current_action ----------


def test_in_sync_does_nothing():
    assert _cur(actual=16.0) == (CURRENT_IN_SYNC, 0)
    assert _cur(desired=0.0, actual=0.0) == (CURRENT_IN_SYNC, 0)
    assert _cur(actual=16.2) == (CURRENT_IN_SYNC, 0)


def test_first_write_is_immediate():
    assert _cur() == (CURRENT_WRITE, 0)


def test_unknown_actual_still_writes():
    # Laderen melder ingen værdi (fx unavailable) — at sætte er ufarligt
    assert _cur(actual=None) == (CURRENT_WRITE, 0)


def test_never_while_call_in_flight():
    assert _cur(write_inflight=True, attempts=3, last_write_ms=T - 3600 * S)[0] == CURRENT_WAIT


def test_waits_for_confirmation_after_first_write():
    assert _cur(attempts=1, last_write_ms=T - 30 * S) == (CURRENT_WAIT, 30 * S)
    assert _cur(attempts=1, last_write_ms=T - 60 * S) == (CURRENT_WRITE, 0)


def test_retry_backoff_grows_and_caps():
    assert _cur(attempts=2, last_write_ms=T - 1 * MIN) == (CURRENT_WAIT, 1 * MIN)
    assert _cur(attempts=2, last_write_ms=T - 2 * MIN)[0] == CURRENT_WRITE
    assert _cur(attempts=3, last_write_ms=T - 4 * MIN) == (CURRENT_WAIT, 1 * MIN)
    assert _cur(attempts=4, last_write_ms=T - 9 * MIN) == (CURRENT_WAIT, 1 * MIN)
    assert _cur(attempts=9, last_write_ms=T - 10 * MIN)[0] == CURRENT_WRITE


def test_planned_change_respects_15_minutes():
    # Strømmen blev ændret for 5 min siden → planlagt ændring venter 10 min
    assert _cur(last_change_ms=T - 5 * MIN) == (CURRENT_WAIT, 10 * MIN)
    assert _cur(last_change_ms=T - 15 * MIN) == (CURRENT_WRITE, 0)


def test_user_action_bypasses_15_minutes():
    assert _cur(last_change_ms=T - 1 * MIN, urgent=True) == (CURRENT_WRITE, 0)


def test_retries_are_not_blocked_by_15_minutes():
    # 15-minutters-reglen gælder skift til en NY værdi, ikke genforsøg af samme
    assert _cur(attempts=1, last_write_ms=T - 61 * S, last_change_ms=T - 61 * S)[0] == (
        CURRENT_WRITE
    )


# ---------- start_failure_state ----------


def _fail(**kw):
    defaults = dict(
        should_be_charging=True,
        power_flowing=False,
        wait_since_ms=None,
        now_ms=1_000_000,
        timeout_ms=FAIL_TIMEOUT,
        already_notified=False,
    )
    defaults.update(kw)
    return start_failure_state(**defaults)


def test_timer_resets_when_charging():
    # Strøm flyder → nulstil timer, ingen notifikation
    assert _fail(power_flowing=True) == (None, False)


def test_timer_resets_when_not_in_slot():
    assert _fail(should_be_charging=False) == (None, False)


def test_timer_starts_on_first_wait_tick():
    wait, notify = _fail(wait_since_ms=None, now_ms=5_000_000)
    assert wait == 5_000_000
    assert notify is False


def test_no_notify_before_timeout():
    now = 5_000_000
    wait, notify = _fail(wait_since_ms=now - (4 * MIN), now_ms=now)
    assert wait == now - (4 * MIN)
    assert notify is False


def test_notify_exactly_once_after_timeout():
    now = 5_000_000
    started = now - (5 * MIN)
    wait, notify = _fail(wait_since_ms=started, now_ms=now, already_notified=False)
    assert notify is True
    # Anden gang: allerede notificeret → ingen gentagelse
    _, notify2 = _fail(wait_since_ms=started, now_ms=now, already_notified=True)
    assert notify2 is False


def test_timer_gate_is_outcome_not_mode():
    # Selv hvis "mode" ikke er requesting: should_be_charging + ingen strøm i slot
    # skal stadig kunne udløse (funktionen kender slet ikke mode)
    now = 9_000_000
    _, notify = _fail(wait_since_ms=now - (6 * MIN), now_ms=now)
    assert notify is True


# ---------- laderens max-strøm-entitet ----------


def test_charger_max_current_entity_is_derived():
    assert (
        charger_max_current_entity("sensor.zag089363_charger_mode")
        == "number.zag089363_charger_max_current"
    )
    assert charger_max_current_entity("sensor.something_else") is None
    assert charger_max_current_entity(None) is None
    assert charger_max_current_entity("sensor._charger_mode") is None



def test_charger_entity_variants():
    mode = "sensor.zag089363_charger_mode"
    assert (
        charger_entity(mode, "sensor", "allocated_charge_current")
        == "sensor.zag089363_allocated_charge_current"
    )
    assert charger_entity(mode, "binary_sensor", "online") == "binary_sensor.zag089363_online"


# ---------- current_applied (Zaptec: MaxCurrent OG ChargeCurrentSet) ----------


def _ap(**kw):
    d = dict(desired=16.0, setting=16.0, charge_current_set=16.0, power_flowing=True)
    d.update(kw)
    return current_applied(**d)


def test_applied_when_setting_and_charger_agree():
    assert _ap() == (True, "")
    assert _ap(desired=0.0, setting=0.0, charge_current_set=0.0, power_flowing=False) == (
        True,
        "",
    )


def test_not_applied_when_setting_differs():
    assert _ap(setting=0.0)[0] is False
    assert _ap(setting=None)[0] is False


def test_not_applied_when_charger_uses_other_current():
    ok, why = _ap(charge_current_set=0.0)
    assert not ok and "0 A" in why


def test_not_applied_when_charging_but_should_be_off():
    ok, why = _ap(desired=0.0, setting=0.0, charge_current_set=None, power_flowing=True)
    assert not ok and "lader stadig" in why


def test_unknown_charge_current_set_is_ignored():
    assert _ap(charge_current_set=None) == (True, "")
