"""
Dynamisch Handelen - Home Assistant integratie (AppDaemon)
==========================================================

AppDaemon app die elke 5 minuten de optimale laad/ontlaad strategie
berekent en publiceert als sensor.dynamisch_handelsstrategie.

BESTANDSLOCATIES IN HA
-----------------------
  /config/appdaemon/apps/dynamisch_handelen.py   ← dit bestand
  /config/appdaemon/apps/strategie_dp.py         ← het algoritme

AppDaemon voegt de apps-map automatisch toe aan sys.path,
waardoor `from strategie_dp import ...` direct werkt.

GEBRUIKTE HA-ENTITEITEN
------------------------
Ingangen:
  sensor.dynamisch_nordpool                              prijs vandaag + morgen
  sensor.zendure_2400_ac_indicatie_beschikbare_energie   kWh leverbaar naar net
  sensor.zendure_2400_ac_indicatie_benodigde_energie     kWh nodig van net om vol te laden
  sensor.zendure_2400_ac_rte_totaal                      round-trip efficiency (%)
  input_number.zendure_2400_ac_max_oplaadvermogen        max laadvermogen (W)
  input_number.zendure_2400_ac_max_ontlaadvermogen       max ontlaadvermogen (W)
  input_number.dynamisch_minimale_spread                 minimale spread (ct/kWh)

Uitgang:
  sensor.dynamisch_handelsstrategie   verwachte winst (€) + volledig schema als attribuut
"""

import math
from datetime import datetime

import appdaemon.plugins.hass.hassapi as hass

# strategie_dp.py staat in dezelfde apps-map; AppDaemon zet die map op sys.path.
from strategie_dp import Accustatus, los_dp_op


class DynamischHandelen(hass.Hass):

    def initialize(self):
        """
        AppDaemon roept initialize() aan bij opstarten en na een reload.
        We registreren hier één terugkerende taak: elke 5 minuten herberekenen.
        'now' zorgt dat de eerste berekening meteen bij het opstarten plaatsvindt.
        """
        self.log("Dynamisch Handelen: gestart, schema wordt elke 5 minuten herberekend")
        self.run_every(self.bereken_strategie, "now", 5 * 60)

    # ── HOOFDFUNCTIE ─────────────────────────────────────────────────────────

    def bereken_strategie(self, kwargs):
        """
        Haalt data op, berekent de strategie en publiceert het resultaat.
        Fouten worden gelogd maar laten AppDaemon verder draaien.
        """
        self.log("Dynamisch Handelen: strategie berekening gestart", level="DEBUG")

        try:
            slots = self._haal_prijsslots()
        except Exception as exc:
            self.log(f"Dynamisch Handelen: fout bij ophalen prijsslots: {exc}", level="ERROR")
            return

        if not slots:
            self.log("Dynamisch Handelen: geen prijsslots beschikbaar", level="WARNING")
            self.set_state(
                "sensor.dynamisch_handelsstrategie",
                state="geen_data",
                attributes={"slots": [], "verwachte_winst_eur": 0},
            )
            return

        try:
            accu = self._haal_accustatus()
        except Exception as exc:
            self.log(f"Dynamisch Handelen: fout bij ophalen accustatus: {exc}", level="ERROR")
            return

        if accu.max_kwh <= 0:
            self.log(
                "Dynamisch Handelen: accucapaciteit onbekend (beschikbaar + benodigde = 0 kWh)",
                level="WARNING",
            )
            return

        min_spread = self._haal_minimale_spread()

        self.log(
            f"Dynamisch Handelen: {len(slots)} slots | "
            f"accu {accu.huidig_kwh:.2f}/{accu.max_kwh:.2f} kWh | "
            f"eta={accu.eta_laad:.3f} | "
            f"laad {accu.max_laad_w:.0f} W / ontlaad {accu.max_ontlaad_w:.0f} W | "
            f"min spread {min_spread:.1f} ct/kWh",
            level="INFO",
        )

        schema = los_dp_op(slots, accu, min_spread_ct_per_kwh=min_spread)

        verwachte_winst = sum(s["winst_eur"] for s in schema)
        laad_slots      = [s for s in schema if s["actie"] == "laden"]
        ontlaad_slots   = [s for s in schema if s["actie"] == "ontladen"]
        volgende        = next((s for s in schema if s["actie"] != "rust"), None)

        self.log(
            f"Dynamisch Handelen: verwacht EUR {verwachte_winst:.3f} | "
            f"{len(laad_slots)} laadslots / {len(ontlaad_slots)} ontlaadslots | "
            f"volgende: {volgende['actie'] if volgende else 'rust'} "
            f"om {volgende['start'] if volgende else '-'}",
            level="INFO",
        )

        self.set_state(
            "sensor.dynamisch_handelsstrategie",
            state=round(verwachte_winst, 3),
            attributes={
                "unit_of_measurement": "EUR",
                "friendly_name":       "Dynamisch Handelen Verwachte Winst",
                "icon":                "mdi:cash-plus",
                "device_class":        "monetary",
                "slots":               schema,
                "laad_slots":          len(laad_slots),
                "ontlaad_slots":       len(ontlaad_slots),
                "volgende_actie":      volgende["actie"] if volgende else "rust",
                "volgende_start":      volgende["start"] if volgende else None,
                "accu_huidig_kwh":     round(accu.huidig_kwh, 3),
                "accu_max_kwh":        round(accu.max_kwh,    3),
                "eta":                 round(accu.eta_laad,   3),
                "min_spread_ct":       min_spread,
                "bijgewerkt":          datetime.now().isoformat(),
            },
        )

    # ── DATA OPHALEN ─────────────────────────────────────────────────────────

    def _haal_prijsslots(self) -> list[dict]:
        """
        Haalt alle beschikbare toekomstige prijsslots op uit de Nordpool sensor.

        sensor.dynamisch_nordpool aggregeert de ruwe Nordpool-data al naar de
        gewenste tijdsresolutie (uur of kwartier) via de bestaande YAML-sensor.
        We gebruiken raw_today én raw_tomorrow zodat we altijd zo ver mogelijk
        vooruit plannen — morgen is beschikbaar vanaf ±14:00.

        Slots die al volledig zijn verstreken worden overgeslagen.
        """
        attr         = self.get_state("sensor.dynamisch_nordpool", attribute="all") or {}
        attributes   = attr.get("attributes", {})
        raw_today    = attributes.get("raw_today")    or []
        raw_tomorrow = attributes.get("raw_tomorrow") or []

        nu    = datetime.now().astimezone()
        slots = []

        for item in raw_today + raw_tomorrow:
            try:
                start = datetime.fromisoformat(str(item["start"])).astimezone()
                end   = datetime.fromisoformat(str(item["end"])).astimezone()
            except (KeyError, ValueError, TypeError) as exc:
                self.log(f"Dynamisch Handelen: ongeldig prijsslot overgeslagen: {exc}", level="WARNING")
                continue

            if end <= nu:
                continue

            slots.append({
                "start":      start,
                "end":        end,
                "price":      float(item["value"]),
                "duration_h": (end - start).total_seconds() / 3600.0,
            })

        slots.sort(key=lambda s: s["start"])
        return slots

    def _haal_accustatus(self) -> Accustatus:
        """
        Leest de actuele batterijstatus uit HA en converteert naar interne eenheden.

        DE TWEE ENERGIE-SENSOREN
        ------------------------
        beschikbare_energie (kWh): energie leverbaar naar net vanuit huidige SoC.
            Berekend als: (soc - min_soc)% × totale_cap × η_ontlaad

        benodigde_energie (kWh): energie nodig van net om accu naar max_soc te laden.
            Berekend als: (max_soc - soc)% × totale_cap / η_laad

        Beide zijn neteenheden (al gecorrigeerd voor η). Voor het DP-algoritme
        converteren we naar opgeslagen kWh (batterij-intern):

            stored_current = beschikbare / η     (verwijder de ontlaadkorting)
            stored_ruimte  = benodigde × η       (verwijder de laadtoeslag)

        RTE: η_laad = η_ontlaad = √(RTE/100). Zie strategie_dp.py voor uitleg.
        """
        beschikbaar_kwh = float(self.get_state("sensor.zendure_2400_ac_indicatie_beschikbare_energie") or 0)
        benodigde_kwh   = float(self.get_state("sensor.zendure_2400_ac_indicatie_benodigde_energie")   or 0)
        rte_pct         = float(self.get_state("sensor.zendure_2400_ac_rte_totaal")                    or 90)
        max_laad_w      = float(self.get_state("input_number.zendure_2400_ac_max_oplaadvermogen")       or 2400)
        max_ontlaad_w   = float(self.get_state("input_number.zendure_2400_ac_max_ontlaadvermogen")      or 2400)

        rte_pct = max(50.0, min(100.0, rte_pct))
        eta     = math.sqrt(rte_pct / 100.0)

        stored_current = beschikbaar_kwh / eta
        stored_ruimte  = benodigde_kwh   * eta

        return Accustatus(
            huidig_kwh    = stored_current,
            max_kwh       = stored_current + stored_ruimte,
            eta_laad      = eta,
            eta_ontlaad   = eta,
            max_laad_w    = max_laad_w,
            max_ontlaad_w = max_ontlaad_w,
        )

    def _haal_minimale_spread(self) -> float:
        """
        Leest de gebruikersingestelde minimale spread (ct/kWh).
        Voorkomt handel bij kleine prijsverschillen die weliswaar theoretisch
        winstgevend zijn maar in de praktijk onzeker zijn.
        """
        return float(self.get_state("input_number.dynamisch_minimale_spread") or 0)
