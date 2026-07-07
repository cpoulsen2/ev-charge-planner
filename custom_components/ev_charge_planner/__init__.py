"""EV Charge Planner — Home Assistant custom integration.

Smart EV-ladning ud fra elpriser (Strømligning) med Zaptec-styring.
Porteret fra en Node-RED-automatisering.

Denne pakke bygges i faser:
  Fase 1 (nu): ren planner-logik (``planner.py``) med tests.
  Fase 2: coordinator (minut-loop), config flow og entities.

``__init__`` holdes bevidst fri for tunge Home Assistant-imports på modul-niveau,
så ``planner.py`` kan importeres og testes uden en HA-installation.
"""

DOMAIN = "ev_charge_planner"
