"""Tests for redningssekvensen (deauthorize → vent → ét authorize) og slot-forsinkelsen."""

from __future__ import annotations

from custom_components.ev_charge_planner.guards import (
    RESCUE_ABORT,
    RESCUE_AUTHORIZE,
    RESCUE_DEAUTHORIZE,
    RESCUE_NONE,
    RESCUE_WAIT,
    rescue_step,
    start_delay_remaining_ms,
)

S = 1000
T0 = 10_000_000


def _step(**kw) -> str:
    defaults = dict(
        is_requesting=True,
        power_flowing=False,
        authorize_done=True,
        press_inflight=False,
        gave_up=False,
        phase="",
        rounds_used=0,
        max_rounds=2,
        has_deauthorize=True,
        authorize_sent_ms=T0,
        deauth_done_ms=None,
        mode_since_ms=T0 - 3600 * S,
        now_ms=T0 + 46 * S,
        confirm_ms=45 * S,
        settle_ms=10 * S,
        deauth_max_wait_ms=90 * S,
    )
    defaults.update(kw)
    return rescue_step(**defaults)


# ---------- start en runde ----------


def test_2609_scenario_deauthorizes_after_confirm_timeout():
    # 26-09: ét authorize, laderen står stadig i requesting uden strøm → redning
    assert _step() == RESCUE_DEAUTHORIZE


def test_no_rescue_before_confirm_timeout():
    assert _step(now_ms=T0 + 20 * S) == RESCUE_NONE


def test_no_rescue_while_zaptec_call_still_running():
    # Zaptec kan gentage et authorize i op til ~100 s — aldrig deauthorize imens
    assert _step(press_inflight=True, now_ms=T0 + 300 * S) == RESCUE_WAIT


def test_no_rescue_before_call_finished():
    # authorize_sent_ms sættes først når kaldet er færdigt
    assert _step(authorize_sent_ms=None, now_ms=T0 + 300 * S) == RESCUE_NONE


def test_no_rescue_when_charging():
    assert _step(power_flowing=True) == RESCUE_NONE


def test_no_rescue_when_not_requesting():
    # fx finished: så skal resume køre først, ikke deauthorize
    assert _step(is_requesting=False) == RESCUE_NONE


def test_no_rescue_without_authorize():
    assert _step(authorize_done=False) == RESCUE_NONE


def test_rounds_are_limited():
    assert _step(rounds_used=1) == RESCUE_DEAUTHORIZE
    assert _step(rounds_used=2) == RESCUE_NONE


def test_no_rescue_after_giving_up_or_without_button():
    assert _step(gave_up=True) == RESCUE_NONE
    assert _step(has_deauthorize=False) == RESCUE_NONE


# ---------- efter deauthorize ----------


def _after(**kw) -> str:
    base = dict(
        phase="deauthorized",
        authorize_done=False,
        rounds_used=1,
        deauth_done_ms=T0,
        mode_since_ms=T0 + 2 * S,
    )
    base.update(kw)
    return _step(**base)


def test_waits_for_deauthorize_call_to_finish():
    assert _after(deauth_done_ms=None, now_ms=T0 + 60 * S) == RESCUE_WAIT


def test_waits_until_requesting_is_stable():
    # requesting siden +2 s; ved +11 s er den kun 9 s stabil → vent
    assert _after(now_ms=T0 + 11 * S) == RESCUE_WAIT
    assert _after(now_ms=T0 + 12 * S) == RESCUE_AUTHORIZE


def test_waits_while_not_requesting():
    assert _after(is_requesting=False, now_ms=T0 + 30 * S) == RESCUE_WAIT


def test_aborts_if_charger_never_returns_to_requesting():
    assert _after(is_requesting=False, now_ms=T0 + 90 * S) == RESCUE_ABORT


def test_authorize_after_deauth_ignores_gave_up_flag_ordering():
    # i deauthorized-fasen er det kun laderens tilstand der tæller
    assert _after(now_ms=T0 + 20 * S) == RESCUE_AUTHORIZE


def test_power_during_rescue_stops_it():
    assert _after(power_flowing=True, now_ms=T0 + 20 * S) == RESCUE_NONE


# ---------- forsinkelse ind i slottet ----------


def test_start_delay():
    blk = 1_000_000
    assert start_delay_remaining_ms(now_ms=blk + 6 * S, block_start_ms=blk, delay_ms=45 * S) == 39 * S
    assert start_delay_remaining_ms(now_ms=blk + 45 * S, block_start_ms=blk, delay_ms=45 * S) == 0
    # slået til midt i slottet → ingen ventetid
    assert start_delay_remaining_ms(now_ms=blk + 5 * 60 * S, block_start_ms=blk, delay_ms=45 * S) == 0
