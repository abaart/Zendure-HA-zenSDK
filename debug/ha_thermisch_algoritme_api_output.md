# HA thermisch algoritme API dump

- Vastgelegd: `2026-05-23T20:05:56.679648+02:00`
- HA URL: `http://192.168.0.111:8123`
- API status: `200`
- Entiteiten gevonden: `39/40`
- Strategie slots: `29`
- Weather forecast entity: `weather.openweathermap`
- Weather forecast status: `200`
- Weather forecast punten: `40`

## Belangrijkste waarden
- `sensor.zendure_2400_ac_warmste_batterij_temperatuur`: `36.0` °C
- `sensor.zendure_2400_ac_warmste_batterij_index`: `3`
- `sensor.zendure_2400_ac_batterij_1_temperatuur`: `30.0` °C
- `sensor.zendure_2400_ac_batterij_2_temperatuur`: `31.0` °C
- `sensor.zendure_2400_ac_batterij_3_temperatuur`: `36.0` °C
- `sensor.buienradar_temperature`: `22.1` °C
- `sensor.openweathermap_temperature`: ontbreekt
- `weather.openweathermap`: `unknown`

## Strategie attributen
- `bijgewerkt`: `2026-05-23T20:00:08.934157+02:00`
- `volgende_actie`: `ontladen`
- `volgende_start`: `2026-05-23T20:00:00+02:00`
- `laad_slots`: `3`
- `ontlaad_slots`: `7`
- `accu_huidig_kwh`: `4.524`
- `accu_max_kwh`: `4.635`
- `eta`: `0.919`
- `batterij_temp_start_c`: `36.0`
- `buiten_temp_huidig_c`: `None`
- `buiten_temp_bron`: `onbekend`
- `weather_entity`: `weather.openweathermap`
- `forecast_bron`: `weather.get_forecasts`
- `forecast_punten`: `40`
- `warmte_afkoeling_halveringstijd_h`: `9.0`
- `warmte_stijging_c_per_c2h`: `20.0`
- `temp_limiet_c`: `36.0`
- `temp_penalty_factor`: `1.0`
- `temp_soc_drempel_pct`: `80.0`

## Compleetheidsproblemen
- Ontbrekende entiteit: `sensor.openweathermap_temperature` HTTP `404`
- Ongeldige state: `input_text.zendure_2400_ac_batterij_volgorde` = ``
- Ongeldige state: `sensor.zendure_2400_ac_batterij_4_temperatuur` = `unknown`
- Ongeldige state: `sensor.zendure_2400_ac_batterij_5_temperatuur` = `unknown`
- Ongeldige state: `sensor.zendure_2400_ac_batterij_6_temperatuur` = `unknown`
- Ongeldige state: `input_text.dynamisch_buitentemperatuur_sensor` = ``
- Ongeldige state: `weather.openweathermap` = `unknown`
- Ontbrekend strategie-attribuut: `buiten_temp_huidig_c` = `None`
- Slotveld mist: slot `24` start `2026-05-24T19:00:00+02:00` actie `ontladen` veld `warmte_penalty_eur`

## Eerste 12 slots
```json
[
  {
    "start": "2026-05-23T19:00:00+02:00",
    "end": "2026-05-23T20:00:00+02:00",
    "prijs_ct": 30.363,
    "actie": "rust",
    "soc_voor_kwh": 4.5,
    "soc_na_kwh": 4.5,
    "soc_voor_pct": 87.7,
    "soc_na_pct": 87.7,
    "batterij_temp_voor_c": 36.0,
    "batterij_temp_na_c": 35.99,
    "buiten_temp_c": 23.4,
    "temp_limiet_c": 36.0,
    "temp_limiet_actief": "true"
  },
  {
    "start": "2026-05-23T20:00:00+02:00",
    "end": "2026-05-23T21:00:00+02:00",
    "prijs_ct": 33.296,
    "actie": "ontladen",
    "vermogen_w": 1500,
    "verwacht_vermogen_w": 1500,
    "soc_voor_kwh": 4.5,
    "soc_na_kwh": 2.9,
    "soc_voor_pct": 87.7,
    "soc_na_pct": 60.1,
    "winst_eur": 0.4898,
    "warmte_penalty_eur": 0.0095,
    "c_waarde": 0.345,
    "batterij_temp_voor_c": 35.99,
    "batterij_temp_na_c": 38.38,
    "buiten_temp_c": 23.4,
    "temp_limiet_c": 36.0,
    "geplande_actie": "ontladen",
    "actief_slot_begin_kwh": 4.5,
    "actief_slot_raw_soc_kwh": 4.519,
    "actuele_soc_kwh": 4.519,
    "actuele_soc_bron": "laadpercentage",
    "doel_soc_kwh": 2.9,
    "actief_slot_delta_kwh": 1.619,
    "actief_slot_resterend_h": 0.998
  },
  {
    "start": "2026-05-23T21:00:00+02:00",
    "end": "2026-05-23T22:00:00+02:00",
    "prijs_ct": 34.44,
    "actie": "ontladen",
    "vermogen_w": 2000,
    "verwacht_vermogen_w": 2000,
    "soc_voor_kwh": 2.9,
    "soc_na_kwh": 0.75,
    "soc_voor_pct": 60.1,
    "soc_na_pct": 23.0,
    "winst_eur": 0.6808,
    "warmte_penalty_eur": 0.0231,
    "c_waarde": 0.464,
    "batterij_temp_voor_c": 38.38,
    "batterij_temp_na_c": 42.68,
    "buiten_temp_c": 23.4,
    "temp_limiet_c": 36.0
  },
  {
    "start": "2026-05-23T22:00:00+02:00",
    "end": "2026-05-23T23:00:00+02:00",
    "prijs_ct": 31.866,
    "actie": "ontladen",
    "vermogen_w": 750,
    "verwacht_vermogen_w": 700,
    "soc_voor_kwh": 0.75,
    "soc_voor_pct": 23.0,
    "soc_na_pct": 10.0,
    "winst_eur": 0.2197,
    "warmte_penalty_eur": 0.001,
    "c_waarde": 0.162,
    "batterij_temp_voor_c": 42.68,
    "batterij_temp_na_c": 43.2,
    "buiten_temp_c": 23.4,
    "temp_limiet_c": 36.0
  },
  {
    "start": "2026-05-23T23:00:00+02:00",
    "end": "2026-05-24T00:00:00+02:00",
    "prijs_ct": 30.285,
    "actie": "rust",
    "soc_voor_pct": 10.0,
    "soc_na_pct": 10.0,
    "batterij_temp_voor_c": 43.2,
    "batterij_temp_na_c": 41.58,
    "buiten_temp_c": 21.3,
    "temp_limiet_c": 36.0
  },
  {
    "start": "2026-05-24T00:00:00+02:00",
    "end": "2026-05-24T01:00:00+02:00",
    "prijs_ct": 30.056,
    "actie": "rust",
    "soc_voor_pct": 10.0,
    "soc_na_pct": 10.0,
    "batterij_temp_voor_c": 41.58,
    "batterij_temp_na_c": 40.08,
    "buiten_temp_c": 21.3,
    "temp_limiet_c": 36.0
  },
  {
    "start": "2026-05-24T01:00:00+02:00",
    "end": "2026-05-24T02:00:00+02:00",
    "prijs_ct": 29.657,
    "actie": "rust",
    "soc_voor_pct": 10.0,
    "soc_na_pct": 10.0,
    "batterij_temp_voor_c": 40.08,
    "batterij_temp_na_c": 38.68,
    "buiten_temp_c": 21.3,
    "temp_limiet_c": 36.0
  },
  {
    "start": "2026-05-24T02:00:00+02:00",
    "end": "2026-05-24T03:00:00+02:00",
    "prijs_ct": 29.074,
    "actie": "rust",
    "soc_voor_pct": 10.0,
    "soc_na_pct": 10.0,
    "batterij_temp_voor_c": 38.68,
    "batterij_temp_na_c": 37.1,
    "buiten_temp_c": 17.3,
    "temp_limiet_c": 36.0
  },
  {
    "start": "2026-05-24T03:00:00+02:00",
    "end": "2026-05-24T04:00:00+02:00",
    "prijs_ct": 28.647,
    "actie": "rust",
    "soc_voor_pct": 10.0,
    "soc_na_pct": 10.0,
    "batterij_temp_voor_c": 37.1,
    "batterij_temp_na_c": 35.63,
    "buiten_temp_c": 17.3,
    "temp_limiet_c": 36.0
  },
  {
    "start": "2026-05-24T04:00:00+02:00",
    "end": "2026-05-24T05:00:00+02:00",
    "prijs_ct": 28.416,
    "actie": "rust",
    "soc_voor_pct": 10.0,
    "soc_na_pct": 10.0,
    "batterij_temp_voor_c": 35.63,
    "batterij_temp_na_c": 34.27,
    "buiten_temp_c": 17.3,
    "temp_limiet_c": 36.0
  },
  {
    "start": "2026-05-24T05:00:00+02:00",
    "end": "2026-05-24T06:00:00+02:00",
    "prijs_ct": 27.884,
    "actie": "rust",
    "soc_voor_pct": 10.0,
    "soc_na_pct": 10.0,
    "batterij_temp_voor_c": 34.27,
    "batterij_temp_na_c": 32.67,
    "buiten_temp_c": 12.7,
    "temp_limiet_c": 36.0
  },
  {
    "start": "2026-05-24T06:00:00+02:00",
    "end": "2026-05-24T07:00:00+02:00",
    "prijs_ct": 27.689,
    "actie": "rust",
    "soc_voor_pct": 10.0,
    "soc_na_pct": 10.0,
    "batterij_temp_voor_c": 32.67,
    "batterij_temp_na_c": 31.19,
    "buiten_temp_c": 12.7,
    "temp_limiet_c": 36.0
  }
]
```
