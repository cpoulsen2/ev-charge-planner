"""Tests for den rene planner-logik.

Verificerer at Python-porteringen matcher adfærden fra Node-RED-noden
"🧮 Beregn Ladeplan".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.ev_charge_planner.planner import (
    SLOT_MS,
    WARN_ALREADY_AT_TARGET,
    WARN_NO_PRICES,
    WARN_NONE,
    WARN_NOT_ENOUGH_TIME,
    compute_plan,
    expand_mixed,
    to_ms,
)

UTC = timezone.utc
BASE = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def hours(h: float) -> datetime:
    return BASE + timedelta(hours=h)


def hourly_prices(prices: list[float]) -> list[dict]:
    """Byg timepriser der starter kl. 00:00 og fremefter."""
    return [{"start": BASE + timedelta(hours=i), "price": p} for i, p in enumerate(prices)]


# ---------- expand_mixed ----------


def test_expand_hourly_gives_four_quarters_each():
    slots = expand_mixed(hourly_prices([1, 1, 1, 1]))
    assert len(slots) == 16  # 4 timer × 4 kvarter


def test_expand_15min_granularity():
    entries = [{"start": BASE + timedelta(minutes=15 * i), "price": i} for i in range(4)]
    slots = expand_mixed(entries)
    # 3 entries med 15-min afstand → 1 kvarter hver; sidste uden 'næste' → antag 60 min → 4
    assert len(slots) == 1 + 1 + 1 + 4


def test_expand_reads_value_when_price_missing():
    slots = expand_mixed([{"start": BASE, "value": 2.5}])
    assert slots[0].price == 2.5


# ---------- compute_plan ----------


def _plan(**kw):
    defaults = dict(
        now_ms=to_ms(BASE),
        deadline_ms=to_ms(hours(4)),
        target_pct=100.0,
        current_soc=0.0,
        capacity_kwh=1.0,
        power_kw=4.0,
        raw_today=hourly_prices([2.0, 0.5, 0.5, 3.0]),
        raw_tomorrow=[],
        min_block_mins=0,
    )
    defaults.update(kw)
    return compute_plan(**defaults)


def test_already_at_target():
    res = _plan(target_pct=50.0, current_soc=50.0)
    assert res.warning == WARN_ALREADY_AT_TARGET
    assert res.plan == []


def test_no_prices_when_empty_data():
    res = _plan(raw_today=[], raw_tomorrow=[])
    assert res.warning == WARN_NO_PRICES
    assert res.plan == []


def test_picks_single_cheapest_slot():
    # energy_needed = 1 kWh, power 4 kW → 1 kvarter nødvendig
    res = _plan()
    assert res.warning == WARN_NONE
    assert len(res.plan) == 1
    block = res.plan[0]
    # Billigste (0.5) starter i time 1
    assert block.start_ms == to_ms(hours(1))
    assert block.duration_min == 15
    assert block.avg_price == 0.5
    assert abs(block.energy_kwh - 1.0) < 1e-9
    assert abs(block.cost - 0.5) < 1e-9


def test_not_enough_time_selects_all_available():
    # Vindue på kun 30 min, men brug for meget → not_enough_time
    res = _plan(
        deadline_ms=to_ms(BASE + timedelta(minutes=30)),
        capacity_kwh=100.0,
        power_kw=11.0,
        raw_today=hourly_prices([1.0, 1.0]),
    )
    assert res.warning == WARN_NOT_ENOUGH_TIME
    assert len(res.plan) == 1
    assert res.plan[0].duration_min == 30  # de 2 tilgængelige slots


def test_contiguous_slots_merge_into_one_block():
    # To billige nabo-timer skal blive én sammenhængende blok
    res = _plan(
        capacity_kwh=10.0,
        power_kw=11.0,
        target_pct=100.0,
        current_soc=80.0,  # energy 2 kWh → ~1 kvarter... juster
        raw_today=hourly_prices([5.0, 0.5, 0.5, 5.0]),
    )
    # Alle valgte slots i time 1-2 er sammenhængende → 1 blok
    assert len(res.plan) == 1
    assert res.plan[0].start_ms == to_ms(hours(1))


def test_min_block_avoids_tiny_blocks():
    # Med min_block_mins=30 må ingen blok være kortere end 2 kvarter
    res = _plan(
        capacity_kwh=10.0,
        power_kw=11.0,
        current_soc=0.0,
        target_pct=40.0,  # energy 4 kWh
        raw_today=hourly_prices([1.0, 0.4, 2.0, 0.4, 1.0, 0.4]),
        deadline_ms=to_ms(hours(6)),
        min_block_mins=30,
    )
    for block in res.plan:
        assert block.duration_min >= 30
