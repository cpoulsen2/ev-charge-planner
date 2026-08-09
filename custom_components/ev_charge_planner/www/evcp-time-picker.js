/**
 * EV Charge Planner — tidsvælger-kort.
 *
 * Et lille custom Lovelace-kort der viser et klokkeslæt som et rigtigt
 * <input type="time">. På iPhone/iPad åbner det iOS' eget rulle-hjul.
 * Ved ændring skrives til en `datetime`-entitet, og datoen sættes automatisk
 * til næste gang det klokkeslæt indtræffer (så man kun skal tænke på tiden).
 *
 * Konfiguration:
 *   type: custom:evcp-time-picker
 *   entity: datetime.ev_charge_planner_departure
 *   label: "⏰ Afgang"          # valgfri overskrift
 */
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
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) this._build();
    this._update();
  }

  getCardSize() {
    return 1;
  }

  static _z(n) {
    return String(n).padStart(2, "0");
  }

  _build() {
    const root = document.createElement("ha-card");
    root.style.padding = "10px 16px";

    const row = document.createElement("div");
    row.style.cssText =
      "display:flex;align-items:center;justify-content:space-between;gap:12px;";

    const left = document.createElement("div");
    const label = document.createElement("div");
    label.style.cssText = "font-size:13px;opacity:0.65;font-weight:600;";
    label.textContent = this._config.label || "Tidspunkt";
    const day = document.createElement("div");
    day.style.cssText =
      "font-size:12px;opacity:0.45;text-transform:capitalize;min-height:14px;";
    left.appendChild(label);
    left.appendChild(day);

    const input = document.createElement("input");
    input.type = "time";
    input.style.cssText =
      "font-size:30px;font-weight:600;color:inherit;" +
      "background:rgba(127,127,127,0.15);border:none;border-radius:12px;" +
      "padding:6px 14px;text-align:center;font-variant-numeric:tabular-nums;" +
      "-webkit-appearance:none;appearance:none;cursor:pointer;";
    // Ændringen håndteres direkte af det ægte DOM-element — ingen sanitizer imellem
    input.addEventListener("change", () => this._onChange(input.value));

    row.appendChild(left);
    row.appendChild(input);
    root.appendChild(row);
    this.innerHTML = "";
    this.appendChild(root);

    this._els = { day, input };
    this._built = true;
  }

  _update() {
    if (!this._hass || !this._els) return;
    const st = this._hass.states[this._config.entity];
    const raw = st ? st.state : null;
    const d =
      raw && raw !== "unknown" && raw !== "unavailable" ? new Date(raw) : null;

    // Overskriv ikke feltet mens brugeren har det åbent/i fokus
    if (document.activeElement !== this._els.input) {
      const z = EvcpTimePicker._z;
      this._els.input.value = d ? `${z(d.getHours())}:${z(d.getMinutes())}` : "";
    }
    this._els.day.textContent = d
      ? d.toLocaleDateString("da-DK", {
          weekday: "long",
          day: "numeric",
          month: "short",
        })
      : "";
  }

  _onChange(value) {
    if (!value || !this._hass) return;
    const parts = value.split(":");
    if (parts.length < 2) return;
    const h = parseInt(parts[0], 10);
    const m = parseInt(parts[1], 10);
    if (isNaN(h) || isNaN(m)) return;

    const now = new Date();
    const t = new Date(now.getFullYear(), now.getMonth(), now.getDate(), h, m, 0, 0);
    // Næste gang klokkeslættet indtræffer (datoen regnes ud automatisk)
    if (t.getTime() <= now.getTime()) t.setDate(t.getDate() + 1);

    const z = EvcpTimePicker._z;
    const iso =
      `${t.getFullYear()}-${z(t.getMonth() + 1)}-${z(t.getDate())} ` +
      `${z(t.getHours())}:${z(t.getMinutes())}:00`;

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
    name: "EV Charge Planner — Tidsvælger",
    description: "Klokkeslæt-vælger (iOS-hjul) til EV Charge Planner datetime-entiteter.",
  });
  console.info("%c EVCP-TIME-PICKER ⚙️ loaded", "color:#4ADE80");
}
