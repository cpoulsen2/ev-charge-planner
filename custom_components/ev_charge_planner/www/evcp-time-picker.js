/**
 * EV Charge Planner — dato/tid-vælger-kort.
 *
 * Et lille custom Lovelace-kort der viser en dato+tid som et rigtigt
 * <input type="datetime-local">. På iPhone/iPad åbner det iOS' eget
 * rulle-hjul med dato, time og minut. Minutter går i spring af 15 (kvarter).
 * Ved ændring skrives værdien direkte til en `datetime`-entitet.
 *
 * Konfiguration:
 *   type: custom:evcp-time-picker
 *   entity: datetime.ev_charge_planner_departure
 *   label: "⏰ Afgang"          # valgfri overskrift
 */
const EVCP_STEP_MIN = 15; // minut-opløsning (kvarter)

class EvcpTimePicker extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("evcp-time-picker: 'entity' mangler i konfigurationen");
    }
    if (config.entity.split(".")[0] !== "datetime") {
      throw new Error("evcp-time-picker: 'entity' skal være en datetime-entitet");
    }
    this._config = config;
    this._built = false;
    this._lastState = null;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) this._build();
    // Perf: opdatér kun DOM når netop denne entitets værdi har ændret sig
    const st = hass.states[this._config.entity];
    const state = st ? st.state : null;
    if (state !== this._lastState) {
      this._lastState = state;
      this._update(state);
    }
  }

  getCardSize() {
    return 1;
  }

  static _z(n) {
    return String(n).padStart(2, "0");
  }

  static _toLocalInput(d) {
    const z = EvcpTimePicker._z;
    return (
      `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}` +
      `T${z(d.getHours())}:${z(d.getMinutes())}`
    );
  }

  _build() {
    const root = document.createElement("ha-card");
    root.style.padding = "12px 16px";

    const label = document.createElement("div");
    label.style.cssText =
      "font-size:13px;opacity:0.65;font-weight:600;margin-bottom:8px;";
    label.textContent = this._config.label || "Dato / tid";

    const input = document.createElement("input");
    input.type = "datetime-local";
    input.step = String(EVCP_STEP_MIN * 60); // sekunder → 15-min spring
    input.style.cssText =
      "width:100%;box-sizing:border-box;font-size:22px;font-weight:600;" +
      "color:inherit;background:rgba(127,127,127,0.15);border:none;" +
      "border-radius:12px;padding:10px 14px;text-align:center;" +
      "font-variant-numeric:tabular-nums;-webkit-appearance:none;" +
      "appearance:none;cursor:pointer;";
    input.addEventListener("change", () => this._onChange(input.value));

    root.appendChild(label);
    root.appendChild(input);
    this.innerHTML = "";
    this.appendChild(root);

    this._els = { input };
    this._built = true;
  }

  _update(state) {
    if (!this._els) return;
    const d =
      state && state !== "unknown" && state !== "unavailable"
        ? new Date(state)
        : null;
    // Overskriv ikke feltet mens brugeren har det åbent/i fokus
    if (document.activeElement !== this._els.input) {
      this._els.input.value = d ? EvcpTimePicker._toLocalInput(d) : "";
    }
  }

  _onChange(value) {
    if (!value || !this._hass) return;
    // value er lokal tid uden tidszone, fx "2026-08-14T07:07"
    const d = new Date(value);
    if (isNaN(d.getTime())) return;
    // Rund til nærmeste kvarter (håndterer også desktop-input uden step)
    d.setMinutes(Math.round(d.getMinutes() / EVCP_STEP_MIN) * EVCP_STEP_MIN, 0, 0);

    const z = EvcpTimePicker._z;
    const iso =
      `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())} ` +
      `${z(d.getHours())}:${z(d.getMinutes())}:00`;

    this._hass.callService("datetime", "set_value", {
      entity_id: this._config.entity,
      datetime: iso,
    });
  }
}

if (!customElements.get("evcp-time-picker")) {
  customElements.define("evcp-time-picker", EvcpTimePicker);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "evcp-time-picker",
    name: "EV Charge Planner — Dato/tid-vælger",
    description:
      "Dato+tid-vælger (iOS-hjul, 15-min spring) til EV Charge Planner datetime-entiteter.",
  });
  console.info("%c EVCP-TIME-PICKER ⚙️ loaded", "color:#4ADE80");
}
