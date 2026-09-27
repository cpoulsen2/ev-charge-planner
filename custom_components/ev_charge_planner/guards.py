"""Rene beslutningsfunktioner for authorize-styring og giv-op-timer.

Dette modul har BEVIDST ingen Home Assistant-afhængigheder, så det kan
unit-testes isoleret (som ``planner.py``). Al tilstand gives ind som argumenter;
funktionerne har ingen sideeffekter. Coordinatoren holder selve tilstanden
(persisteret i ``Runtime``) og kalder disse funktioner.

Baggrund: Zaptec-laderen låser hvis den modtager to authorize-kommandoer uden at
kablet tages ud imellem. Derfor må authorize sendes højst én gang, indtil kablet
tages ud, laderen faktisk har ladet (autorisationen er brugt), eller vi selv har
sendt deauthorize — med en hård minimums-tid mellem to tryk som backstop.
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


RESCUE_NONE = "none"
RESCUE_WAIT = "wait"
RESCUE_DEAUTHORIZE = "deauthorize"
RESCUE_AUTHORIZE = "authorize"
RESCUE_ABORT = "abort"


def rescue_step(
    *,
    is_requesting: bool,
    power_flowing: bool,
    authorize_done: bool,
    press_inflight: bool,
    gave_up: bool,
    phase: str,
    rounds_used: int,
    max_rounds: int,
    has_deauthorize: bool,
    authorize_sent_ms: int | None,
    deauth_done_ms: int | None,
    mode_since_ms: int | None,
    now_ms: int,
    confirm_ms: int,
    settle_ms: int,
    deauth_max_wait_ms: int,
) -> str:
    """Redningssekvens når et authorize ikke gav strøm: deauthorize → vent → ét authorize.

    - Mens et Zaptec-kald stadig kører (integrationen kan selv gentage det i op til
      ~100 s), gøres intet nyt — ellers kunne et forsinket authorize ramme efter vores
      deauthorize og give to autorisationer.
    - ``phase == "deauthorized"``: vent til laderen har stået stabilt i requesting i
      ``settle_ms`` efter deauthorize, og send så ét authorize. Kommer den ikke tilbage
      i requesting inden ``deauth_max_wait_ms`` → opgiv redningen.
    - Ellers: start en runde hvis authorize er sendt, laderen stadig står i requesting
      uden strøm ``confirm_ms`` efter kaldet blev færdigt, og der er runder tilbage.
    """
    if power_flowing:
        return RESCUE_NONE
    if press_inflight:
        return RESCUE_WAIT
    if phase == "deauthorized":
        if deauth_done_ms is None:
            return RESCUE_WAIT
        waited = now_ms - deauth_done_ms
        stable = mode_since_ms is not None and now_ms - mode_since_ms >= settle_ms
        if is_requesting and waited >= settle_ms and stable:
            return RESCUE_AUTHORIZE
        if waited >= deauth_max_wait_ms:
            return RESCUE_ABORT
        return RESCUE_WAIT
    if gave_up or not has_deauthorize or rounds_used >= max_rounds:
        return RESCUE_NONE
    if (
        authorize_done
        and is_requesting
        and authorize_sent_ms is not None
        and now_ms - authorize_sent_ms >= confirm_ms
    ):
        return RESCUE_DEAUTHORIZE
    return RESCUE_NONE


def start_delay_remaining_ms(*, now_ms: int, block_start_ms: int, delay_ms: int) -> int:
    """Hvor længe endnu før det første authorize i et slot må sendes (0 = nu).

    Undgår det præcise kvarterskifte, hvor mange pris-automatikker rammer Zaptec-skyen
    samtidig og svaret bliver langsomt nok til at Zaptec-integrationen gentager kaldet.
    """
    return max(0, block_start_ms + delay_ms - now_ms)


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
