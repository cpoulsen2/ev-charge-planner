"""Tests for den rene authorize-guard og giv-op-timer.

Dækker accept-kriterierne fra rettelsesplanen (v0.8.1) der kan testes uden HA.
"""

from __future__ import annotations

from custom_components.ev_charge_planner.guards import (
    may_authorize,
    start_failure_state,
)

MIN = 60_000  # ét minut i ms
AUTH_INTERVAL = 3 * MIN
FAIL_TIMEOUT = 5 * MIN


def _auth(**kw) -> bool:
    defaults = dict(
        is_requesting=True,
        authorize_done=False,
        gave_up=False,
        last_authorize_ms=None,
        now_ms=1_000_000,
        min_interval_ms=AUTH_INTERVAL,
    )
    defaults.update(kw)
    return may_authorize(**defaults)


# ---------- may_authorize ----------


def test_authorize_allowed_first_time_in_requesting():
    assert _auth() is True


def test_no_authorize_when_not_requesting():
    # Kun connected_requesting må autorisere (aldrig i charging/finished/disconnected)
    assert _auth(is_requesting=False) is False


def test_no_second_authorize_in_same_episode():
    # Accept-kriterie 1: højst ét authorize pr. requesting-episode
    assert _auth(authorize_done=True) is False


def test_gave_up_blocks_authorize():
    # gave_up undertrykker authorize (men skal ikke røre resume — det håndteres i coordinator)
    assert _auth(gave_up=True) is False


def test_backstop_blocks_within_interval():
    # Accept-kriterie 8: backstoppen blokerer selv når authorize_done er False
    # (fx efter en ikke-disconnect re-arm)
    now = 10_000_000
    assert _auth(authorize_done=False, last_authorize_ms=now - (2 * MIN), now_ms=now) is False


def test_backstop_allows_after_interval():
    now = 10_000_000
    assert _auth(authorize_done=False, last_authorize_ms=now - (3 * MIN), now_ms=now) is True


def test_backstop_and_episode_are_independent():
    # Selv efter intervallet er gået, blokerer episode-flaget stadig
    now = 10_000_000
    assert _auth(authorize_done=True, last_authorize_ms=now - (10 * MIN), now_ms=now) is False


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
