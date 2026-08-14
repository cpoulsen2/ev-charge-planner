"""Rene beslutningsfunktioner for authorize-styring og giv-op-timer.

Dette modul har BEVIDST ingen Home Assistant-afhængigheder, så det kan
unit-testes isoleret (som ``planner.py``). Al tilstand gives ind som argumenter;
funktionerne har ingen sideeffekter. Coordinatoren holder selve tilstanden
(persisteret i ``Runtime``) og kalder disse funktioner.

Baggrund: en Zaptec Go låser hvis den modtager to authorize-kommandoer i træk
uden at kablet tages ud imellem. Derfor må authorize sendes højst én gang pr.
requesting-episode, med en hård minimums-tid mellem to tryk som backstop.
"""

from __future__ import annotations


def may_authorize(
    *,
    is_requesting: bool,
    authorize_done: bool,
    gave_up: bool,
    last_authorize_ms: int | None,
    now_ms: int,
    min_interval_ms: int,
) -> bool:
    """Afgør om der må sendes ét authorize-tryk nu.

    - ``is_requesting``: laderen står i ``connected_requesting`` (venter på autorisation).
    - ``authorize_done``: vi har allerede autoriseret i denne session (én-gang-pr-episode).
    - ``gave_up``: vi har opgivet at starte (undertrykker KUN authorize, ikke resume).
    - ``last_authorize_ms``/``min_interval_ms``: hård backstop — aldrig to authorize
      tættere end intervallet, uanset flag/kodesti/reload.
    """
    if not is_requesting:
        return False
    if authorize_done or gave_up:
        return False
    if last_authorize_ms is not None and (now_ms - last_authorize_ms) < min_interval_ms:
        return False
    return True


def start_failure_state(
    *,
    should_be_charging: bool,
    power_flowing: bool,
    wait_since_ms: int | None,
    now_ms: int,
    timeout_ms: int,
    already_notified: bool,
) -> tuple[int | None, bool]:
    """Udfaldsdrevet giv-op-timer.

    Returnerer ``(new_wait_since_ms, should_notify)``:
    - ``new_wait_since_ms is None`` → nulstil timeren (vi lader, eller er ikke i et
      aktivt slot). Kalderen bør også rydde ``already_notified``.
    - ellers venter vi på strøm; ``should_notify`` er True præcis den ene gang hvor
      ventetiden netop har passeret ``timeout_ms`` og der ikke allerede er notificeret.

    Gates på UDFALDET (skal lade + ingen strøm), ikke på charger-mode — en lader der
    står i ``connected_finished`` med 0 kW i et aktivt slot skal også udløse det.
    """
    if power_flowing or not should_be_charging:
        return (None, False)
    if wait_since_ms is None:
        wait_since_ms = now_ms
    should_notify = (not already_notified) and (now_ms - wait_since_ms) >= timeout_ms
    return (wait_since_ms, should_notify)
