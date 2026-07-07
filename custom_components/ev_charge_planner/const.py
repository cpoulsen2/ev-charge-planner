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

# --- Config entry: data (fast opsætning) ---
CONF_PRICE_SENSOR = "price_sensor"
CONF_CHARGER_MODE_SENSOR = "charger_mode_sensor"
CONF_CHARGE_POWER_SENSOR = "charge_power_sensor"
CONF_SESSION_ENERGY_SENSOR = "session_energy_sensor"
CONF_AUTHORIZE_BUTTON = "authorize_button"
CONF_RESUME_BUTTON = "resume_button"
CONF_STOP_BUTTON = "stop_button"
CONF_NOTIFY_SERVICE = "notify_service"

# --- Config entry: options (kan ændres senere) ---
CONF_VEHICLES = "vehicles"
CONF_MIN_BLOCK_MINUTES = "min_block_minutes"

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
