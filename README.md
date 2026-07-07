# EV Charge Planner

Smart EV-ladning til Home Assistant, der lader bilen på de billigste timer ud fra
elpriser (Strømligning) og styrer en Zaptec-ladeboks. Porteret fra en Node-RED-automatisering
til en rigtig custom integration, så den kan installeres via HACS og fejlsøges ordentligt.

> **Status:** Under opbygning. Fase 1 (ren planlægningslogik + tests) er på plads.
> Coordinator, config flow og entities følger.

## Hvorfor en integration frem for Node-RED?

- **Indbygget fejlsøgning** — en `sensor.evcp_status` viser *hvorfor* der (ikke) lades lige nu,
  logbog-events ved hver handling (authorize/resume/stop), debug-logging og diagnostics-download.
- **Ingen helper-jungle** — integrationen ejer sine egne entities i stedet for 10+ manuelle
  `input_number`/`input_boolean`.
- **UI-opsætning** — vælg pris-sensor, Zaptec-entities og tilføj biler i grænsefladen.

## Funktioner (mål)

- Sliding-window prisoptimering (billigste 15-min-slots inden deadline)
- To modes: **Standard** (klar inden kl. 06:00) og **Afgang** (klar inden afrejsetid)
- Manuel SoC pr. bil (ingen bil-API krævet) — live SoC beregnes ud fra tilført energi
- Køretøjsstyring i UI: ingen standard-biler, kun **Guest** indbygget; tilføj selv biler
- Zaptec authorize/resume/stop med korrekt håndtering af bilskift og "bilen stoppede selv"
- Notifikation når ladningen faktisk starter

## Installation (via HACS) — når integrationen er færdig

1. HACS → Integrations → tre-prikker → **Custom repositories**
2. Tilføj `https://github.com/USERNAME/ev-charge-planner` som type **Integration**
3. Installér **EV Charge Planner**, genstart Home Assistant
4. Settings → Devices & Services → **Add Integration** → EV Charge Planner

## Udvikling

```bash
pip install -r requirements-dev.txt
pytest
```

Planlægningslogikken (`custom_components/ev_charge_planner/planner.py`) har ingen
Home Assistant-afhængigheder og kan testes isoleret.

## Licens

MIT
