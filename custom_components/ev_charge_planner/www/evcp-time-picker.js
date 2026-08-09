/**
 * EV Charge Planner — dato/tid-vælger-kort.
 *
 * Et lille custom Lovelace-kort med to felter: dato øverst og klokkeslæt
 * nedenunder. Begge er rigtige HTML-inputs, så på iPhone/iPad åbner iOS'
 * eget rulle-hjul (dato-hjul hhv. tids-hjul). Minutter går i spring af 15.
 * Ved ændring skrives den kombinerede dato+tid direkte til en `datetime`-entitet.
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
    return 2;
  }

  static _z(n) {
    return String(n).padStart(2, "0");
  }

  _build() {
    const z = EvcpTimePicker._z;
    const root = document.createElement("ha-card");
    root.style.padding = "12px 16px";

    const label = document.createElement("div");
    label.style.cssText =
      "font-size:13px;opacity:0.65;font-weight:600;margin-bottom:8px;";
    label.textContent = this._config.label || "Dato / tid";

    const baseInput =
      "width:100%;box-sizing:border-box;font-size:22px;font-weight:600;" +
      "color:inherit;background:rgba(127,127,127,0.15);border:none;" +
      "border-radius:12px;padding:10px 14px;text-align:center;" +
      "font-variant-numeric:tabular-nums;-webkit-appearance:none;" +
      "appearance:none;cursor:pointer;";

    const dateInput = document.createElement("input");
    dateInput.type = "date";
    dateInput.style.cssText = baseInput + "margin-bottom:8px;";
    dateInput.addEventListener("change", () => this._onChange());

    const timeInput = document.createElement("input");
    timeInput.type = "time";
    timeInput.step = String(EVCP_STEP_MIN * 60); // sekunder → 15-min spring
    timeInput.style.cssText = baseInput;
    timeInput.addEventListener("change", () => this._onChange());

    root.appendChild(label);
    root.appendChild(dateInput);
    root.appendChild(timeInput);
    this.innerHTML = "";
    this.appendChild(root);

    this._els = { dateInput, timeInput };
    this._built = true;
  }

  _update(state) {
    if (!this._els) return;
    const d =
      state && state !== "unknown" && state !== "unavailable"
        ? new Date(state)
        : null;
    const active = document.activeElement;
    const z = EvcpTimePicker._z;
    // Overskriv ikke et felt mens brugeren har det åbent/i fokus
    if (active !== this._els.dateInput) {
      this._els.dateInput.value = d
        ? `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}`
        : "";
    }
    if (active !== this._els.timeInput) {
      this._els.timeInput.value = d
        ? `${z(d.getHours())}:${z(d.getMinutes())}`
        : "";
    }
  }

  _onChange() {
    if (!this._hass || !this._els) return;
    const dateVal = this._els.dateInput.value; // "YYYY-MM-DD"
    const timeVal = this._els.timeInput.value; // "HH:MM"
    if (!dateVal || !timeVal) return; // vent til begge er sat

    const dp = dateVal.split("-").map((n) => parseInt(n, 10));
    const tp = timeVal.split(":").map((n) => parseInt(n, 10));
    if (dp.length < 3 || tp.length < 2 || dp.concat(tp).some(isNaN)) return;

    const d = new Date(dp[0], dp[1] - 1, dp[2], tp[0], tp[1], 0, 0);
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
