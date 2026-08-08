# EV Charge Planner

Smart EV-ladning til Home Assistant, der lader bilen på de billigste timer ud fra
elpriser (Strømligning) og styrer en Zaptec-ladeboks. Porteret fra en Node-RED-automatisering
til en rigtig custom integration, så den kan installeres via HACS og fejlsøges ordentligt.

> **Status:** I brug. Planlægning, coordinator, config flow og alle entities er på plads
> og kører i produktion. Nye funktioner tilføjes løbende.

## Hvorfor en integration frem for Node-RED?

- **Indbygget fejlsøgning** — `sensor.ev_charge_planner_status` viser *hvorfor* der (ikke) lades
  lige nu, med logbog-events ved hver handling (authorize/resume/stop) og debug-logging.
- **Ingen helper-jungle** — integrationen ejer sine egne entities i stedet for 10+ manuelle
  `input_number`/`input_boolean`.
- **UI-opsætning** — vælg pris-sensor, Zaptec-entities og tilføj biler i grænsefladen.

## Funktioner

- **Sliding-window prisoptimering** — vælger de billigste 15-min-slots inden deadline.
- **To modes:** **Standard** (klar inden kl. 06:00) og **Afgang** (klar inden afrejsetid).
  Afgang-tidspunktet holdes automatisk i fremtiden (næste kl. 07:00), men kan overstyres.
- **Ladevindue (tidligst start)** — i Afgang kan du sætte et tidligst-start-tidspunkt, så
  planlæggeren kun vælger slots i vinduet `[tidligst start → afrejse]`. Slås til/fra med en
  kontakt; er den fra, bruges hele tiden frem til afrejse (som før).
- **Live SoC pr. bil** — tre kilder: live-sensor (fx Tesla), anker + tilført energi
  (fx VW, hvis sensoren kun opdaterer under kørsel), eller manuel + tilført energi (Gæst).
- **Ladetid til mål** — sensor der viser hvor lang tid der kræves for at nå målet.
- **Køretøjsstyring i UI** — ingen standard-biler, kun **Guest** indbygget; tilføj selv biler
  med kapacitet og valgfri SoC-sensor.
- **Zaptec authorize/resume/stop** med korrekt håndtering af bilskift, "bilen stoppede selv"
  og genstart af Home Assistant midt i en ladning (planen persisteres).
- **Observatør-tilstand** — beregn og log alt uden at røre laderen (til indkøring).
- **Notifikationer** — konfigurerbare beskeder ved ladestart, mål nået, bil tilsluttet m.m.

## Installation (via HACS)

1. HACS → Integrationer → tre-prikker → **Brugerdefinerede repositories**
2. Tilføj `https://github.com/cpoulsen2/ev-charge-planner` som type **Integration**
3. Installér **EV Charge Planner**, genstart Home Assistant
4. Indstillinger → Enheder & tjenester → **Tilføj integration** → EV Charge Planner
5. Vælg pris-sensor og Zaptec-entities, og tilføj dine biler under integrationens indstillinger.

## Udvikling

```bash
pip install -r requirements-dev.txt
pytest
```

Planlægningslogikken (`custom_components/ev_charge_planner/planner.py`) har ingen
Home Assistant-afhængigheder og kan testes isoleret.

## Licens

MIT
