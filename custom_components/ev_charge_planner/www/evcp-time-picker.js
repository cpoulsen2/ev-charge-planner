/**
 * EV Charge Planner - date/time picker card.
 *
 * A small custom Lovelace card with two fields: a date on top and a time
 * below. Both are real HTML inputs, so on iPhone/iPad iOS opens its own
 * scroll wheel (date wheel / time wheel). Minutes step in 15-min increments.
 * On change the combined date+time is written to a `datetime` entity.
 *
 * Config:
 *   type: custom:evcp-time-picker
 *   entity: datetime.ev_charge_planner_departure
 *   label: "Departure"          # optional heading
 *
 * NOTE: keep this file ASCII-only so it can never be mangled by a wrong
 * charset when served. All user-facing text comes from the card config
 * (label) or the browser locale (date name), not from this file.
 */
var EVCP_STEP_MIN = 15; // minute resolution (quarter hour)

class EvcpTimePicker extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("evcp-time-picker: 'entity' is required");
    }
    this._config = config;
    this._built = false;
    this._lastState = null;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) this._build();
    var st = hass.states[this._config.entity];
    var state = st ? st.state : null;
    // Perf: only touch the DOM when this entity's own value changed
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
    var root = document.createElement("ha-card");
    root.style.padding = "12px 16px";

    var label = document.createElement("div");
    label.style.cssText =
      "font-size:13px;opacity:0.65;font-weight:600;margin-bottom:8px;";
    label.textContent = this._config.label || "Date / time";

    var baseInput =
      "width:100%;box-sizing:border-box;font-size:22px;font-weight:600;" +
      "color:inherit;background:rgba(127,127,127,0.15);border:none;" +
      "border-radius:12px;padding:10px 14px;text-align:center;" +
      "font-variant-numeric:tabular-nums;-webkit-appearance:none;" +
      "appearance:none;cursor:pointer;";

    var dateInput = document.createElement("input");
    dateInput.type = "date";
    dateInput.style.cssText = baseInput + "margin-bottom:8px;";
    dateInput.addEventListener("change", () => this._onChange());

    var timeInput = document.createElement("input");
    timeInput.type = "time";
    timeInput.step = String(EVCP_STEP_MIN * 60); // seconds -> 15-min steps
    timeInput.style.cssText = baseInput;
    timeInput.addEventListener("change", () => this._onChange());

    root.appendChild(label);
    root.appendChild(dateInput);
    root.appendChild(timeInput);
    this.innerHTML = "";
    this.appendChild(root);

    this._els = { dateInput: dateInput, timeInput: timeInput };
    this._built = true;
  }

  _update(state) {
    if (!this._els) return;
    var d =
      state && state !== "unknown" && state !== "unavailable"
        ? new Date(state)
        : null;
    var active = document.activeElement;
    var z = EvcpTimePicker._z;
    // Do not overwrite a field while the user has it open / focused
    if (active !== this._els.dateInput) {
      this._els.dateInput.value = d
        ? d.getFullYear() + "-" + z(d.getMonth() + 1) + "-" + z(d.getDate())
        : "";
    }
    if (active !== this._els.timeInput) {
      this._els.timeInput.value = d
        ? z(d.getHours()) + ":" + z(d.getMinutes())
        : "";
    }
  }

  _onChange() {
    if (!this._hass || !this._els) return;
    var dateVal = this._els.dateInput.value; // "YYYY-MM-DD"
    var timeVal = this._els.timeInput.value; // "HH:MM"
    if (!dateVal || !timeVal) return; // wait until both are set

    var dp = dateVal.split("-").map((n) => parseInt(n, 10));
    var tp = timeVal.split(":").map((n) => parseInt(n, 10));
    if (dp.length < 3 || tp.length < 2 || dp.concat(tp).some(isNaN)) return;

    var d = new Date(dp[0], dp[1] - 1, dp[2], tp[0], tp[1], 0, 0);
    // Snap to nearest quarter (also covers desktop input without step)
    d.setMinutes(Math.round(d.getMinutes() / EVCP_STEP_MIN) * EVCP_STEP_MIN, 0, 0);

    var z = EvcpTimePicker._z;
    var iso =
      d.getFullYear() + "-" + z(d.getMonth() + 1) + "-" + z(d.getDate()) + " " +
      z(d.getHours()) + ":" + z(d.getMinutes()) + ":00";

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
    name: "EV Charge Planner - Date/time picker",
    description:
      "Date+time picker (iOS wheel, 15-min steps) for EV Charge Planner datetime entities.",
  });
  console.info("%c EVCP-TIME-PICKER loaded", "color:#4ADE80");
}
