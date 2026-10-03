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
- **Styring via strømgrænse** — laderen styres kun med installationens strøm (fx
  `number.zag089363_available_current`): ladestrøm (standard 16 A) i ladeslots og ved
  "Lad straks", ellers 0 A. Sætter man stikket i uden for et slot, lader bilen ikke.
  Kaldet til Zaptec gentages med stigende pauser, til laderen melder den ønskede værdi,
  og der er aldrig to kald i gang samtidig. Planlagte ændringer følger Zaptecs anbefaling
  om højst én ændring pr. 15 min; brugerhandlinger (Stop, Lad straks) sker med det samme.
- **Slås integrationen fra eller slettes**, sættes strømmen tilbage til ladestrømmen, så
  laderen virker som en almindelig lader igen.
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
