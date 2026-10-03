"""Konstanter for EV Charge Planner."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "ev_charge_planner"

PLATFORMS = [
    "select",
    "number",
    "datetime",
    "switch",
    "button",
    "sensor",
    "binary_sensor",
]

UPDATE_INTERVAL = timedelta(seconds=60)

# --- Lademodus ---
MODE_STANDARD = "Standard"
MODE_DEPARTURE = "Afgang"
CHARGE_MODES = [MODE_STANDARD, MODE_DEPARTURE]

# --- Køretøjsvalg ---
CHOOSE_VEHICLE = "Vælg bil"  # standard/ingen bil valgt
GUEST_VEHICLE = "Guest"

# --- Standardværdier ---
DEFAULT_POWER_KW = 11.0
DEFAULT_TARGET_SOC = 80.0
DEFAULT_GUEST_CAPACITY_KWH = 60.0
STANDARD_DEADLINE_HOUR = 6  # Standard-mode: klar inden kl. 06:00

# --- Car-side stop detection ---
CHARGE_POWER_THRESHOLD_KW = 0.1  # under dette regnes som "ingen strøm flyder"
CAR_SIDE_STOP_TICKS = 4  # antal minutter med 0 W efter ladning før "bilen stoppede selv"

# --- Strømstyring (laderen styres KUN via en strømgrænse: X A eller 0 A) ---
DEFAULT_CHARGE_CURRENT = 16  # A i ladeslots / "Lad straks"
CURRENT_CONFIRM_DELAY = timedelta(seconds=60)  # vent på at Zaptec melder ny værdi før genforsøg
CURRENT_RETRY_DELAYS = (  # derefter: genforsøg med stigende pause
    timedelta(minutes=2),
    timedelta(minutes=5),
    timedelta(minutes=10),
)
CURRENT_MIN_CHANGE_INTERVAL = timedelta(minutes=15)  # Zaptec: højst én ændring pr. 15 min
USER_ACTION_URGENCY = timedelta(minutes=2)  # brugerhandling må ændre strømmen med det samme
START_FAILED_TIMEOUT = timedelta(minutes=5)  # notificér hvis der ikke lades så længe i et slot
SLOW_CALL_WARNING = timedelta(seconds=10)  # Zaptec-kald længere end dette logges som advarsel

# --- Config entry: data (fast opsætning) ---
CONF_PRICE_SENSOR = "price_sensor"
CONF_TOMORROW_SENSOR = "tomorrow_sensor"  # valgfri: sensor med morgendagens priser
CONF_CHARGER_MODE_SENSOR = "charger_mode_sensor"
CONF_CHARGE_POWER_SENSOR = "charge_power_sensor"
CONF_SESSION_ENERGY_SENSOR = "session_energy_sensor"
CONF_NOTIFY_SERVICE = "notify_service"

# --- Config entry: options (kan ændres senere) ---
# Strøm-entiteten (fx number.zag089363_available_current) og ladestrømmen ligger i
# options, så de kan vælges under Konfigurér → Indstillinger (også på en gammel opsætning).
CONF_CURRENT_ENTITY = "current_entity"
CONF_CHARGE_CURRENT = "charge_current"
CONF_VEHICLES = "vehicles"
CONF_MIN_BLOCK_MINUTES = "min_block_minutes"
CONF_NOTIFY_TARGETS = "notify_targets"  # liste af notify.*-tjenester

# Notifikationstyper og deres standard (til/fra). Nøglerne gemmes i options.
NOTIFY_DEFAULTS = {
    "notify_charging_started": True,
    "notify_target_reached": True,
    "notify_car_side_stop": True,
    "notify_cable_connected": False,
    "notify_new_plan": False,
    "notify_not_enough_time": True,
}

# Sti som notifikationerne åbner ved tryk på selve beskeden.
# Relativ med vilje: så følger den den forbindelse appen er på
# (wifi hjemme / ekstern adgang ude) i stedet for en hårdkodet vært.
NOTIFY_CLICK_PATH = "/lovelace/electricity"

# --- Zaptec charger_mode værdier ---
CM_DISCONNECTED = "disconnected"
CM_REQUESTING = "connected_requesting"
CM_CHARGING = "connected_charging"
CM_FINISHED = "connected_finished"

# --- Beslutnings-actions (status-sensor) ---
ACT_IDLE = "idle"
ACT_START = "start"
ACT_PAUSE = "pause"
ACT_CHARGING = "charging"
ACT_TARGET_REACHED = "target_reached"
ACT_BLOCKED = "blocked"
ACT_WAITING = "waiting"

# --- Events (logbog / fejlsøgning) ---
EVENT_ACTION = f"{DOMAIN}_action"
