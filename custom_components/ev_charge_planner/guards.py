"""Rene beslutningsfunktioner for strømstyring og giv-op-timer.

Dette modul har BEVIDST ingen Home Assistant-afhængigheder, så det kan
unit-testes isoleret (som ``planner.py``). Al tilstand gives ind som argumenter;
funktionerne har ingen sideeffekter. Coordinatoren holder selve tilstanden
(persisteret i ``Runtime``) og kalder disse funktioner.

Laderen styres udelukkende via en strømgrænse (fx installationens available
current): ladestrøm i et slot, ellers 0 A. At sætte en værdi er idempotent — det
samme kald to gange giver samme resultat — så kald kan trygt gentages, til laderen
melder den ønskede værdi tilbage.
"""

from __future__ import annotations


def charger_max_current_entity(charger_mode_sensor: str | None) -> str | None:
    """Laderens egen max-strøm-entitet, udledt af charger mode-sensoren.

    ``sensor.zag089363_charger_mode`` → ``number.zag089363_charger_max_current``
    (Zaptec-integrationens navngivning). None hvis navnet ikke følger mønstret.
    """
    prefix, suffix = "sensor.", "_charger_mode"
    if not charger_mode_sensor:
        return None
    if not (charger_mode_sensor.startswith(prefix) and charger_mode_sensor.endswith(suffix)):
        return None
    charger = charger_mode_sensor[len(prefix) : -len(suffix)]
    return f"number.{charger}_charger_max_current" if charger else None


CURRENT_IN_SYNC = "in_sync"
CURRENT_WRITE = "write"
CURRENT_WAIT = "wait"


def current_action(
    *,
    desired: float,
    actual: float | None,
    write_inflight: bool,
    attempts: int,
    last_write_ms: int | None,
    last_change_ms: int | None,
    urgent: bool,
    now_ms: int,
    confirm_ms: int,
    retry_ms: tuple[int, ...],
    min_change_interval_ms: int,
) -> tuple[str, int]:
    """Skal den ønskede strøm sendes til laderen nu?

    Returnerer ``(handling, ventetid_ms)``:
    - ``CURRENT_IN_SYNC``: laderen melder allerede den ønskede værdi.
    - ``CURRENT_WRITE``: send værdien nu.
    - ``CURRENT_WAIT``: vent (``ventetid_ms`` til næste mulige forsøg; 0 = ukendt,
      fx fordi et kald stadig kører).

    - ``attempts``/``last_write_ms`` gælder den NUVÆRENDE ønskede værdi (kalderen
      nulstiller dem når den ønskede værdi skifter). Efter første kald ventes
      ``confirm_ms`` på at Zaptec melder værdien tilbage; derefter stigende pauser
      fra ``retry_ms`` (sidste værdi gentages).
    - ``last_change_ms``: hvornår strømmen sidst blev ændret til en ny værdi. Zaptec
      anbefaler højst én ændring pr. ``min_change_interval_ms``; det overholdes for
      planlagte ændringer, men ikke for brugerhandlinger (``urgent``).
    """
    if actual is not None and abs(actual - desired) < 0.5:
        return (CURRENT_IN_SYNC, 0)
    if write_inflight:
        return (CURRENT_WAIT, 0)
    earliest = now_ms
    if attempts > 0 and last_write_ms is not None:
        if attempts == 1:
            delay = confirm_ms
        else:
            delay = retry_ms[min(attempts - 2, len(retry_ms) - 1)]
        earliest = max(earliest, last_write_ms + delay)
    if attempts == 0 and not urgent and last_change_ms is not None:
        earliest = max(earliest, last_change_ms + min_change_interval_ms)
    if earliest <= now_ms:
        return (CURRENT_WRITE, 0)
    return (CURRENT_WAIT, earliest - now_ms)


def start_failure_state(
    *,
    should_be_charging: bool,
    power_flowing: bool,
    wait_since_ms: int | None,
    now_ms: int,
    timeout_ms: int,
    already_notified: bool,
) -> tuple[int | None, bool]:
    """Udfaldsdrevet advarsels-timer.

    Returnerer ``(new_wait_since_ms, should_notify)``:
    - ``new_wait_since_ms is None`` → nulstil timeren (vi lader, eller er ikke i et
      aktivt slot). Kalderen bør også rydde ``already_notified``.
    - ellers venter vi på strøm; ``should_notify`` er True præcis den ene gang hvor
      ventetiden netop har passeret ``timeout_ms`` og der ikke allerede er notificeret.
    """
    if power_flowing or not should_be_charging:
        return (None, False)
    if wait_since_ms is None:
        wait_since_ms = now_ms
    should_notify = (not already_notified) and (now_ms - wait_since_ms) >= timeout_ms
    return (wait_since_ms, should_notify)
