"""
Dynamisch Handelen - Home Assistant integratie (AppDaemon)
==========================================================

AppDaemon app die elke minuut de optimale laad/ontlaad strategie
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
  sensor.zendure_2400_ac_laadpercentage                  actuele SoC (%) voor actieve-slot-correctie
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
from strategie_dp import (
    Accustatus,
    bereken_derating,
    bereken_laadvermogen_voor_aansturing,
    los_dp_op,
    rond_vermogen_omhoog,
)


class DynamischHandelen(hass.Hass):

    def initialize(self):
        """
        AppDaemon roept initialize() aan bij opstarten en na een reload.
        We registreren hier één terugkerende taak: elke minuut herberekenen.
        'now' zorgt dat de eerste berekening meteen bij het opstarten plaatsvindt.
        """
        self.log("Dynamisch Handelen: gestart, schema wordt elke minuut herberekend")
        self.run_every(self.bereken_strategie, "now", 60)

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
            accu, hw_min_pct, hw_max_pct = self._haal_accustatus()
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
        self._corrigeer_actief_slot_vermogen(schema, accu, hw_min_pct, hw_max_pct)
        spread_blokkades = self._markeer_spread_blokkades(schema, accu.eta_laad, min_spread)

        # Vertaal DP-interne SoC% (0–100% van hw-venster) naar echte battery-%
        # zodat de grafiek overeenkomt met wat de Zendure rapporteert.
        hw_range = hw_max_pct - hw_min_pct
        for s in schema:
            s["soc_voor_pct"] = round(hw_min_pct + s["soc_voor_pct"] / 100.0 * hw_range, 1)
            s["soc_na_pct"]   = round(hw_min_pct + s["soc_na_pct"]   / 100.0 * hw_range, 1)

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
                "spread_blokkades":    spread_blokkades,
                "spread_blokkades_aantal": len(spread_blokkades),
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

            effectieve_start = max(start, nu)
            slots.append({
                "start":      start,
                "end":        end,
                "price":      float(item["value"]),
                "duration_h": (end - effectieve_start).total_seconds() / 3600.0,
            })

        slots.sort(key=lambda s: s["start"])
        return slots

    # ── SPREAD-UITLEG ────────────────────────────────────────────────────────

    def _markeer_spread_blokkades(
        self,
        schema: list[dict],
        eta: float,
        min_spread_ct: float,
    ) -> list[dict]:
        """
        Zet uitleg op rust-slots waar de ruwe spread wel zichtbaar is maar
        min_spread_ct na rendement niet wordt gehaald.

        Voor elk rust-slot zoekt de functie de hoogste toekomstige prijs in het
        schema. Als beste_verkoop_ct hoger is dan prijs_ct maar lager dan de
        benodigde verkoopprijs, krijgt het slot velden voor dashboard en debug.
        """
        blokkades: list[dict] = []
        if eta <= 0:
            return blokkades

        spread_helft = min_spread_ct / 2.0
        eta_kwadraat = eta * eta

        for i, slot in enumerate(schema):
            prijs_ct = slot.get("prijs_ct")
            if slot.get("actie") != "rust" or prijs_ct is None:
                continue

            toekomstige_prijzen = [
                s.get("prijs_ct")
                for s in schema[i + 1:]
                if isinstance(s.get("prijs_ct"), (int, float))
            ]
            if not toekomstige_prijzen:
                continue

            beste_verkoop_ct = max(toekomstige_prijzen)
            bruto_spread_ct = beste_verkoop_ct - prijs_ct
            benodigde_verkoop_ct = ((prijs_ct + spread_helft) / eta_kwadraat) + spread_helft

            if bruto_spread_ct < min_spread_ct or beste_verkoop_ct >= benodigde_verkoop_ct:
                continue

            blokkade = {
                "start": slot.get("start"),
                "end": slot.get("end"),
                "prijs_ct": round(prijs_ct, 3),
                "beste_verkoop_ct": round(beste_verkoop_ct, 3),
                "benodigde_verkoop_ct": round(benodigde_verkoop_ct, 3),
                "bruto_spread_ct": round(bruto_spread_ct, 3),
                "spread_tekort_ct": round(benodigde_verkoop_ct - beste_verkoop_ct, 3),
                "min_spread_ct": round(min_spread_ct, 3),
                "reden": "spread_te_klein",
            }

            slot["geen_laden_reden"] = "spread_te_klein"
            slot["beste_verkoop_ct"] = blokkade["beste_verkoop_ct"]
            slot["benodigde_verkoop_ct"] = blokkade["benodigde_verkoop_ct"]
            slot["bruto_spread_ct"] = blokkade["bruto_spread_ct"]
            slot["spread_tekort_ct"] = blokkade["spread_tekort_ct"]
            slot["min_spread_ct"] = blokkade["min_spread_ct"]
            blokkades.append(blokkade)

        return blokkades

    # ── ACTIEVE-SLOT-CORRECTIE ───────────────────────────────────────────────

    def _corrigeer_actief_slot_vermogen(
        self,
        schema: list[dict],
        accu: "Accustatus",
        hw_min_pct: float,
        hw_max_pct: float,
    ) -> None:
        """
        Corrigeert alleen het lopende slot op basis van de actuele Zendure-SoC.

        los_dp_op() maakt een target voor het einde van elk prijsslot. Binnen het
        huidige slot gebruiken we die target als vaste opdracht. Bij laden met
        bereken_derating(actuele_soc_kwh, accu.max_kwh) lager dan 1.0 blijft
        vermogen_w gelijk aan accu.max_laad_w; verwacht_vermogen_w krijgt dan de
        lagere verwachte BMS-opname.
        """
        nu = datetime.now().astimezone()
        actief = None
        actief_index = None
        for index, slot in enumerate(schema):
            try:
                start = datetime.fromisoformat(str(slot["start"])).astimezone()
                end = datetime.fromisoformat(str(slot["end"])).astimezone()
            except (KeyError, ValueError, TypeError):
                continue
            if start <= nu < end:
                actief = slot
                actief_index = index
                break

        if actief is None or actief_index is None or actief.get("actie") not in ("laden", "ontladen"):
            return

        actuele_soc_kwh = self._haal_actuele_soc_kwh_via_laadpercentage(
            accu.max_kwh,
            hw_min_pct,
            hw_max_pct,
        )
        if actuele_soc_kwh is None:
            return

        doel = self._haal_actief_slot_doel(actief, actuele_soc_kwh)
        actie = doel["actie"]
        target_kwh = doel["target_kwh"]
        begin_kwh = doel["begin_kwh"]

        try:
            eindtijd = datetime.fromisoformat(str(actief["end"])).astimezone()
        except (KeyError, ValueError, TypeError):
            return

        resterend_h = max(0.0, (eindtijd - nu).total_seconds() / 3600.0)
        if resterend_h <= 0.0:
            return

        if actie == "laden":
            target_kwh = self._verhoog_actief_laadslot_doel(
                schema,
                actief_index,
                target_kwh,
                actuele_soc_kwh,
                accu,
                resterend_h,
            )
            delta_kwh = max(0.0, target_kwh - actuele_soc_kwh)
            energie_net_kwh = delta_kwh / accu.eta_laad if accu.eta_laad > 0 else 0.0
            verwacht_vermogen_w = min(
                accu.max_laad_w,
                energie_net_kwh / resterend_h * 1000.0 if resterend_h > 0 else 0.0,
            )
            derating = bereken_derating(actuele_soc_kwh, accu.max_kwh)
            vermogen_w = bereken_laadvermogen_voor_aansturing(
                verwacht_vermogen_w,
                accu.max_laad_w,
                derating,
            )
        else:
            delta_kwh = max(0.0, actuele_soc_kwh - target_kwh)
            energie_net_kwh = delta_kwh * accu.eta_ontlaad
            verwacht_vermogen_w = min(
                accu.max_ontlaad_w,
                energie_net_kwh / resterend_h * 1000.0 if resterend_h > 0 else 0.0,
            )
            vermogen_w = rond_vermogen_omhoog(verwacht_vermogen_w, accu.max_ontlaad_w)

        doel_bereikt = vermogen_w <= 0
        actief["actie"] = "rust" if doel_bereikt else actie
        actief["geplande_actie"] = actie
        actief["vermogen_w"] = vermogen_w
        actief["verwacht_vermogen_w"] = rond_vermogen_omhoog(
            verwacht_vermogen_w,
            accu.max_laad_w * derating if actie == "laden" else accu.max_ontlaad_w,
        )
        actief["actief_slot_begin_kwh"] = round(begin_kwh, 3)
        actief["actuele_soc_kwh"] = round(actuele_soc_kwh, 3)
        actief["doel_soc_kwh"] = round(target_kwh, 3)
        actief["doel_bereikt"] = doel_bereikt

    def _verhoog_actief_laadslot_doel(
        self,
        schema: list[dict],
        actief_index: int,
        basis_doel_kwh: float,
        actuele_soc_kwh: float,
        accu: "Accustatus",
        resterend_h: float,
    ) -> float:
        """
        Verhoogt het actieve laadslotdoel als latere laadslots niet goedkoper zijn.

        Het actieve slot mag energie uit latere aaneengesloten laadslots naar
        voren halen wanneer die latere slots dezelfde of een hogere prijs hebben.
        Een goedkoper volgend laadslot stopt de verhoging.
        """
        actieve_prijs = self._prijs_ct(schema[actief_index])
        if actieve_prijs is None:
            return basis_doel_kwh

        doel_kwh = basis_doel_kwh
        for volgend in schema[actief_index + 1:]:
            if volgend.get("actie") != "laden":
                break

            volgende_prijs = self._prijs_ct(volgend)
            if volgende_prijs is None or volgende_prijs < actieve_prijs:
                break

            try:
                doel_kwh = max(doel_kwh, float(volgend["soc_na_kwh"]))
            except (KeyError, TypeError, ValueError):
                break

        maximaal_haalbaar_kwh = (
            actuele_soc_kwh
            + accu.max_laad_w / 1000.0 * resterend_h * accu.eta_laad
        )
        return min(doel_kwh, max(basis_doel_kwh, maximaal_haalbaar_kwh))

    def _prijs_ct(self, slot: dict) -> float | None:
        """Leest prijs_ct als getal uit een schema-slot."""
        try:
            return float(slot["prijs_ct"])
        except (KeyError, TypeError, ValueError):
            return None

    def _haal_actueel_slot_doel_uit_vorige_sensor(self, start: str, end: str) -> dict | None:
        """Leest begin-SoC en target-SoC voor het actieve slot uit de vorige sensorstate."""
        vorige_slots = self.get_state("sensor.dynamisch_handelsstrategie", attribute="slots") or []
        for slot in vorige_slots:
            if str(slot.get("start")) != start or str(slot.get("end")) != end:
                continue
            actie = slot.get("geplande_actie") or slot.get("actie")
            if actie not in ("laden", "ontladen"):
                return None
            try:
                return {
                    "actie": actie,
                    "begin_kwh": float(slot.get("actief_slot_begin_kwh", slot["soc_voor_kwh"])),
                    "target_kwh": float(slot.get("doel_soc_kwh", slot["soc_na_kwh"])),
                }
            except (KeyError, TypeError, ValueError):
                return None
        return None

    def _haal_actief_slot_doel(self, actief: dict, actuele_soc_kwh: float) -> dict:
        """
        Geeft de vaste opdracht voor het actieve slot terug.

        De vorige sensorstate is de bron voor hetzelfde actieve slot. Als er nog
        geen vorige sensorstate is, gebruikt de functie de nieuwe DP-uitkomst en
        sensor.zendure_2400_ac_laadpercentage als begin-SoC.
        """
        start = str(actief["start"])
        end = str(actief["end"])
        actie = actief["actie"]

        vorig = self._haal_actueel_slot_doel_uit_vorige_sensor(start, end)
        if vorig is not None and vorig["actie"] == actie:
            return vorig

        return {
            "actie": actie,
            "begin_kwh": actuele_soc_kwh,
            "target_kwh": float(actief["soc_na_kwh"]),
        }

    def _haal_actuele_soc_kwh_via_laadpercentage(
        self,
        max_kwh: float,
        hw_min_pct: float,
        hw_max_pct: float,
    ) -> float | None:
        """
        Converteert sensor.zendure_2400_ac_laadpercentage naar DP-interne kWh.

        De DP-interne schaal loopt van 0 kWh bij hw_min_pct naar max_kwh bij
        hw_max_pct. De echte Zendure-SoC wordt daarom eerst naar dat venster
        omgerekend en daarna begrensd op 0..max_kwh.
        """
        hw_range = hw_max_pct - hw_min_pct
        if max_kwh <= 0 or hw_range <= 0:
            return None

        try:
            soc_pct = float(self.get_state("sensor.zendure_2400_ac_laadpercentage"))
        except (TypeError, ValueError):
            return None

        venster_pct = (soc_pct - hw_min_pct) / hw_range
        return min(max_kwh, max(0.0, venster_pct * max_kwh))

    def _haal_accustatus(self) -> tuple["Accustatus", float, float]:
        """
        Leest de actuele batterijstatus uit HA en converteert naar interne eenheden.

        DE TWEE ENERGIE-SENSOREN
        ------------------------
        beschikbare_energie (kWh): energie leverbaar naar net vanuit huidige SoC.
            Berekend als: (soc - hw_min)% × totale_cap × η_ontlaad

        benodigde_energie (kWh): energie nodig van net om accu naar hw_max te laden.
            Berekend als: (hw_max - soc)% × totale_cap / η_laad

        Beide zijn neteenheden (al gecorrigeerd voor η). Voor het DP-algoritme
        converteren we naar opgeslagen kWh (batterij-intern):

            stored_current = beschikbare / η     (verwijder de ontlaadkorting)
            stored_ruimte  = benodigde × η       (verwijder de laadtoeslag)

        RTE: η_laad = η_ontlaad = √(RTE/100). Zie strategie_dp.py voor uitleg.

        Geeft ook hw_min_pct en hw_max_pct terug zodat de DP-uitvoer vertaald
        kan worden naar echte battery-SoC% (0–100 % van totale capaciteit).
        """
        beschikbaar_kwh = float(self.get_state("sensor.zendure_2400_ac_indicatie_beschikbare_energie") or 0)
        benodigde_kwh   = float(self.get_state("sensor.zendure_2400_ac_indicatie_benodigde_energie")   or 0)
        rte_pct         = float(self.get_state("sensor.zendure_2400_ac_rte_totaal")                    or 90)
        max_laad_w      = float(self.get_state("input_number.zendure_2400_ac_max_oplaadvermogen")       or 2400)
        max_ontlaad_w   = float(self.get_state("input_number.zendure_2400_ac_max_ontlaadvermogen")      or 2400)
        hw_min_pct      = float(self.get_state("sensor.zendure_2400_ac_minimale_laadpercentage")        or 0)
        hw_max_pct      = float(self.get_state("sensor.zendure_2400_ac_maximale_laadpercentage")        or 100)

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
        ), hw_min_pct, hw_max_pct

    def _haal_minimale_spread(self) -> float:
        """
        Leest de gebruikersingestelde minimale spread (ct/kWh).
        Voorkomt handel bij kleine prijsverschillen die weliswaar theoretisch
        winstgevend zijn maar in de praktijk onzeker zijn.
        """
        return float(self.get_state("input_number.dynamisch_minimale_spread") or 0)
