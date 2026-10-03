# EV Charge Planner

Smart EV-ladning til Home Assistant, der lader bilen på de billigste timer ud fra
elpriser (Strømligning) og styrer en Zaptec-ladeboks.

> **Status:** I brug. Planlægning, coordinator, config flow og alle entities er på plads
> og kører i produktion. Nye funktioner tilføjes løbende.

## Funktioner

- **Sliding-window prisoptimering** — vælger de billigste 15-min-slots inden deadline.
- **Klar til afgang** — bilen lades, så den er klar til afrejsetiden (standard: næste
  kl. 07:00). Du kan selv vælge et andet tidspunkt, fx i morgen kl. 17; det gælder, til
  bilen tages ud. Når stikket tages ud, går afgangen tilbage til næste kl. 07:00. Er
  afgangstiden passeret, mens bilen sidder i, gælder næste morgen kl. 07:00.
- **Ladevindue (tidligst start)** — du kan sætte et tidligst-start-tidspunkt, så
  planlæggeren kun vælger slots i vinduet `[tidligst start → afrejse]`. Slås til/fra med en
  kontakt; er den fra, bruges hele tiden frem til afrejse (som før).
- **Live SoC pr. bil** — tre kilder: live-sensor (fx Tesla), anker + tilført energi
  (fx VW, hvis sensoren kun opdaterer under kørsel), eller manuel + tilført energi (Gæst).
- **Ladetid til mål** — sensor der viser hvor lang tid der kræves for at nå målet.
- **Køretøjsstyring i UI** — ingen standard-biler, kun **Guest** indbygget; tilføj selv biler
  med kapacitet og valgfri SoC-sensor.
- **Sessionsstyring af Zaptec-laderen**:
  1. Når kablet tages ud, sættes laderens max current (`number.<lader>_charger_max_current`,
     findes automatisk) til **0 A**, så næste bil ikke begynder at lade ved isætning.
  2. Fra isætning til **første ladeslot** står den på 0 A.
  3. Ved første ladeslot (eller "Lad straks") sættes max current til ladestrømmen
     (standard **16 A**) — én gang.
  4. **Resten af sessionen** styres udelukkende med ladekontakten
     (`switch.<lader>_charging`): pause uden for slots, genoptag i slots. Max current
     røres ikke, før kablet tages ud.

  Alle kald til Zaptec gentages med stigende pauser, til laderen melder det ønskede
  resultat, og der er aldrig to kald i gang samtidig. Status viser fasen
  (`session_phase`), max current og hvad laderen gør.
- **Slås integrationen fra eller slettes**, sættes max current tilbage til ladestrømmen og
  en pauset lader genoptages, så den virker som en almindelig lader igen.
- **Robust over for genstart** — planen og strømstyringens tilstand gemmes, så Home
  Assistant kan genstarte midt i en ladning.
- **Observatør-tilstand** — beregn og log alt uden at røre laderen (til indkøring).
- **Notifikationer** — konfigurerbare beskeder ved ladestart, mål nået, bil tilsluttet m.m.

## Installation (via HACS)

1. HACS → Integrationer → tre-prikker → **Brugerdefinerede repositories**
2. Tilføj `https://github.com/cpoulsen2/ev-charge-planner` som type **Integration**
3. Installér **EV Charge Planner**, genstart Home Assistant
4. Indstillinger → Enheder & tjenester → **Tilføj integration** → EV Charge Planner
5. Vælg pris-sensor, Zaptec-sensorerne og **strøm-entiteten** — Zaptec-installationens
   *available current* (fx `number.zag089363_available_current`) — og tilføj dine biler
   under integrationens indstillinger.

### Krav til laderen

- **Autorisation skal være slået fra** i Zaptec (laderen må ikke vente på authorize).
- Zaptec-appens ladetilstand skal stå på **Standard**, ikke Planlagt, Automatisk eller Eco,
  og intet andet (automationer, Zaptec Sense) må ændre installationens available current.

### Opgradering fra 0.11 eller ældre

Tidligere versioner styrede laderen med authorize/resume/stop-knapper. Efter opgradering:
**Konfigurér → Indstillinger** → vælg strøm-entiteten og ladestrømmen. Indtil da rører
planneren ikke laderen (status viser *"Vælg strøm-entitet"*).

## Udvikling

```bash
pip install -r requirements-dev.txt
pytest
```

Planlægningslogikken (`planner.py`) og strømstyringens beslutninger (`guards.py`) har
ingen Home Assistant-afhængigheder og testes isoleret. `tests_ha/` indeholder en
end-to-end-simulation mod en rigtig HA-kerne og en falsk Zaptec-installation:

```bash
pip install -r requirements-ha-test.txt
pytest tests_ha -p no:cacheprovider -o asyncio_mode=auto
```

## Licens

MIT
