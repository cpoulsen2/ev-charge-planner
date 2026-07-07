"""Ren pris-optimering for EV Charge Planner.

Dette modul er en tro portering af Node-RED-noden "🧮 Beregn Ladeplan"
(sliding-window prisoptimering). Det har BEVIDST ingen Home Assistant-afhængigheder,
så det kan unit-testes isoleret og verificeres mod de gamle Node-RED-planer.

Al tid håndteres i epoch-millisekunder (som JavaScript ``Date.getTime()``),
så adfærden matcher den oprindelige kode 1:1.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

SLOT_MS = 15 * 60 * 1000  # 900000 — én kvart-slot i millisekunder

# Mulige advarsler i resultatet
WARN_NONE = "none"
WARN_ALREADY_AT_TARGET = "already_at_target"
WARN_NO_PRICES = "no_prices"
WARN_NOT_ENOUGH_TIME = "not_enough_time"


@dataclass(frozen=True)
class PriceSlot:
    """En enkelt 15-minutters prisslot."""

    time_ms: int
    price: float


@dataclass(frozen=True)
class PlanBlock:
    """En sammenhængende ladeblok i planen."""

    start_ms: int
    end_ms: int
    avg_price: float
    energy_kwh: float
    cost: float
    duration_min: int

    @property
    def start_dt(self) -> datetime:
        return datetime.fromtimestamp(self.start_ms / 1000, tz=timezone.utc)

    @property
    def end_dt(self) -> datetime:
        return datetime.fromtimestamp(self.end_ms / 1000, tz=timezone.utc)


@dataclass
class PlanResult:
    """Resultatet af en planberegning."""

    plan: list[PlanBlock] = field(default_factory=list)
    warning: str = WARN_NONE
    estimated_cost: float = 0.0
    energy_needed: float = 0.0
    slots_needed: int = 0
    deadline_ms: int = 0
    current_soc: float = 0.0
    target_pct: float = 0.0


def to_ms(value: datetime | int | float | str) -> int:
    """Konvertér datetime/ISO-streng/epoch til epoch-millisekunder."""
    if isinstance(value, (int, float)):
        # Antag allerede millisekunder hvis stort nok, ellers sekunder
        return int(value if value > 1e11 else value * 1000)
    if isinstance(value, str):
        value = _parse_iso(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1000)
    raise TypeError(f"Kan ikke konvertere {value!r} til millisekunder")


def _parse_iso(text: str) -> datetime:
    """Parse en ISO-8601-streng (håndterer efterstillet 'Z')."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def expand_mixed(entries: list[dict]) -> list[PriceSlot]:
    """Udvid pris-entries til 15-min slots (håndterer blandet granularitet).

    Tro portering af ``expandMixed`` fra Node-RED. Hver entry har ``start``
    (ISO/datetime/epoch) og enten ``price`` eller ``value``. Slot-varigheden
    udledes af afstanden til næste entry: <=20 min → 15 min, ellers afstanden;
    ingen næste entry → antag 60 min.
    """
    result: list[PriceSlot] = []
    n = len(entries)
    for i in range(n):
        entry = entries[i]
        t_ms = to_ms(entry["start"])
        price = entry.get("price")
        if price is None:
            price = entry.get("value", 0)
        price = float(price)

        slot_min = 60
        if i + 1 < n:
            gap = (to_ms(entries[i + 1]["start"]) - t_ms) / 60000
            if 0 < gap <= 60:
                slot_min = 15 if gap <= 20 else gap

        num_quarters = round(slot_min / 15)
        for q in range(num_quarters):
            result.append(PriceSlot(time_ms=t_ms + q * SLOT_MS, price=price))
    return result


def _build_blocks(
    cheap_set: set[int], sorted_slots: list[PriceSlot]
) -> list[list[PriceSlot]]:
    """Byg sammenhængende blokke af valgte slots (tro portering af buildBlocks)."""
    result: list[list[PriceSlot]] = []
    blk: list[PriceSlot] | None = None
    blk_end: int = 0
    for s in sorted_slots:
        ms = s.time_ms
        if ms in cheap_set:
            if blk is None:
                blk = [s]
                blk_end = ms + SLOT_MS
            elif ms == blk_end:
                blk.append(s)
                blk_end = ms + SLOT_MS
            else:
                result.append(blk)
                blk = [s]
                blk_end = ms + SLOT_MS
        elif blk is not None:
            result.append(blk)
            blk = None
    if blk is not None:
        result.append(blk)
    return result


def compute_deadline_ms(now_ms: int, mode: str, departure_ms: int | None) -> int | None:
    """Beregn deadline i epoch-ms.

    Standard: i dag kl. 06:00 lokal — men da vi arbejder i UTC-ms her, forventer
    kalderen at levere ``departure_ms`` for Afgang-mode og selv beregne Standard-
    deadline med korrekt tidszone. For Standard uden departure returneres None,
    og kalderen skal levere en deadline. (Coordinatoren håndterer tidszonen.)
    """
    if mode == "Standard":
        return None  # Coordinatoren beregner 06:00 i lokal tid
    return departure_ms


def compute_plan(
    *,
    now_ms: int,
    deadline_ms: int,
    target_pct: float,
    current_soc: float,
    capacity_kwh: float,
    power_kw: float,
    raw_today: list[dict],
    raw_tomorrow: list[dict],
    min_block_mins: int = 0,
) -> PlanResult:
    """Beregn en ladeplan med sliding-window prisoptimering.

    Tro portering af Node-RED-noden "🧮 Beregn Ladeplan".
    """
    result = PlanResult(
        deadline_ms=deadline_ms,
        current_soc=current_soc,
        target_pct=target_pct,
    )

    power = power_kw or 11

    # Udvid hver liste separat (som i Node-RED), kombinér, og filtrér til vinduet
    prices = expand_mixed(raw_today) + expand_mixed(raw_tomorrow)
    if not prices:
        # Ingen prisdata overhovedet
        result.warning = WARN_NO_PRICES
        return result

    prices = [
        s for s in prices if s.time_ms + SLOT_MS > now_ms and s.time_ms < deadline_ms
    ]

    energy_needed = capacity_kwh * max(0, target_pct - current_soc) / 100
    result.energy_needed = energy_needed
    if energy_needed <= 0:
        result.warning = WARN_ALREADY_AT_TARGET
        return result

    slots_needed = math.ceil((energy_needed / power) * 4)
    result.slots_needed = slots_needed
    to_select = slots_needed

    if not prices:
        result.warning = WARN_NO_PRICES
        return result

    if to_select > len(prices):
        result.warning = WARN_NOT_ENOUGH_TIME
        to_select = len(prices)

    # Vælg de billigste 'to_select' slots
    cheap_times: set[int] = {
        s.time_ms for s in sorted(prices, key=lambda s: s.price)[:to_select]
    }

    sorted_slots = sorted(prices, key=lambda s: s.time_ms)
    blocks = _build_blocks(cheap_times, sorted_slots)

    # Minimum-blokstørrelse: undgå meget korte ladesessioner (tro portering)
    min_block_slots = max(1, round(min_block_mins / 15)) if min_block_mins > 0 else 1

    if min_block_slots > 1:
        for _ in range(5):
            short_idx = next(
                (i for i, b in enumerate(blocks) if len(b) < min_block_slots), -1
            )
            if short_idx < 0:
                break
            short_block = blocks[short_idx]

            if len(cheap_times) - len(short_block) < slots_needed:
                for s in short_block:
                    cheap_times.discard(s.time_ms)
                adj_candidates = sorted(
                    (
                        s
                        for s in sorted_slots
                        if s.time_ms not in cheap_times
                        and (
                            (s.time_ms - SLOT_MS) in cheap_times
                            or (s.time_ms + SLOT_MS) in cheap_times
                        )
                    ),
                    key=lambda s: s.price,
                )
                if len(adj_candidates) >= len(short_block):
                    to_add = len(short_block)
                    for s in adj_candidates:
                        if to_add <= 0:
                            break
                        cheap_times.add(s.time_ms)
                        to_add -= 1
                    blocks = _build_blocks(cheap_times, sorted_slots)
                else:
                    for s in short_block:
                        cheap_times.add(s.time_ms)
                    break
                continue

            for s in short_block:
                cheap_times.discard(s.time_ms)
            not_selected = sorted(
                (s for s in sorted_slots if s.time_ms not in cheap_times),
                key=lambda s: s.price,
            )
            to_add = len(short_block)
            # Første runde: nabo til allerede valgte (udvider eksisterende blokke)
            for s in not_selected:
                if to_add <= 0:
                    break
                ms = s.time_ms
                if (ms - SLOT_MS) in cheap_times or (ms + SLOT_MS) in cheap_times:
                    cheap_times.add(ms)
                    to_add -= 1
            # Anden runde: billigste resterende
            for s in not_selected:
                if to_add <= 0:
                    break
                if s.time_ms not in cheap_times:
                    cheap_times.add(s.time_ms)
                    to_add -= 1
            blocks = _build_blocks(cheap_times, sorted_slots)

    plan: list[PlanBlock] = []
    for b in blocks:
        avg_price = sum(s.price for s in b) / len(b)
        kwh = power * (len(b) / 4)
        start_ms = b[0].time_ms
        end_ms = b[-1].time_ms + SLOT_MS
        plan.append(
            PlanBlock(
                start_ms=start_ms,
                end_ms=end_ms,
                avg_price=avg_price,
                energy_kwh=kwh,
                cost=kwh * avg_price,
                duration_min=len(b) * 15,
            )
        )

    result.plan = plan
    result.estimated_cost = sum(b.cost for b in plan)
    return result
