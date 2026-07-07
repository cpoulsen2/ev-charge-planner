"""Konstanter for EV Charge Planner."""

from __future__ import annotations

DOMAIN = "ev_charge_planner"

# --- Lademodus ---
MODE_STANDARD = "Standard"
MODE_DEPARTURE = "Afgang"

# --- Indbygget køretøj ---
GUEST_VEHICLE = "Guest"

# --- Standardværdier ---
DEFAULT_POWER_KW = 11.0
DEFAULT_GUEST_CAPACITY_KWH = 60.0
STANDARD_DEADLINE_HOUR = 6  # Standard-mode: klar inden kl. 06:00

# --- Car-side stop detection ---
CHARGE_POWER_THRESHOLD_KW = 0.1  # under dette regnes som "ingen strøm flyder"
CAR_SIDE_STOP_TICKS = 4  # antal minutter med 0 W efter ladning før "bilen stoppede selv"

# --- Config entry nøgler ---
CONF_PRICE_SENSOR = "price_sensor"
CONF_CHARGER_MODE_SENSOR = "charger_mode_sensor"
CONF_CHARGE_POWER_SENSOR = "charge_power_sensor"
CONF_SESSION_ENERGY_SENSOR = "session_energy_sensor"
CONF_AUTHORIZE_BUTTON = "authorize_button"
CONF_RESUME_BUTTON = "resume_button"
CONF_STOP_BUTTON = "stop_button"
CONF_NOTIFY_SERVICE = "notify_service"
CONF_VEHICLES = "vehicles"

# --- Zaptec charger_mode værdier ---
MODE_DISCONNECTED = "disconnected"
MODE_REQUESTING = "connected_requesting"
MODE_CHARGING = "connected_charging"
MODE_FINISHED = "connected_finished"
