"""
Dynamisch Handelen - Home Assistant integratie (AppDaemon)
==========================================================

AppDaemon app die dagelijks om 14:35 en op verzoek de optimale
laad/ontlaad strategie berekent en publiceert als sensor.dynamisch_handelsstrategie.

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
  input_number.dynamisch_warmte_penalty_laden_factor     gewicht voor warmteverlies bij laden
  input_number.dynamisch_warmte_penalty_ontladen_factor  gewicht voor warmteverlies bij ontladen
  input_number.dynamisch_warmte_afkoeling_halveringstijd_uren  afkoeling richting buitentemperatuur
  input_number.dynamisch_warmte_stijging_laden_c_per_c2h  packtemperatuurstijging door C² × uur laden
  input_number.dynamisch_warmte_stijging_ontladen_c_per_c2h  packtemperatuurstijging door C² × uur ontladen
  input_number.dynamisch_max_temp_boven_80_soc            packtemperatuurlimiet boven 80% SoC
  input_number.dynamisch_max_temp_onder_80_soc            packtemperatuurlimiet onder 80% SoC
  input_number.dynamisch_temp_penalty_factor              gewicht voor temperatuur-overschrijding
  input_number.dynamisch_temp_penalty_100_soc_factor      extra overtemp-gewicht bij 100% SoC
  input_number.dynamisch_hoge_soc_verblijf_penalty_factor verblijfskosten boven 90% SoC
  input_number.dynamisch_lage_soc_verblijf_penalty_factor verblijfskosten onder 10% SoC
  input_number.dynamisch_standby_verbruik_w               standbyverbruik bij niet-laden (W)
  input_text.dynamisch_buitentemperatuur_sensor           optionele sensor met actuele buitentemperatuur
  input_text.dynamisch_weather_entity                     optionele weather entity voor forecast
  input_button.dynamisch_handelsstrategie_herberekenen   knop voor handmatige herberekening
  input_button.dynamisch_strategie_advies_herberekenen   knop voor handmatige adviesanalyse
  input_number.dynamisch_advies_analyse_dagen            aantal dagen historie voor advies
  input_boolean.dynamisch_handelsstrategie_berekening_bezig  laadstatus voor dashboardknop

Uitgang:
  sensor.dynamisch_handelsstrategie   verwachte winst (€) + volledig schema als attribuut
  sensor.dynamisch_strategie_advies   advies over DP- en thermische parameters
  sensor.dynamisch_handelsstrategie_berekening_duur  duur van de laatste bereken-run
"""

import math
from datetime import datetime, timedelta, time
from time import monotonic

import appdaemon.plugins.hass.hassapi as hass

# strategie_dp.py staat in dezelfde apps-map; AppDaemon zet die map op sys.path.
from strategie_dp import (
    Accustatus,
    HOGE_SOC_VERBLIJF_PENALTY_FACTOR,
    LAGE_SOC_VERBLIJF_PENALTY_FACTOR,
    STANDBY_VERBRUIK_W,
    StrategieBerekeningGeannuleerd,
    WARMTE_STIJGING_LADEN_C_PER_C2H,
    WARMTE_STIJGING_ONTLADEN_C_PER_C2H,
    WARMTE_PENALTY_LADEN_FACTOR,
    WARMTE_PENALTY_ONTLADEN_FACTOR,
    TEMP_LIMIET_C,
    TEMP_LIMIET_LAGE_SOC_C,
    TEMP_PENALTY_100_SOC_FACTOR,
    TEMP_PENALTY_FACTOR,
    bereken_derating,
    bereken_laadvermogen_voor_aansturing,
    los_dp_op,
    rond_vermogen_omhoog,
)


GRAFIEK_HISTORIE_UREN = 6.0
FIJNMAZIGE_SLOT_MINUTEN = 15
FIJNMAZIGE_HORIZON_UREN = 3.0


def _lees_slot_datetimes(slot: dict) -> tuple[datetime, datetime] | None:
    """Leest start en end uit een strategieslot als timezone-aware datetimes."""
    try:
        start_raw = slot["start"]
        end_raw = slot["end"]
    except KeyError:
        return None

    def parse(value) -> datetime:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            return dt.astimezone()
        return dt.astimezone()

    try:
        return parse(start_raw), parse(end_raw)
    except (TypeError, ValueError):
        return None


def _lees_datetime_waarde(waarde) -> datetime | None:
    """Leest een datetime-waarde uit HA history of sensorattributen."""
    if isinstance(waarde, datetime):
        return waarde.astimezone()

    if waarde is None:
        return None

    tekst = str(waarde).strip()
    if not tekst:
        return None

    try:
        return datetime.fromisoformat(tekst.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def haal_grafiek_slots_uit_history_items(
    strategie_items: list[dict],
    nu: datetime,
    historie_uren: float = GRAFIEK_HISTORIE_UREN,
) -> list[dict]:
    """
    Leest recente verlopen strategieslots uit recorder-history voor dashboardgrafieken.

    De functie gebruikt alleen `attributes.slots` uit oude sensorstates.
    `attributes.slots_grafiek` wordt genegeerd, zodat oude dashboard-history niet
    opnieuw in zichzelf wordt opgenomen.
    """
    grens = nu.astimezone() - timedelta(hours=historie_uren)
    gekozen: dict[tuple[str, str], tuple[datetime | None, dict]] = {}

    for item in strategie_items:
        attrs = item.get("attributes") or {}
        slots = attrs.get("slots") or []
        if not isinstance(slots, list):
            continue

        item_tijd = _lees_datetime_waarde(
            item.get("last_changed") or item.get("last_updated")
        )

        for slot in slots:
            if not isinstance(slot, dict):
                continue

            datetimes = _lees_slot_datetimes(slot)
            if datetimes is None:
                continue
            start, end = datetimes
            if end <= grens or start >= nu:
                continue
            if item_tijd is not None and item_tijd > end:
                continue

            sleutel = (start.isoformat(), end.isoformat())
            vorige = gekozen.get(sleutel)
            vorige_tijd = vorige[0] if vorige else None
            if vorige is not None:
                if item_tijd is None:
                    continue
                if vorige_tijd is not None and item_tijd <= vorige_tijd:
                    continue

            gekozen[sleutel] = (item_tijd, dict(slot))

    return [
        slot
        for _, (_, slot) in sorted(
            gekozen.items(),
            key=lambda item: item[0][0],
        )
    ]


def bouw_grafiek_slots(
    vorige_slots: list[dict],
    nieuwe_slots: list[dict],
    nu: datetime,
    historie_uren: float = GRAFIEK_HISTORIE_UREN,
) -> list[dict]:
    """
    Combineert recente verlopen slots met de nieuwe strategie voor dashboardgrafieken.

    `nieuwe_slots` blijft de actuele planning voor automatiseringen.
    `bouw_grafiek_slots` bewaart alleen oude slots waarvan `end` binnen het
    historievenster valt.
    """
    grens = nu.astimezone() - timedelta(hours=historie_uren)
    gecombineerd: dict[tuple[str, str], dict] = {}

    for slot in vorige_slots:
        datetimes = _lees_slot_datetimes(slot)
        if datetimes is None:
            continue
        start, end = datetimes
        if end <= grens or start >= nu:
            continue
        gecombineerd[(start.isoformat(), end.isoformat())] = dict(slot)

    for slot in nieuwe_slots:
        datetimes = _lees_slot_datetimes(slot)
        if datetimes is None:
            continue
        start, end = datetimes
        gecombineerd[(start.isoformat(), end.isoformat())] = dict(slot)

    return [
        slot
        for _, slot in sorted(
            gecombineerd.items(),
            key=lambda item: item[0][0],
        )
    ]


class DynamischHandelen(hass.Hass):

    def initialize(self):
        """
        AppDaemon roept initialize() aan bij opstarten en na een reload.
        We registreren hier vier uurlijkse taken en knop/config-triggers.
        """
        self.log("Dynamisch Handelen: gestart, schema wordt elk kwartier herberekend")
        self._berekening_bezig = False
        self._herberekening_gepland = False
        self._laatste_herberekening_kwargs = None
        self._berekening_generatie = 0
        self._zet_berekening_bezig(False)
        self._initialiseer_berekening_duur_sensor()
        self._initialiseer_advies_sensor()
        self.run_hourly(self.bereken_strategie, time(0, 0, 0))
        self.run_hourly(self.bereken_strategie, time(0, 15, 0))
        self.run_hourly(self.bereken_strategie, time(0, 30, 0))
        self.run_hourly(self.bereken_strategie, time(0, 45, 0))
        self.listen_state(
            self._herbereken_op_knop,
            "input_button.dynamisch_handelsstrategie_herberekenen",
        )
        self.listen_state(
            self._herbereken_advies_op_knop,
            "input_button.dynamisch_strategie_advies_herberekenen",
        )
        self.listen_state(
            self._herbereken_advies_op_config,
            "input_number.dynamisch_advies_analyse_dagen",
        )
        self.listen_state(
            self._herbereken_op_config,
            "input_number.dynamisch_warmte_penalty_laden_factor",
        )
        self.listen_state(
            self._herbereken_op_config,
            "input_number.dynamisch_warmte_penalty_ontladen_factor",
        )
        for entity in (
            "input_number.dynamisch_warmte_afkoeling_halveringstijd_uren",
            "input_number.dynamisch_warmte_stijging_laden_c_per_c2h",
            "input_number.dynamisch_warmte_stijging_ontladen_c_per_c2h",
            # Oude helpernaam blijft een trigger voor installaties die de package nog niet hebben bijgewerkt.
            "input_number.dynamisch_warmte_stijging_c_per_c2h",
            "input_number.dynamisch_max_temp_boven_80_soc",
            "input_number.dynamisch_max_temp_onder_80_soc",
            "input_number.dynamisch_temp_penalty_factor",
            "input_number.dynamisch_temp_penalty_100_soc_factor",
            "input_number.dynamisch_hoge_soc_verblijf_penalty_factor",
            "input_number.dynamisch_lage_soc_verblijf_penalty_factor",
            "input_number.dynamisch_standby_verbruik_w",
            "input_text.dynamisch_buitentemperatuur_sensor",
            "input_text.dynamisch_weather_entity",
        ):
            self.listen_state(self._herbereken_op_config, entity)

    # ── HOOFDFUNCTIE ─────────────────────────────────────────────────────────

    def _herbereken_op_knop(self, entity, attribute, old, new, kwargs):
        """Herberekent de strategie na een druk op de HA-knop."""
        self.log("Dynamisch Handelen: handmatige herberekening gestart via HA-knop")
        self.bereken_strategie({"trigger": entity})

    def _herbereken_op_config(self, entity, attribute, old, new, kwargs):
        """Herberekent de strategie wanneer een DP-configuratie wijzigt."""
        self.log(
            f"Dynamisch Handelen: herberekening gestart door wijziging van {entity}",
            level="INFO",
        )
        self.bereken_strategie({"trigger": entity})

    def _herbereken_advies_op_knop(self, entity, attribute, old, new, kwargs):
        """Herberekent het strategie-advies na een druk op de HA-knop."""
        self.log("Dynamisch Handelen: adviesanalyse handmatig gestart via HA-knop")
        self.bereken_strategie_advies({"trigger": entity})

    def _herbereken_advies_op_config(self, entity, attribute, old, new, kwargs):
        """Herberekent het strategie-advies wanneer de adviesconfiguratie wijzigt."""
        self.log(
            f"Dynamisch Handelen: adviesanalyse gestart door wijziging van {entity}",
            level="INFO",
        )
        self.bereken_strategie_advies({"trigger": entity})

    def bereken_strategie(self, kwargs):
        """
        Zet de dashboardknop op loading terwijl _bereken_strategie_impl() rekent.
        Extra triggers tijdens een lopende berekening annuleren die run en
        starten daarna één nieuwe run met de laatste trigger.
        """
        if self._berekening_bezig:
            self._berekening_generatie += 1
            self._herberekening_gepland = True
            self._laatste_herberekening_kwargs = kwargs or {}
            self._zet_berekening_bezig(False)
            self._zet_berekening_bezig(True)
            self.log(
                "Dynamisch Handelen: berekening loopt al; huidige run wordt geannuleerd en opnieuw gestart",
                level="INFO",
            )
            return

        self._berekening_bezig = True
        self._berekening_generatie += 1
        berekening_generatie = self._berekening_generatie
        berekening_start = monotonic()
        berekening_status = "voltooid"
        self._zet_berekening_bezig(True)
        try:
            self._bereken_strategie_impl(kwargs, berekening_generatie)
        except StrategieBerekeningGeannuleerd:
            berekening_status = "geannuleerd"
            self.log(
                "Dynamisch Handelen: strategie berekening geannuleerd door nieuwere input",
                level="INFO",
            )
        except Exception:
            berekening_status = "fout"
            raise
        finally:
            self._publiceer_berekening_duur(
                berekening_start,
                berekening_status,
                kwargs,
                berekening_generatie,
            )
            self._berekening_bezig = False
            self._zet_berekening_bezig(False)
            if self._herberekening_gepland:
                geplande_kwargs = self._laatste_herberekening_kwargs or {
                    "trigger": "geplande_herberekening",
                }
                self._herberekening_gepland = False
                self._laatste_herberekening_kwargs = None
                self.run_in(
                    self._voer_geplande_herberekening_uit,
                    1,
                    geplande_kwargs=geplande_kwargs,
                )

    def _initialiseer_berekening_duur_sensor(self) -> None:
        """Maakt de duur-sensor direct aan zodat het dashboard geen entity-melding toont."""
        if self.get_state("sensor.dynamisch_handelsstrategie_berekening_duur") is not None:
            return

        try:
            self.set_state(
                "sensor.dynamisch_handelsstrategie_berekening_duur",
                state=0.0,
                attributes={
                    "unit_of_measurement": "s",
                    "device_class": "duration",
                    "state_class": "measurement",
                    "friendly_name": "Dynamisch Strategie Berekening Duur",
                    "icon": "mdi:timer-outline",
                    "status": "nog_niet_berekend",
                    "trigger": None,
                    "generatie": self._berekening_generatie,
                    "laatst_bijgewerkt": datetime.now().astimezone().isoformat(),
                },
            )
        except Exception as exc:
            self.log(
                f"Dynamisch Handelen: kon berekening-duur sensor niet initialiseren: {exc}",
                level="DEBUG",
            )

    def _publiceer_berekening_duur(
        self,
        start_monotonic: float,
        status: str,
        kwargs: dict | None,
        berekening_generatie: int,
    ) -> None:
        """Publiceert hoe lang de laatste strategie-run duurde."""
        duur_s = max(0.0, monotonic() - start_monotonic)
        trigger = kwargs.get("trigger") if isinstance(kwargs, dict) else None
        try:
            self.set_state(
                "sensor.dynamisch_handelsstrategie_berekening_duur",
                state=round(duur_s, 2),
                attributes={
                    "unit_of_measurement": "s",
                    "device_class": "duration",
                    "state_class": "measurement",
                    "friendly_name": "Dynamisch Strategie Berekening Duur",
                    "icon": "mdi:timer-outline",
                    "status": status,
                    "trigger": trigger,
                    "generatie": berekening_generatie,
                    "laatst_bijgewerkt": datetime.now().astimezone().isoformat(),
                },
            )
        except Exception as exc:
            self.log(
                f"Dynamisch Handelen: kon berekening-duur sensor niet bijwerken: {exc}",
                level="DEBUG",
            )

    def _initialiseer_advies_sensor(self) -> None:
        """Maakt de advies-sensor direct aan zodat het dashboard geen entity-melding toont."""
        if self.get_state("sensor.dynamisch_strategie_advies") is not None:
            return

        self._publiceer_advies_sensor(
            "nog_niet_berekend",
            {
                "advies_tekst": "Adviesanalyse is nog niet uitgevoerd.",
                "adviesregels": [],
                "analyse_dagen": self._haal_advies_analyse_dagen(),
                "confidence": "onbekend",
                "history_beschikbaar": False,
                "laatst_bijgewerkt": datetime.now().astimezone().isoformat(),
            },
        )

    def bereken_strategie_advies(self, kwargs=None) -> None:
        """
        Analyseert recente strategie-runs en publiceert voorzichtig parameteradvies.

        De analyse gebruikt recorder-history als die via AppDaemon beschikbaar is.
        Zonder voldoende historische strategie-slots of temperatuurmetingen geeft
        de sensor expliciet lage betrouwbaarheid in plaats van schijnzekerheid.
        """
        dagen = self._haal_advies_analyse_dagen()
        trigger = kwargs.get("trigger") if isinstance(kwargs, dict) else None
        self.log(
            f"Dynamisch Handelen: adviesanalyse gestart over {dagen} dagen",
            level="INFO",
        )

        try:
            strategie_items = self._haal_history_items(
                "sensor.dynamisch_handelsstrategie",
                dagen,
                volledige_attributen=True,
            )
            temp_items = self._haal_history_items(
                "sensor.zendure_2400_ac_warmste_batterij_temperatuur",
                dagen,
            )
        except Exception as exc:
            self.log(
                f"Dynamisch Handelen: adviesanalyse kon history niet lezen: {exc}",
                level="WARNING",
            )
            self._publiceer_advies_sensor(
                "history_onbeschikbaar",
                {
                    "advies_tekst": "Recorder-history is niet beschikbaar voor de adviesanalyse.",
                    "adviesregels": ["Controleer of AppDaemon history mag lezen en of recorder actief is."],
                    "analyse_dagen": dagen,
                    "confidence": "laag",
                    "history_beschikbaar": False,
                    "trigger": trigger,
                    "laatst_bijgewerkt": datetime.now().astimezone().isoformat(),
                },
            )
            return

        slots_uit_history = self._haal_geanalyseerde_strategie_slots(strategie_items, dagen)
        slots_uit_huidige_sensor = self._haal_huidige_geanalyseerde_strategie_slots(dagen)
        slots = self._combineer_geanalyseerde_slots(
            slots_uit_history,
            slots_uit_huidige_sensor,
        )
        temp_samples = self._haal_temp_samples(temp_items)
        advies = self._bouw_strategie_advies(slots, temp_samples, dagen)
        advies["strategie_history_items"] = len(strategie_items)
        advies["strategie_history_items_met_slots"] = self._tel_history_items_met_slots(
            strategie_items
        )
        advies["strategie_slots_uit_history"] = len(slots_uit_history)
        advies["strategie_slots_uit_huidige_sensor"] = len(slots_uit_huidige_sensor)
        advies["temperatuur_samples"] = len(temp_samples)
        advies["trigger"] = trigger
        self._publiceer_advies_sensor(advies.pop("state"), advies)

    def _publiceer_advies_sensor(self, state: str, attributes: dict) -> None:
        """Publiceert de strategie-advies sensor."""
        basis = {
            "friendly_name": "Dynamisch Strategie Advies",
            "icon": "mdi:lightbulb-on-outline",
        }
        basis.update(attributes)
        try:
            self.set_state(
                "sensor.dynamisch_strategie_advies",
                state=state,
                attributes=basis,
            )
        except Exception as exc:
            self.log(
                f"Dynamisch Handelen: kon advies-sensor niet bijwerken: {exc}",
                level="DEBUG",
            )

    def _haal_history_items(
        self,
        entity_id: str,
        dagen: int | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        volledige_attributen: bool = False,
    ) -> list[dict]:
        """Leest HA history en normaliseert AppDaemon's verschillende return-vormen."""
        kwargs = {
            "entity_id": entity_id,
            "significant_changes_only": False,
        }
        if volledige_attributen:
            kwargs["minimal_response"] = False
            kwargs["no_attributes"] = False
        if dagen is not None:
            kwargs["days"] = dagen
        if start_time is not None:
            kwargs["start_time"] = start_time
        if end_time is not None:
            kwargs["end_time"] = end_time

        try:
            history = self.get_history(**kwargs) or []
        except TypeError:
            compat_kwargs = {
                key: value
                for key, value in kwargs.items()
                if key not in (
                    "minimal_response",
                    "no_attributes",
                    "significant_changes_only",
                )
            }
            try:
                history = self.get_history(**compat_kwargs) or []
            except TypeError:
                if start_time is not None or end_time is not None:
                    try:
                        history = self.get_history(
                            entity_id,
                            start_time=start_time,
                            end_time=end_time,
                        ) or []
                    except TypeError:
                        history = self.get_history(entity_id, days=dagen or 1) or []
                else:
                    history = self.get_history(entity_id, days=dagen or 1) or []
        return self._flatten_history_items(history)

    def _haal_huidige_strategie_attributen(self) -> dict:
        """Leest de actuele strategie-attributen uit HA, met losse attribuut-fallbacks."""
        alle_state = (
            self.get_state("sensor.dynamisch_handelsstrategie", attribute="all") or {}
        )
        if isinstance(alle_state, dict):
            attributes = alle_state.get("attributes")
            if isinstance(attributes, dict):
                return attributes

        attributes = {}
        for attribuut in ("slots_grafiek", "slots"):
            waarde = self.get_state(
                "sensor.dynamisch_handelsstrategie",
                attribute=attribuut,
            )
            if waarde is not None:
                attributes[attribuut] = waarde
        return attributes

    def _haal_huidige_geanalyseerde_strategie_slots(self, dagen: int) -> list[dict]:
        """
        Leest verlopen slots uit de actuele strategie-sensor.

        HA recorder-history kan compacte states zonder `attributes.slots` teruggeven.
        De actuele sensor bewaart `slots_grafiek`, waarin recente verlopen slots staan.
        """
        attributes = self._haal_huidige_strategie_attributen()
        history_items = []
        for attribuut in ("slots_grafiek", "slots"):
            slots = attributes.get(attribuut)
            if isinstance(slots, list) and slots:
                history_items.append({"attributes": {"slots": slots}})

        if not history_items:
            return []

        return self._haal_geanalyseerde_strategie_slots(history_items, dagen)

    def _combineer_geanalyseerde_slots(self, *sloten_lijsten: list[dict]) -> list[dict]:
        """Combineert slotlijsten op start- en eindtijd zonder dubbele slots."""
        gekozen: dict[tuple[str, str], dict] = {}
        for slots in sloten_lijsten:
            for slot in slots:
                start = slot.get("_start_dt") or self._parse_datetime(slot.get("start"))
                end = slot.get("_end_dt") or self._parse_datetime(slot.get("end"))
                if start is None or end is None:
                    continue
                kopie = dict(slot)
                kopie["_start_dt"] = start
                kopie["_end_dt"] = end
                gekozen[(start.isoformat(), end.isoformat())] = kopie

        return sorted(gekozen.values(), key=lambda s: s["_start_dt"])

    def _tel_history_items_met_slots(self, items: list[dict]) -> int:
        """Telt history-items met een bruikbaar `attributes.slots` attribuut."""
        aantal = 0
        for item in items:
            attrs = item.get("attributes") or {}
            if isinstance(attrs.get("slots"), list):
                aantal += 1
        return aantal

    def _historische_float_state(
        self,
        entity_id: str,
        tijd: datetime | None,
        default: float | None = None,
    ) -> tuple[float | None, str]:
        """Leest de laatst bekende numerieke state op of voor `tijd`."""
        if tijd is None:
            waarde = self._float_state(entity_id)
            return (waarde if waarde is not None else default), "huidig"

        start_time = tijd - timedelta(hours=6)
        end_time = tijd + timedelta(minutes=1)
        items = self._haal_history_items(entity_id, start_time=start_time, end_time=end_time)

        beste_voor = None
        beste_voor_tijd = None
        eerste_na = None
        eerste_na_tijd = None

        for item in items:
            try:
                waarde = float(item.get("state"))
            except (TypeError, ValueError):
                continue

            item_tijd = (
                self._parse_datetime(item.get("last_changed"))
                or self._parse_datetime(item.get("last_updated"))
            )
            if item_tijd is None:
                continue

            if item_tijd <= tijd:
                if beste_voor_tijd is None or item_tijd > beste_voor_tijd:
                    beste_voor = waarde
                    beste_voor_tijd = item_tijd
                continue

            if eerste_na_tijd is None or item_tijd < eerste_na_tijd:
                eerste_na = waarde
                eerste_na_tijd = item_tijd

        if beste_voor is not None:
            return beste_voor, "history"

        if eerste_na is not None and eerste_na_tijd is not None:
            if (eerste_na_tijd - tijd).total_seconds() <= 120:
                return eerste_na, "history_na_start"

        waarde = self._float_state(entity_id)
        if waarde is not None:
            return waarde, "huidig_wegens_history_onbeschikbaar"
        return default, "default_wegens_history_onbeschikbaar"

    def _flatten_history_items(self, value) -> list[dict]:
        if isinstance(value, dict):
            if "state" in value or "attributes" in value:
                return [value]

            items: list[dict] = []
            for nested in value.values():
                items.extend(self._flatten_history_items(nested))
            return items

        if isinstance(value, list):
            items: list[dict] = []
            for nested in value:
                items.extend(self._flatten_history_items(nested))
            return items

        return []

    def _parse_datetime(self, waarde) -> datetime | None:
        return _lees_datetime_waarde(waarde)

    def _haal_geanalyseerde_strategie_slots(
        self,
        strategie_items: list[dict],
        dagen: int,
    ) -> list[dict]:
        """Pakt unieke, inmiddels verstreken strategieslots uit recente sensor-history."""
        nu = datetime.now().astimezone()
        sinds = nu - timedelta(days=dagen)
        gekozen: dict[tuple[str, str], dict] = {}

        for item in strategie_items:
            attrs = item.get("attributes") or {}
            item_tijd = self._parse_datetime(
                item.get("last_changed") or item.get("last_updated")
            )
            slots = attrs.get("slots") or []
            if not isinstance(slots, list):
                continue

            for slot in slots:
                if not isinstance(slot, dict):
                    continue

                start = self._parse_datetime(slot.get("start"))
                end = self._parse_datetime(slot.get("end"))
                if start is None or end is None or end < sinds or end > nu:
                    continue
                if item_tijd is not None and item_tijd > end:
                    continue

                sleutel = (start.isoformat(), end.isoformat())
                vorige = gekozen.get(sleutel)
                vorige_tijd = vorige.get("_item_tijd") if vorige else None
                if vorige is not None and item_tijd is not None and vorige_tijd is not None and item_tijd <= vorige_tijd:
                    continue

                kopie = dict(slot)
                kopie["_start_dt"] = start
                kopie["_end_dt"] = end
                kopie["_item_tijd"] = item_tijd
                gekozen[sleutel] = kopie

        return sorted(gekozen.values(), key=lambda s: s["_start_dt"])

    def _haal_temp_samples(self, temp_items: list[dict]) -> list[tuple[datetime, float]]:
        samples: list[tuple[datetime, float]] = []
        for item in temp_items:
            tijd = self._parse_datetime(item.get("last_changed") or item.get("last_updated"))
            if tijd is None:
                continue
            try:
                waarde = float(item.get("state"))
            except (TypeError, ValueError):
                continue
            samples.append((tijd, waarde))
        return sorted(samples, key=lambda item: item[0])

    def _temp_rond_tijd(
        self,
        samples: list[tuple[datetime, float]],
        tijd: datetime,
        marge_voor: timedelta = timedelta(minutes=45),
        marge_na: timedelta = timedelta(minutes=10),
    ) -> float | None:
        beste: tuple[datetime, float] | None = None
        for sample_tijd, waarde in samples:
            if sample_tijd > tijd + marge_na:
                break
            if sample_tijd >= tijd - marge_voor:
                beste = (sample_tijd, waarde)
        return beste[1] if beste is not None else None

    def _mediaan(self, waarden: list[float]) -> float | None:
        if not waarden:
            return None

        gesorteerd = sorted(waarden)
        midden = len(gesorteerd) // 2
        if len(gesorteerd) % 2:
            return gesorteerd[midden]
        return (gesorteerd[midden - 1] + gesorteerd[midden]) / 2.0

    def _gemiddelde(self, waarden: list[float]) -> float:
        return sum(waarden) / len(waarden) if waarden else 0.0

    def _pas_factor_aan(self, huidig: float, mediaan_fout_c: float | None) -> float:
        if mediaan_fout_c is None or abs(mediaan_fout_c) < 1.0:
            return round(huidig, 2)

        stap = min(0.35, max(-0.35, mediaan_fout_c * 0.08))
        return round(max(0.0, huidig * (1.0 + stap)), 2)

    def _bouw_strategie_advies(
        self,
        slots: list[dict],
        temp_samples: list[tuple[datetime, float]],
        dagen: int,
    ) -> dict:
        """Maakt advies uit historische strategie-slots en gemeten packtemperaturen."""
        huidig_laden = self._haal_warmte_penalty_laden_factor()
        huidig_ontladen = self._haal_warmte_penalty_ontladen_factor()
        huidig_temp_penalty = self._haal_float_met_default(
            "input_number.dynamisch_temp_penalty_factor",
            TEMP_PENALTY_FACTOR,
            minimum=0.0,
        )
        huidig_stijging_laden = self._haal_warmte_stijging_factor(
            "input_number.dynamisch_warmte_stijging_laden_c_per_c2h",
            WARMTE_STIJGING_LADEN_C_PER_C2H,
        )
        huidig_stijging_ontladen = self._haal_warmte_stijging_factor(
            "input_number.dynamisch_warmte_stijging_ontladen_c_per_c2h",
            WARMTE_STIJGING_ONTLADEN_C_PER_C2H,
        )
        huidig_halvering = self._haal_float_met_default(
            "input_number.dynamisch_warmte_afkoeling_halveringstijd_uren",
            2.0,
            minimum=0.05,
        )

        laad_slots = [s for s in slots if s.get("actie") == "laden"]
        ontlaad_slots = [s for s in slots if s.get("actie") == "ontladen"]
        rust_slots = [s for s in slots if s.get("actie") == "rust"]
        actie_slots = laad_slots + ontlaad_slots
        overtemp_slots = [
            s for s in slots
            if float(s.get("overtemp_penalty_eur") or s.get("temp_penalty_eur") or 0.0) > 0
        ]

        c_laden = [float(s.get("c_waarde") or 0.0) for s in laad_slots]
        c_ontladen = [float(s.get("c_waarde") or 0.0) for s in ontlaad_slots]
        warmte_laden_ct = [
            float(s.get("warmte_penalty_eur") or 0.0) * 100.0 for s in laad_slots
        ]
        warmte_ontladen_ct = [
            float(s.get("warmte_penalty_eur") or 0.0) * 100.0 for s in ontlaad_slots
        ]
        overtemp_ct = [
            float(s.get("overtemp_penalty_eur") or s.get("temp_penalty_eur") or 0.0) * 100.0
            for s in slots
        ]

        fouten_laden: list[float] = []
        fouten_ontladen: list[float] = []
        fouten_rust: list[float] = []
        for slot in slots:
            voorspeld = slot.get("batterij_temp_na_c")
            if voorspeld is None:
                continue

            gemeten = self._temp_rond_tijd(temp_samples, slot["_end_dt"])
            if gemeten is None:
                continue

            fout = gemeten - float(voorspeld)
            if slot.get("actie") == "laden":
                fouten_laden.append(fout)
            elif slot.get("actie") == "ontladen":
                fouten_ontladen.append(fout)
            else:
                fouten_rust.append(fout)

        mediaan_fout_laden = self._mediaan(fouten_laden)
        mediaan_fout_ontladen = self._mediaan(fouten_ontladen)
        mediaan_fout_rust = self._mediaan(fouten_rust)

        aanbevolen_stijging_laden = self._pas_factor_aan(huidig_stijging_laden, mediaan_fout_laden)
        aanbevolen_stijging_ontladen = self._pas_factor_aan(huidig_stijging_ontladen, mediaan_fout_ontladen)
        aanbevolen_halvering = round(huidig_halvering, 2)
        if mediaan_fout_rust is not None and abs(mediaan_fout_rust) >= 1.0:
            richting = 1.0 + min(0.35, max(-0.35, mediaan_fout_rust * 0.08))
            aanbevolen_halvering = round(max(0.25, huidig_halvering * richting), 2)

        overtemp_ratio = len(overtemp_slots) / len(slots) if slots else 0.0
        aanbevolen_temp_penalty = huidig_temp_penalty
        if overtemp_ratio >= 0.08:
            aanbevolen_temp_penalty = max(0.05, huidig_temp_penalty * 1.25)
        elif len(slots) >= 20 and overtemp_ratio == 0.0 and self._gemiddelde(overtemp_ct) == 0.0:
            aanbevolen_temp_penalty = huidig_temp_penalty
        aanbevolen_temp_penalty = round(aanbevolen_temp_penalty, 3)

        aanbevolen_laden = round(huidig_laden, 2)
        if c_laden and self._gemiddelde(c_laden) >= 0.45 and self._gemiddelde(warmte_laden_ct) < 1.0:
            aanbevolen_laden = round(min(10.0, max(0.1, huidig_laden * 1.15)), 2)

        aanbevolen_ontladen = round(huidig_ontladen, 2)
        if c_ontladen and self._gemiddelde(c_ontladen) >= 0.45 and self._gemiddelde(warmte_ontladen_ct) < 1.0:
            aanbevolen_ontladen = round(min(10.0, max(0.1, huidig_ontladen * 1.15)), 2)

        regels: list[str] = []
        confidence = "laag"
        state = "te_weinig_data"
        temp_vergelijkingen = len(fouten_laden) + len(fouten_ontladen) + len(fouten_rust)

        if len(slots) < 8:
            regels.append(
                f"Nog weinig strategieslots gevonden ({len(slots)}). Laat de analyse enkele dagen meelopen."
            )
        elif temp_vergelijkingen < 5:
            regels.append(
                f"{len(slots)} strategieslots gevonden, maar slechts {temp_vergelijkingen} bruikbare temperatuurvergelijkingen."
            )
            state = "meer_temperatuurdata_nodig"
        else:
            confidence = "middel" if temp_vergelijkingen < 20 else "hoog"
            state = "stabiel"

        if mediaan_fout_laden is not None and abs(mediaan_fout_laden) >= 1.0:
            state = "warmte_model_afwijking"
            richting = "hoger" if mediaan_fout_laden > 0 else "lager"
            regels.append(
                f"Laden eindigde mediaan {mediaan_fout_laden:+.1f} °C t.o.v. voorspelling; zet warmte stijging laden waarschijnlijk {richting}."
            )

        if mediaan_fout_ontladen is not None and abs(mediaan_fout_ontladen) >= 1.0:
            state = "warmte_model_afwijking"
            richting = "hoger" if mediaan_fout_ontladen > 0 else "lager"
            regels.append(
                f"Ontladen eindigde mediaan {mediaan_fout_ontladen:+.1f} °C t.o.v. voorspelling; zet warmte stijging ontladen waarschijnlijk {richting}."
            )

        if mediaan_fout_rust is not None and abs(mediaan_fout_rust) >= 1.0:
            state = "warmte_model_afwijking"
            richting = "trager" if mediaan_fout_rust > 0 else "sneller"
            regels.append(
                f"Rustslots koelen mediaan {mediaan_fout_rust:+.1f} °C t.o.v. voorspelling; afkoeling lijkt {richting}."
            )

        if overtemp_ratio >= 0.08:
            state = "check_temperatuurlimieten"
            regels.append(
                f"Overtemp-penalty kwam voor in {len(overtemp_slots)} van {len(slots)} slots; verhoog de penalty of verlaag vermogen/temperatuurlimiet."
            )

        if actie_slots and self._gemiddelde([float(s.get("c_waarde") or 0.0) for s in actie_slots]) >= 0.45:
            regels.append("Gemiddelde C-waarde is hoog; C-waarde penalty's zijn de moeite om actief te houden.")

        if not regels:
            regels.append("Geen duidelijke afwijking gevonden. Huidige factoren lijken voorlopig passend.")

        return {
            "state": state,
            "advies_tekst": " ".join(regels),
            "adviesregels": regels,
            "analyse_dagen": dagen,
            "geanalyseerde_slots": len(slots),
            "laad_slots": len(laad_slots),
            "ontlaad_slots": len(ontlaad_slots),
            "rust_slots": len(rust_slots),
            "temperatuur_vergelijkingen": temp_vergelijkingen,
            "confidence": confidence,
            "history_beschikbaar": True,
            "overtemp_slots": len(overtemp_slots),
            "overtemp_slots_ratio": round(overtemp_ratio, 3),
            "gemiddelde_overtemp_penalty_ct": round(self._gemiddelde(overtemp_ct), 3),
            "gemiddelde_warmte_penalty_laden_ct": round(self._gemiddelde(warmte_laden_ct), 3),
            "gemiddelde_warmte_penalty_ontladen_ct": round(self._gemiddelde(warmte_ontladen_ct), 3),
            "gemiddelde_c_laden": round(self._gemiddelde(c_laden), 3),
            "gemiddelde_c_ontladen": round(self._gemiddelde(c_ontladen), 3),
            "mediaan_temp_fout_laden_c": round(mediaan_fout_laden, 2) if mediaan_fout_laden is not None else None,
            "mediaan_temp_fout_ontladen_c": round(mediaan_fout_ontladen, 2) if mediaan_fout_ontladen is not None else None,
            "mediaan_temp_fout_rust_c": round(mediaan_fout_rust, 2) if mediaan_fout_rust is not None else None,
            "aanbevolen_warmte_stijging_laden_c_per_c2h": aanbevolen_stijging_laden,
            "aanbevolen_warmte_stijging_ontladen_c_per_c2h": aanbevolen_stijging_ontladen,
            "aanbevolen_afkoeling_halveringstijd_h": aanbevolen_halvering,
            "aanbevolen_temp_penalty_factor": aanbevolen_temp_penalty,
            "aanbevolen_warmte_penalty_laden_factor": aanbevolen_laden,
            "aanbevolen_warmte_penalty_ontladen_factor": aanbevolen_ontladen,
            "laatst_bijgewerkt": datetime.now().astimezone().isoformat(),
        }

    def _voer_geplande_herberekening_uit(self, kwargs):
        """Voert één extra herberekening uit na triggers tijdens een lopende run."""
        geplande_kwargs = kwargs.get("geplande_kwargs") if isinstance(kwargs, dict) else None
        self.bereken_strategie(geplande_kwargs or {"trigger": "geplande_herberekening"})

    def _zet_berekening_bezig(self, bezig: bool) -> None:
        """Stuurt de dashboard-helper voor de loading state van de refreshknop."""
        entity_id = "input_boolean.dynamisch_handelsstrategie_berekening_bezig"
        state = "on" if bezig else "off"
        service = "input_boolean/turn_on" if bezig else "input_boolean/turn_off"
        try:
            self.call_service(
                service,
                entity_id=entity_id,
            )
        except Exception as exc:
            self.log(
                f"Dynamisch Handelen: kon loading-helper niet bijwerken: {exc}",
                level="DEBUG",
            )
        try:
            self.set_state(
                entity_id,
                state=state,
                attributes={
                    "friendly_name": "Dynamisch Strategie Berekening Bezig",
                    "icon": "mdi:loading",
                },
            )
        except Exception as exc:
            self.log(
                f"Dynamisch Handelen: kon loading-helper state niet bijwerken: {exc}",
                level="DEBUG",
            )

    def _bereken_strategie_impl(self, kwargs, berekening_generatie: int):
        """
        Haalt data op, berekent de strategie en publiceert het resultaat.
        Fouten worden gelogd maar laten AppDaemon verder draaien.
        """
        self.log("Dynamisch Handelen: strategie berekening gestart", level="DEBUG")

        def is_geannuleerd() -> bool:
            return berekening_generatie != self._berekening_generatie

        def stop_als_geannuleerd() -> None:
            if is_geannuleerd():
                raise StrategieBerekeningGeannuleerd()

        try:
            slots = self._haal_prijsslots()
        except Exception as exc:
            self.log(f"Dynamisch Handelen: fout bij ophalen prijsslots: {exc}", level="ERROR")
            return
        stop_als_geannuleerd()

        if not slots:
            self.log("Dynamisch Handelen: geen prijsslots beschikbaar", level="WARNING")
            self.set_state(
                "sensor.dynamisch_handelsstrategie",
                state="geen_data",
                attributes={"slots": [], "verwachte_winst_eur": 0},
            )
            return

        try:
            dp_start_tijd = self._bepaal_dp_start_tijd(slots)
            accu, hw_min_pct, hw_max_pct, accu_bronnen = self._haal_accustatus(dp_start_tijd)
        except Exception as exc:
            self.log(f"Dynamisch Handelen: fout bij ophalen accustatus: {exc}", level="ERROR")
            return
        stop_als_geannuleerd()

        if accu.max_kwh <= 0:
            self.log(
                "Dynamisch Handelen: accucapaciteit onbekend (beschikbaar + benodigde = 0 kWh)",
                level="WARNING",
            )
            return

        min_spread = self._haal_minimale_spread()
        warmte_penalty_laden_factor = self._haal_warmte_penalty_laden_factor()
        warmte_penalty_ontladen_factor = self._haal_warmte_penalty_ontladen_factor()
        standby_verbruik_w = self._haal_standby_verbruik_w()
        plateau_spreiding = self._haal_plateau_spreiding()
        thermisch = self._haal_thermische_config(slots, dp_start_tijd)
        stop_als_geannuleerd()
        fijnmazige_slots = [
            s for s in slots
            if s.get("resolutie") == "fijnmazig_kwartier"
        ]

        self.log(
            f"Dynamisch Handelen: {len(slots)} slots "
            f"({len(fijnmazige_slots)} kwartierslots in eerste {FIJNMAZIGE_HORIZON_UREN:.0f} uur) | "
            f"accu {accu.huidig_kwh:.2f}/{accu.max_kwh:.2f} kWh | "
            f"eta={accu.eta_laad:.3f} | "
            f"laad {accu.max_laad_w:.0f} W / ontlaad {accu.max_ontlaad_w:.0f} W | "
            f"min spread {min_spread:.1f} ct/kWh | "
            f"warmte laden {warmte_penalty_laden_factor:.2f} | "
            f"warmte ontladen {warmte_penalty_ontladen_factor:.2f} | "
            f"standby {standby_verbruik_w:.1f} W | "
            f"plateau {'aan' if plateau_spreiding else 'uit'} | "
            f"packtemp {thermisch['batterij_temp_start_c'] if thermisch['batterij_temp_start_c'] is not None else '-'} °C | "
            f"warmte stijging laden {thermisch['warmte_stijging_laden_c_per_c2h']:.2f} °C/C²h | "
            f"warmte stijging ontladen {thermisch['warmte_stijging_ontladen_c_per_c2h']:.2f} °C/C²h | "
            f"temp limiet hoog/laag {thermisch['temp_limiet_c']:.1f}/{thermisch['temp_limiet_lage_soc_c']:.1f} °C | "
            f"100% SoC temp factor {thermisch['temp_penalty_100_soc_factor']:.2f} | "
            f"SoC verblijf hoog/laag {thermisch['hoge_soc_verblijf_penalty_factor']:.2f}/{thermisch['lage_soc_verblijf_penalty_factor']:.2f} | "
            f"forecast {thermisch['forecast_bron']}",
            level="INFO",
        )

        schema = los_dp_op(
            slots,
            accu,
            min_spread_ct_per_kwh=min_spread,
            plateau_spreiding=plateau_spreiding,
            warmte_penalty_laden_factor=warmte_penalty_laden_factor,
            warmte_penalty_ontladen_factor=warmte_penalty_ontladen_factor,
            standby_verbruik_w=standby_verbruik_w,
            batterij_temp_start_c=thermisch["batterij_temp_start_c"],
            warmte_afkoeling_halveringstijd_h=thermisch["warmte_afkoeling_halveringstijd_h"],
            warmte_stijging_laden_c_per_c2h=thermisch["warmte_stijging_laden_c_per_c2h"],
            warmte_stijging_ontladen_c_per_c2h=thermisch["warmte_stijging_ontladen_c_per_c2h"],
            temp_limiet_c=thermisch["temp_limiet_c"],
            temp_limiet_lage_soc_c=thermisch["temp_limiet_lage_soc_c"],
            temp_penalty_factor=thermisch["temp_penalty_factor"],
            temp_penalty_100_soc_factor=thermisch["temp_penalty_100_soc_factor"],
            hoge_soc_verblijf_penalty_factor=thermisch["hoge_soc_verblijf_penalty_factor"],
            lage_soc_verblijf_penalty_factor=thermisch["lage_soc_verblijf_penalty_factor"],
            soc_min_pct=hw_min_pct,
            soc_max_pct=hw_max_pct,
            annuleer_check=is_geannuleerd,
        )
        stop_als_geannuleerd()
        self._corrigeer_actief_slot_vermogen(schema, accu, hw_min_pct, hw_max_pct)
        stop_als_geannuleerd()
        spread_blokkades = self._markeer_spread_blokkades(schema, accu.eta_laad, min_spread)
        stop_als_geannuleerd()

        # Vertaal DP-interne SoC% (0–100% van hw-venster) naar echte battery-%
        # zodat de grafiek overeenkomt met wat de Zendure rapporteert.
        hw_range = hw_max_pct - hw_min_pct
        for s in schema:
            s["soc_voor_pct"] = round(hw_min_pct + s["soc_voor_pct"] / 100.0 * hw_range, 1)
            s["soc_na_pct"]   = round(hw_min_pct + s["soc_na_pct"]   / 100.0 * hw_range, 1)
            if s.get("stop_soc_pct") is not None:
                try:
                    stop_soc_pct = float(s["stop_soc_pct"])
                except (TypeError, ValueError):
                    s["stop_soc_pct"] = None
                else:
                    s["stop_soc_pct"] = round(hw_min_pct + stop_soc_pct / 100.0 * hw_range, 1)
        stop_als_geannuleerd()

        verwachte_winst = sum(s["winst_eur"] for s in schema)
        laad_slots      = [s for s in schema if s["actie"] == "laden"]
        ontlaad_slots   = [s for s in schema if s["actie"] == "ontladen"]
        volgende        = next((s for s in schema if s["actie"] != "rust"), None)
        nu_publicatie   = datetime.now().astimezone()
        huidig = None
        for slot in schema:
            datetimes = _lees_slot_datetimes(slot)
            if datetimes is None:
                continue
            start, end = datetimes
            if start <= nu_publicatie < end:
                huidig = slot
                break
        history_grafiek_slots: list[dict] = []
        try:
            strategie_history_items = self._haal_history_items(
                "sensor.dynamisch_handelsstrategie",
                1,
            )
            history_grafiek_slots = haal_grafiek_slots_uit_history_items(
                strategie_history_items,
                nu_publicatie,
                GRAFIEK_HISTORIE_UREN,
            )
        except Exception as exc:
            self.log(
                f"Dynamisch Handelen: kon grafiek-history niet lezen: {exc}",
                level="DEBUG",
            )
        vorige_grafiek_slots = (
            self.get_state("sensor.dynamisch_handelsstrategie", attribute="slots_grafiek")
            or self.get_state("sensor.dynamisch_handelsstrategie", attribute="slots")
            or []
        )
        if not isinstance(vorige_grafiek_slots, list):
            vorige_grafiek_slots = []
        grafiek_slots = bouw_grafiek_slots(
            history_grafiek_slots + vorige_grafiek_slots,
            schema,
            nu_publicatie,
            GRAFIEK_HISTORIE_UREN,
        )
        grafiek_start = grafiek_slots[0]["start"] if grafiek_slots else None
        strategie_einde = schema[-1]["end"] if schema else None

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
                "slots_grafiek":       grafiek_slots,
                "grafiek_historie_uren": GRAFIEK_HISTORIE_UREN,
                "grafiek_start":       grafiek_start,
                "strategie_einde":     strategie_einde,
                "planning_resolutie":   "eerste 3 uur per 15 min, daarna bronresolutie",
                "fijnmazige_horizon_h": FIJNMAZIGE_HORIZON_UREN,
                "fijnmazige_slot_minuten": FIJNMAZIGE_SLOT_MINUTEN,
                "fijnmazige_slots":     len(fijnmazige_slots),
                "bron_slots":           len(slots) - len(fijnmazige_slots),
                "laad_slots":          len(laad_slots),
                "ontlaad_slots":       len(ontlaad_slots),
                "spread_blokkades":    spread_blokkades,
                "spread_blokkades_aantal": len(spread_blokkades),
                "huidige_actie":       huidig["actie"] if huidig else "rust",
                "huidige_stop_soc_pct": huidig.get("stop_soc_pct") if huidig else None,
                "huidige_stop_soc_richting": huidig.get("stop_soc_richting") if huidig else "geen",
                "volgende_actie":      volgende["actie"] if volgende else "rust",
                "volgende_start":      volgende["start"] if volgende else None,
                "accu_huidig_kwh":     round(accu.huidig_kwh, 3),
                "accu_max_kwh":        round(accu.max_kwh,    3),
                "dp_start":            dp_start_tijd.isoformat() if dp_start_tijd else None,
                "accu_bronnen":        accu_bronnen,
                "eta":                 round(accu.eta_laad,   3),
                "min_spread_ct":       min_spread,
                "warmte_penalty_laden_factor": warmte_penalty_laden_factor,
                "warmte_penalty_ontladen_factor": warmte_penalty_ontladen_factor,
                "standby_verbruik_w": standby_verbruik_w,
                "batterij_temp_start_c": thermisch["batterij_temp_start_c"],
                "batterij_temp_bron": thermisch["batterij_temp_bron"],
                "buiten_temp_huidig_c": thermisch["buiten_temp_huidig_c"],
                "buiten_temp_bron": thermisch["buiten_temp_bron"],
                "weather_entity": thermisch["weather_entity"],
                "forecast_bron": thermisch["forecast_bron"],
                "forecast_punten": thermisch["forecast_punten"],
                "warmte_afkoeling_halveringstijd_h": thermisch["warmte_afkoeling_halveringstijd_h"],
                "warmte_stijging_laden_c_per_c2h": thermisch["warmte_stijging_laden_c_per_c2h"],
                "warmte_stijging_ontladen_c_per_c2h": thermisch["warmte_stijging_ontladen_c_per_c2h"],
                # Backwards-compatible attribuut voor bestaande dashboards of automations.
                "warmte_stijging_c_per_c2h": thermisch["warmte_stijging_laden_c_per_c2h"],
                "temp_limiet_c": thermisch["temp_limiet_c"],
                "temp_limiet_lage_soc_c": thermisch["temp_limiet_lage_soc_c"],
                "temp_penalty_factor": thermisch["temp_penalty_factor"],
                "temp_penalty_100_soc_factor": thermisch["temp_penalty_100_soc_factor"],
                "hoge_soc_verblijf_penalty_factor": thermisch["hoge_soc_verblijf_penalty_factor"],
                "lage_soc_verblijf_penalty_factor": thermisch["lage_soc_verblijf_penalty_factor"],
                "temp_soc_drempel_pct": 80.0,
                "plateau_spreiding":   plateau_spreiding,
                "bijgewerkt":          nu_publicatie.isoformat(),
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

        Slots die al volledig zijn verstreken worden overgeslagen. De eerste
        drie uur worden opgesplitst in kwartierslots zodat de DP sneller kan
        reageren op temperatuur- en SoC-drempels.
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
        return self._verdeel_eerste_uren_in_kwartierslots(slots, nu)

    @staticmethod
    def _verdeel_eerste_uren_in_kwartierslots(
        slots: list[dict],
        nu: datetime,
        horizon_h: float = FIJNMAZIGE_HORIZON_UREN,
        slot_minuten: int = FIJNMAZIGE_SLOT_MINUTEN,
    ) -> list[dict]:
        """Splitst nabije prijsslots in kwartieren voor thermische sturing."""
        if horizon_h <= 0 or slot_minuten <= 0:
            return slots

        horizon_start = nu.replace(
            minute=(nu.minute // slot_minuten) * slot_minuten,
            second=0,
            microsecond=0,
        )
        horizon = horizon_start + timedelta(hours=horizon_h)
        stap = timedelta(minutes=slot_minuten)
        verdeelde_slots: list[dict] = []

        for slot in slots:
            start = slot["start"]
            end = slot["end"]
            if end <= nu:
                continue

            if start >= horizon:
                nieuw_slot = dict(slot)
                nieuw_slot.setdefault("resolutie", "bron")
                nieuw_slot["duration_h"] = (end - start).total_seconds() / 3600.0
                verdeelde_slots.append(nieuw_slot)
                continue

            deel_start = start
            while deel_start < end and deel_start < horizon:
                deel_end = min(deel_start + stap, end, horizon)
                if deel_end > nu:
                    nieuw_slot = dict(slot)
                    nieuw_slot["start"] = deel_start
                    nieuw_slot["end"] = deel_end
                    nieuw_slot["duration_h"] = (deel_end - deel_start).total_seconds() / 3600.0
                    nieuw_slot["resolutie"] = "fijnmazig_kwartier"
                    verdeelde_slots.append(nieuw_slot)
                deel_start = deel_end

            if deel_start < end:
                nieuw_slot = dict(slot)
                nieuw_slot["start"] = deel_start
                nieuw_slot["end"] = end
                nieuw_slot["duration_h"] = (end - deel_start).total_seconds() / 3600.0
                nieuw_slot["resolutie"] = "bron"
                verdeelde_slots.append(nieuw_slot)

        verdeelde_slots.sort(key=lambda s: s["start"])
        return verdeelde_slots

    def _bepaal_dp_start_tijd(self, slots: list[dict]) -> datetime | None:
        """Gebruikt het begin van het lopende prijsslot als starttijd voor de DP."""
        nu = datetime.now().astimezone()
        for slot in slots:
            try:
                start = datetime.fromisoformat(str(slot["start"])).astimezone()
                end = datetime.fromisoformat(str(slot["end"])).astimezone()
            except (KeyError, ValueError, TypeError):
                continue
            if start <= nu < end:
                return start
        return None

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
        huidige slot gebruikt deze correctie alleen dat DP-target als opdracht.
        Deze functie haalt geen energie uit latere slots naar voren, omdat de DP
        dan thermische penalties en SoC-verblijfskosten niet opnieuw evalueert.
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

        if actief is None or actief.get("actie") not in ("laden", "ontladen"):
            return

        raw_actuele_soc_kwh = self._haal_actuele_soc_kwh_via_laadpercentage(
            accu.max_kwh,
            hw_min_pct,
            hw_max_pct,
        )
        if raw_actuele_soc_kwh is None:
            return

        actie = actief["actie"]
        geplande_vermogen_w = self._begrens_gepland_vermogen(
            actief.get("vermogen_w"),
            accu.max_laad_w if actie == "laden" else accu.max_ontlaad_w,
        )
        volgende_actie = (
            schema[actief_index + 1].get("actie")
            if actief_index is not None and actief_index + 1 < len(schema)
            else "rust"
        )
        try:
            target_kwh = self._begrens_kwh_naar_accu(float(actief["soc_na_kwh"]), accu.max_kwh)
            begin_kwh = float(actief.get("soc_voor_kwh", raw_actuele_soc_kwh))
        except (KeyError, TypeError, ValueError):
            return

        try:
            eindtijd = datetime.fromisoformat(str(actief["end"])).astimezone()
        except (KeyError, ValueError, TypeError):
            return

        resterend_h = max(0.0, (eindtijd - nu).total_seconds() / 3600.0)
        if resterend_h <= 0.0:
            return

        actuele_soc_kwh, soc_bron = self._schat_actuele_soc_kwh(
            actief,
            actie,
            raw_actuele_soc_kwh,
            accu,
            nu,
        )

        if actie == "laden":
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
            target_kwh = self._begrens_kwh_naar_accu(target_kwh, accu.max_kwh)
            delta_kwh = max(0.0, actuele_soc_kwh - target_kwh)
            energie_net_kwh = delta_kwh * accu.eta_ontlaad
            verwacht_vermogen_w = min(
                accu.max_ontlaad_w,
                energie_net_kwh / resterend_h * 1000.0 if resterend_h > 0 else 0.0,
            )
            vermogen_w = rond_vermogen_omhoog(verwacht_vermogen_w, accu.max_ontlaad_w)

        doel_bereikt = vermogen_w <= 0
        doorlopen_zelfde_actie = False
        if doel_bereikt and volgende_actie == actie and geplande_vermogen_w > 0:
            vermogen_w = geplande_vermogen_w
            verwacht_vermogen_w = geplande_vermogen_w
            doel_bereikt = False
            doorlopen_zelfde_actie = True

        actief["actie"] = "rust" if doel_bereikt else actie
        actief["geplande_actie"] = actie
        actief["vermogen_w"] = vermogen_w
        actief["verwacht_vermogen_w"] = rond_vermogen_omhoog(
            verwacht_vermogen_w,
            accu.max_laad_w * derating if actie == "laden" else accu.max_ontlaad_w,
        )
        actief["actief_slot_begin_kwh"] = round(begin_kwh, 3)
        actief["actief_slot_raw_soc_kwh"] = round(raw_actuele_soc_kwh, 3)
        actief["actuele_soc_kwh"] = round(actuele_soc_kwh, 3)
        actief["actuele_soc_bron"] = soc_bron
        actief["actief_slot_doel_kwh"] = round(target_kwh, 3)
        actief["doel_soc_kwh"] = round(target_kwh, 3)
        actief["soc_na_kwh"] = round(target_kwh, 3)
        actief["soc_na_pct"] = round(target_kwh / accu.max_kwh * 100, 1) if accu.max_kwh > 0 else 0.0
        actief["actief_slot_delta_kwh"] = round(
            target_kwh - actuele_soc_kwh if actie == "laden" else actuele_soc_kwh - target_kwh,
            3,
        )
        actief["actief_slot_resterend_h"] = round(resterend_h, 3)
        actief["doel_bereikt"] = doel_bereikt
        actief["actief_slot_doorlopen_zelfde_actie"] = doorlopen_zelfde_actie
        if doel_bereikt:
            actief["stop_soc_kwh"] = None
            actief["stop_soc_pct"] = None
            actief["stop_soc_richting"] = "geen"
        else:
            if volgende_actie == actie:
                actief["stop_soc_kwh"] = None
                actief["stop_soc_pct"] = None
                actief["stop_soc_richting"] = "geen"
            else:
                actief["stop_soc_kwh"] = round(target_kwh, 3)
                actief["stop_soc_pct"] = round(target_kwh / accu.max_kwh * 100, 1) if accu.max_kwh > 0 else 0.0
                actief["stop_soc_richting"] = (
                    "boven_of_gelijk" if actie == "laden" else "onder_of_gelijk"
                )
        if doel_bereikt:
            actief["actief_slot_stopreden"] = "doel_bereikt"

    def _prijs_ct(self, slot: dict) -> float | None:
        """Leest prijs_ct als getal uit een schema-slot."""
        try:
            return float(slot["prijs_ct"])
        except (KeyError, TypeError, ValueError):
            return None

    def _begrens_kwh_naar_accu(self, waarde_kwh: float, max_kwh: float) -> float:
        """Begrenst een DP-interne kWh-waarde op de huidige accugrootte."""
        return min(max(0.0, waarde_kwh), max(0.0, max_kwh))

    def _begrens_gepland_vermogen(self, waarde_w, maximum_w: float) -> int:
        """Leest een gepland vermogen en begrenst het op de huidige hardwarelimiet."""
        try:
            vermogen_w = float(waarde_w)
        except (TypeError, ValueError):
            return 0

        return round(min(max(0.0, vermogen_w), max(0.0, maximum_w)))

    def _schat_actuele_soc_kwh(
        self,
        actief: dict,
        actie: str,
        raw_soc_kwh: float,
        accu: "Accustatus",
        nu: datetime,
    ) -> tuple[float, str]:
        """
        Schat de actuele SoC binnen hetzelfde actieve slot wanneer electricLevel
        nog hetzelfde hele percentage meldt.
        """
        start = str(actief["start"])
        end = str(actief["end"])
        vorige_slots = self.get_state("sensor.dynamisch_handelsstrategie", attribute="slots") or []
        vorige_slot = None
        for slot in vorige_slots:
            if str(slot.get("start")) == start and str(slot.get("end")) == end:
                vorige_slot = slot
                break

        if vorige_slot is None:
            return raw_soc_kwh, "laadpercentage"

        vorige_actie = vorige_slot.get("geplande_actie") or vorige_slot.get("actie")
        if vorige_actie != actie:
            return raw_soc_kwh, "laadpercentage"

        try:
            vorige_raw_soc_kwh = float(vorige_slot["actief_slot_raw_soc_kwh"])
            vorige_soc_kwh = float(vorige_slot["actuele_soc_kwh"])
        except (KeyError, TypeError, ValueError):
            return raw_soc_kwh, "laadpercentage"

        if abs(raw_soc_kwh - vorige_raw_soc_kwh) > 0.001:
            return raw_soc_kwh, "laadpercentage"

        bijgewerkt = self.get_state("sensor.dynamisch_handelsstrategie", attribute="bijgewerkt")
        try:
            vorige_tijd = datetime.fromisoformat(str(bijgewerkt)).astimezone()
        except (TypeError, ValueError):
            return raw_soc_kwh, "laadpercentage"

        verstreken_h = max(0.0, (nu - vorige_tijd).total_seconds() / 3600.0)
        if verstreken_h <= 0.0 or verstreken_h > 5.0 / 60.0:
            return raw_soc_kwh, "laadpercentage"

        try:
            vermogen_w = float(self.get_state("sensor.zendure_2400_ac_vermogen_aansturing") or 0)
        except (TypeError, ValueError):
            return raw_soc_kwh, "laadpercentage"

        geschat_soc_kwh = vorige_soc_kwh
        if actie == "laden" and vermogen_w > 0 and accu.eta_laad > 0:
            geschat_soc_kwh += vermogen_w / 1000.0 * verstreken_h * accu.eta_laad
        elif actie == "ontladen" and vermogen_w < 0 and accu.eta_ontlaad > 0:
            geschat_soc_kwh -= abs(vermogen_w) / 1000.0 * verstreken_h / accu.eta_ontlaad
        else:
            return raw_soc_kwh, "laadpercentage"

        return min(accu.max_kwh, max(0.0, geschat_soc_kwh)), "vermogen_aansturing"

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

    def _haal_accustatus(self, dp_start_tijd: datetime | None = None) -> tuple["Accustatus", float, float, dict]:
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
        beschikbaar_kwh, beschikbaar_bron = self._historische_float_state(
            "sensor.zendure_2400_ac_indicatie_beschikbare_energie",
            dp_start_tijd,
            0.0,
        )
        benodigde_kwh, benodigde_bron = self._historische_float_state(
            "sensor.zendure_2400_ac_indicatie_benodigde_energie",
            dp_start_tijd,
            0.0,
        )
        rte_pct, rte_bron = self._historische_float_state(
            "sensor.zendure_2400_ac_rte_totaal",
            dp_start_tijd,
            90.0,
        )
        max_laad_w, max_laad_bron = self._historische_float_state(
            "input_number.zendure_2400_ac_max_oplaadvermogen",
            dp_start_tijd,
            2400.0,
        )
        max_ontlaad_w, max_ontlaad_bron = self._historische_float_state(
            "input_number.zendure_2400_ac_max_ontlaadvermogen",
            dp_start_tijd,
            2400.0,
        )
        hw_min_pct, hw_min_bron = self._historische_float_state(
            "sensor.zendure_2400_ac_minimale_laadpercentage",
            dp_start_tijd,
            0.0,
        )
        hw_max_pct, hw_max_bron = self._historische_float_state(
            "sensor.zendure_2400_ac_maximale_laadpercentage",
            dp_start_tijd,
            100.0,
        )

        beschikbaar_kwh = float(beschikbaar_kwh or 0.0)
        benodigde_kwh = float(benodigde_kwh or 0.0)
        rte_pct = float(rte_pct or 90.0)
        max_laad_w = float(max_laad_w or 2400.0)
        max_ontlaad_w = float(max_ontlaad_w or 2400.0)
        hw_min_pct = float(hw_min_pct or 0.0)
        hw_max_pct = float(hw_max_pct or 100.0)

        rte_pct = max(50.0, min(100.0, rte_pct))
        eta     = math.sqrt(rte_pct / 100.0)

        stored_current = beschikbaar_kwh / eta
        stored_ruimte  = benodigde_kwh   * eta

        bronnen = {
            "tijd": dp_start_tijd.isoformat() if dp_start_tijd else None,
            "beschikbare_energie": beschikbaar_bron,
            "benodigde_energie": benodigde_bron,
            "rte_totaal": rte_bron,
            "max_laad_w": max_laad_bron,
            "max_ontlaad_w": max_ontlaad_bron,
            "hw_min_pct": hw_min_bron,
            "hw_max_pct": hw_max_bron,
        }

        return Accustatus(
            huidig_kwh    = stored_current,
            max_kwh       = stored_current + stored_ruimte,
            eta_laad      = eta,
            eta_ontlaad   = eta,
            max_laad_w    = max_laad_w,
            max_ontlaad_w = max_ontlaad_w,
        ), hw_min_pct, hw_max_pct, bronnen

    def _haal_minimale_spread(self) -> float:
        """
        Leest de gebruikersingestelde minimale spread (ct/kWh).
        Voorkomt handel bij kleine prijsverschillen die weliswaar theoretisch
        winstgevend zijn maar in de praktijk onzeker zijn.
        """
        return float(self.get_state("input_number.dynamisch_minimale_spread") or 0)

    def _haal_advies_analyse_dagen(self) -> int:
        """
        Leest hoeveel dagen recorder-history de adviesanalyse mag gebruiken.
        Korter dan 30 dagen is prima: de sensor publiceert dan lagere confidence
        als er nog weinig bruikbare slots zijn.
        """
        waarde = self._haal_float_met_default(
            "input_number.dynamisch_advies_analyse_dagen",
            14,
            minimum=1,
        )
        return min(30, max(1, int(round(waarde))))

    def _haal_warmte_penalty_laden_factor(self) -> float:
        """
        Leest hoeveel gewicht de DP aan C-waarde warmteverlies bij laden moet geven.
        """
        waarde = self.get_state("input_number.dynamisch_warmte_penalty_laden_factor")
        if waarde in (None, "unknown", "unavailable"):
            waarde = self.get_state("input_number.dynamisch_warmte_penalty_factor")
        try:
            return max(0.0, float(waarde))
        except (TypeError, ValueError):
            return WARMTE_PENALTY_LADEN_FACTOR

    def _haal_warmte_penalty_ontladen_factor(self) -> float:
        """
        Leest hoeveel gewicht de DP aan C-waarde warmteverlies bij ontladen moet geven.
        """
        waarde = self.get_state("input_number.dynamisch_warmte_penalty_ontladen_factor")
        try:
            return max(0.0, float(waarde))
        except (TypeError, ValueError):
            return WARMTE_PENALTY_ONTLADEN_FACTOR

    def _haal_standby_verbruik_w(self) -> float:
        """Leest het standbyverbruik voor rust- en ontlaadslots in W."""
        return self._haal_float_met_default(
            "input_number.dynamisch_standby_verbruik_w",
            STANDBY_VERBRUIK_W,
            minimum=0.0,
        )

    def _haal_float_met_default(self, entity_id: str, default: float, minimum: float | None = None) -> float:
        """Leest een numerieke HA-helper en gebruikt default bij unknown/unavailable."""
        waarde = self.get_state(entity_id)
        try:
            getal = float(waarde)
        except (TypeError, ValueError):
            getal = default
        if minimum is not None:
            getal = max(minimum, getal)
        return getal

    def _haal_warmte_stijging_factor(self, entity_id: str, default: float) -> float:
        """
        Leest een richting-specifieke packtemperatuurfactor.

        input_number.dynamisch_warmte_stijging_c_per_c2h blijft de fallback voor
        installaties waar de twee nieuwe input_number helpers nog ontbreken.
        """
        waarde = self.get_state(entity_id)
        if waarde in (None, "unknown", "unavailable"):
            waarde = self.get_state("input_number.dynamisch_warmte_stijging_c_per_c2h")
        try:
            return max(0.0, float(waarde))
        except (TypeError, ValueError):
            return default

    def _haal_entity_id_uit_input_text(self, entity_id: str) -> str | None:
        """Leest een entity_id uit input_text en negeert lege helperwaarden."""
        waarde = self.get_state(entity_id)
        if waarde in (None, "unknown", "unavailable"):
            return None
        tekst = str(waarde).strip()
        return tekst or None

    def _float_state(self, entity_id: str) -> float | None:
        """Leest een state als float; unknown/unavailable geeft None."""
        try:
            return float(self.get_state(entity_id))
        except (TypeError, ValueError):
            return None

    def _haal_warmste_batterij_temp_c(self) -> float | None:
        """Neemt de hoogste batterij-packtemperatuur, niet de invertertemperatuur."""
        warmste = self._float_state("sensor.zendure_2400_ac_warmste_batterij_temperatuur")
        if warmste is not None:
            return warmste

        try:
            aantal = int(float(self.get_state("sensor.zendure_2400_ac_aantal_batterijen") or 0))
        except (TypeError, ValueError):
            aantal = 0
        max_index = aantal if aantal > 0 else 6

        waarden = []
        for index in range(1, max_index + 1):
            waarde = self._float_state(f"sensor.zendure_2400_ac_batterij_{index}_temperatuur")
            if waarde is not None:
                waarden.append(waarde)
        return max(waarden) if waarden else None

    def _haal_warmste_batterij_temp_c_op_tijd(self, tijd: datetime | None) -> tuple[float | None, str]:
        """Leest de warmste batterij-packtemperatuur op de DP-starttijd."""
        warmste, warmste_bron = self._historische_float_state(
            "sensor.zendure_2400_ac_warmste_batterij_temperatuur",
            tijd,
        )
        if warmste is not None:
            return warmste, warmste_bron

        try:
            aantal = int(float(self.get_state("sensor.zendure_2400_ac_aantal_batterijen") or 0))
        except (TypeError, ValueError):
            aantal = 0
        max_index = aantal if aantal > 0 else 6

        waarden = []
        bronnen = []
        for index in range(1, max_index + 1):
            waarde, bron = self._historische_float_state(
                f"sensor.zendure_2400_ac_batterij_{index}_temperatuur",
                tijd,
            )
            if waarde is not None:
                waarden.append(waarde)
                bronnen.append(bron)

        if waarden:
            return max(waarden), ",".join(sorted(set(bronnen)))
        return None, "onbekend"

    def _haal_buitentemperatuur_c(self, tijd: datetime | None = None) -> tuple[float | None, str]:
        """Leest buitentemperatuur uit de ingestelde sensor of OpenWeatherMap."""
        sensor_entity = (
            self._haal_entity_id_uit_input_text("input_text.dynamisch_buitentemperatuur_sensor")
            or "sensor.openweathermap_temperature"
        )

        if tijd is not None:
            sensor_temp, sensor_bron = self._historische_float_state(sensor_entity, tijd)
            if sensor_temp is not None:
                return sensor_temp, f"{sensor_entity}:{sensor_bron}"

        sensor_temp = self._float_state(sensor_entity)
        if sensor_temp is not None:
            return sensor_temp, sensor_entity

        weather_entity = self._haal_weather_entity()
        if weather_entity:
            try:
                weather_temp = float(self.get_state(weather_entity, attribute="temperature"))
                return weather_temp, f"{weather_entity}.temperature"
            except (TypeError, ValueError):
                pass

        return None, "onbekend"

    def _haal_weather_entity(self) -> str | None:
        """Leest de weather entity voor forecastdata."""
        ingesteld = self._haal_entity_id_uit_input_text("input_text.dynamisch_weather_entity")
        if ingesteld:
            return ingesteld

        standaard = "weather.openweathermap"
        return standaard if self.get_state(standaard) is not None else None

    def _normaliseer_forecast_punten(self, forecast: list[dict] | None) -> list[tuple[datetime, float]]:
        """Zet HA weather forecast-items om naar gesorteerde (tijd, temperatuur)-punten."""
        punten: list[tuple[datetime, float]] = []
        if not forecast:
            return punten

        for item in forecast:
            try:
                tijd = datetime.fromisoformat(str(item["datetime"])).astimezone()
                temp = float(item["temperature"])
            except (KeyError, TypeError, ValueError):
                continue
            punten.append((tijd, temp))
        punten.sort(key=lambda punt: punt[0])
        return punten

    def _haal_forecast_punten(self, weather_entity: str | None) -> tuple[list[tuple[datetime, float]], str]:
        """Haalt hourly forecast op via weather.get_forecasts of via oudere forecast-attributen."""
        if not weather_entity:
            return [], "geen_weather_entity"

        try:
            response = self.call_service(
                "weather/get_forecasts",
                entity_id=weather_entity,
                type="hourly",
                return_response=True,
            )
        except Exception as exc:
            self.log(
                f"Dynamisch Handelen: forecast service gaf geen resultaat voor {weather_entity}: {exc}",
                level="DEBUG",
            )
            response = None

        kandidaat_mappings = []
        if isinstance(response, dict):
            kandidaat_mappings.append(response)
            result = response.get("result")
            if isinstance(result, dict):
                kandidaat_mappings.append(result)
                response_data = result.get("response")
                if isinstance(response_data, dict):
                    kandidaat_mappings.insert(0, response_data)

        for mapping in kandidaat_mappings:
            payload = mapping.get(weather_entity)
            if payload is None:
                payload = next((waarde for waarde in mapping.values() if isinstance(waarde, dict)), None)
            if not isinstance(payload, dict):
                continue
            punten = self._normaliseer_forecast_punten(payload.get("forecast"))
            if punten:
                return punten, "weather.get_forecasts"

        forecast_attr = self.get_state(weather_entity, attribute="forecast")
        punten = self._normaliseer_forecast_punten(forecast_attr if isinstance(forecast_attr, list) else None)
        if punten:
            return punten, f"{weather_entity}.forecast"

        return [], "forecast_onbeschikbaar"

    def _forecast_temp_voor_slot(
        self,
        punten: list[tuple[datetime, float]],
        start: datetime,
    ) -> float | None:
        """Kiest de laatste forecasttemperatuur op of voor het slot, of anders de eerste erna."""
        if not punten:
            return None

        start = start.astimezone()
        gekozen = punten[0][1]
        for tijd, temp in punten:
            if tijd > start:
                break
            gekozen = temp
        return gekozen

    def _haal_thermische_config(self, slots: list[dict], dp_start_tijd: datetime | None = None) -> dict:
        """Leest thermische HA-config en zet buiten_temp_c op ieder prijsslot."""
        batterij_temp_start_c, batterij_temp_bron = self._haal_warmste_batterij_temp_c_op_tijd(dp_start_tijd)
        buiten_temp_huidig_c, buiten_temp_bron = self._haal_buitentemperatuur_c(dp_start_tijd)
        weather_entity = self._haal_weather_entity()
        forecast_punten, forecast_bron = self._haal_forecast_punten(weather_entity)

        for slot in slots:
            temp = self._forecast_temp_voor_slot(forecast_punten, slot["start"])
            if temp is None:
                temp = buiten_temp_huidig_c
            if temp is not None:
                slot["buiten_temp_c"] = temp

        return {
            "batterij_temp_start_c": batterij_temp_start_c,
            "batterij_temp_bron": batterij_temp_bron,
            "buiten_temp_huidig_c": buiten_temp_huidig_c,
            "buiten_temp_bron": buiten_temp_bron,
            "weather_entity": weather_entity,
            "forecast_bron": forecast_bron,
            "forecast_punten": len(forecast_punten),
            "warmte_afkoeling_halveringstijd_h": self._haal_float_met_default(
                "input_number.dynamisch_warmte_afkoeling_halveringstijd_uren",
                2.0,
                minimum=0.05,
            ),
            "warmte_stijging_laden_c_per_c2h": self._haal_warmte_stijging_factor(
                "input_number.dynamisch_warmte_stijging_laden_c_per_c2h",
                WARMTE_STIJGING_LADEN_C_PER_C2H,
            ),
            "warmte_stijging_ontladen_c_per_c2h": self._haal_warmte_stijging_factor(
                "input_number.dynamisch_warmte_stijging_ontladen_c_per_c2h",
                WARMTE_STIJGING_ONTLADEN_C_PER_C2H,
            ),
            "temp_limiet_c": self._haal_float_met_default(
                "input_number.dynamisch_max_temp_boven_80_soc",
                TEMP_LIMIET_C,
            ),
            "temp_limiet_lage_soc_c": self._haal_float_met_default(
                "input_number.dynamisch_max_temp_onder_80_soc",
                TEMP_LIMIET_LAGE_SOC_C,
            ),
            "temp_penalty_factor": self._haal_float_met_default(
                "input_number.dynamisch_temp_penalty_factor",
                TEMP_PENALTY_FACTOR,
                minimum=0.0,
            ),
            "temp_penalty_100_soc_factor": self._haal_float_met_default(
                "input_number.dynamisch_temp_penalty_100_soc_factor",
                TEMP_PENALTY_100_SOC_FACTOR,
                minimum=1.0,
            ),
            "hoge_soc_verblijf_penalty_factor": self._haal_float_met_default(
                "input_number.dynamisch_hoge_soc_verblijf_penalty_factor",
                HOGE_SOC_VERBLIJF_PENALTY_FACTOR,
                minimum=0.0,
            ),
            "lage_soc_verblijf_penalty_factor": self._haal_float_met_default(
                "input_number.dynamisch_lage_soc_verblijf_penalty_factor",
                LAGE_SOC_VERBLIJF_PENALTY_FACTOR,
                minimum=0.0,
            ),
        }

    def _haal_plateau_spreiding(self) -> bool:
        """
        Leest uit apps.yaml of de plateau-nabewerking actief mag zijn.
        """
        waarde = self.args.get("plateau_spreiding", True)
        if isinstance(waarde, bool):
            return waarde
        return str(waarde).strip().lower() in ("1", "true", "yes", "on", "aan")
