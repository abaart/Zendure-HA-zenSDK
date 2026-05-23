# Temperatuurmodel verificatie

- Vastgelegd: `2026-05-23T20:11:37.612643+02:00`
- History venster: `2026-05-16T20:11:22.833195+02:00` tot `2026-05-23T20:11:17.279041+02:00`
- Samples: `2016` met stap `5 minuten`
- Gekwalificeerde actieve blokken: `25`
- Actieve blokken met temperatuurstijging: `19`
- Actieve stijgende blokken waar model zonder actieve koeling meer dan 0,5 °C te laag zit: `9`
- Actieve stijgende blokken waar model met actieve koeling meer dan 0,5 °C te laag zit: `14`
- Gebruikte `accu_max_kwh`: `4.635`
- Gebruikte `eta`: `0.919`
- Gebruikte `input_number.dynamisch_warmte_stijging_c_per_c2h`: `20.0`
- Gebruikte `input_number.dynamisch_warmte_afkoeling_halveringstijd_uren`: `9.0`
- Historische factor zonder koelingscorrectie: `24.6 °C per C²h`

## Grootste gemeten stijgingen
- `2026-05-20T13:51:00+02:00` tot `2026-05-20T16:41:00+02:00` | `laden` | duur `2.83 h` | gemiddeld vermogen `1926 W` | temp `22.0 -> 38.0 °C` = `+16.0 °C` | model zonder actieve koeling `+9.3 °C` | verschil `+6.7 °C` | model met actieve koeling `+7.3 °C` | verschil `+8.7 °C` | factor_observed `34.5`
- `2026-05-22T11:01:00+02:00` tot `2026-05-22T14:01:00+02:00` | `laden` | duur `3.00 h` | gemiddeld vermogen `1844 W` | temp `25.0 -> 39.0 °C` = `+14.0 °C` | model zonder actieve koeling `+8.7 °C` | verschil `+5.3 °C` | model met actieve koeling `+7.2 °C` | verschil `+6.8 °C` | factor_observed `32.1`
- `2026-05-17T10:01:00+02:00` tot `2026-05-17T14:36:00+02:00` | `laden` | duur `4.58 h` | gemiddeld vermogen `1184 W` | temp `23.0 -> 34.0 °C` = `+11.0 °C` | model zonder actieve koeling `+6.2 °C` | verschil `+4.8 °C` | model met actieve koeling `+2.1 °C` | verschil `+8.9 °C` | factor_observed `35.3`
- `2026-05-18T12:01:00+02:00` tot `2026-05-18T14:11:00+02:00` | `laden` | duur `2.17 h` | gemiddeld vermogen `1481 W` | temp `23.0 -> 33.0 °C` = `+10.0 °C` | model zonder actieve koeling `+4.3 °C` | verschil `+5.7 °C` | model met actieve koeling `+2.7 °C` | verschil `+7.3 °C` | factor_observed `46.2`
- `2026-05-23T12:56:00+02:00` tot `2026-05-23T15:36:00+02:00` | `laden` | duur `2.67 h` | gemiddeld vermogen `1339 W` | temp `28.0 -> 38.0 °C` = `+10.0 °C` | model zonder actieve koeling `+4.5 °C` | verschil `+5.5 °C` | model met actieve koeling `+3.9 °C` | verschil `+6.1 °C` | factor_observed `44.2`
- `2026-05-22T20:01:00+02:00` tot `2026-05-22T22:01:00+02:00` | `ontladen` | duur `2.00 h` | gemiddeld vermogen `2398 W` | temp `33.0 -> 43.0 °C` = `+10.0 °C` | model zonder actieve koeling `+12.7 °C` | verschil `-2.7 °C` | model met actieve koeling `+10.1 °C` | verschil `-0.1 °C` | factor_observed `15.8`
- `2026-05-17T20:01:00+02:00` tot `2026-05-17T23:01:00+02:00` | `ontladen` | duur `3.00 h` | gemiddeld vermogen `1587 W` | temp `26.0 -> 35.0 °C` = `+9.0 °C` | model zonder actieve koeling `+9.5 °C` | verschil `-0.5 °C` | model met actieve koeling `+5.2 °C` | verschil `+3.8 °C` | factor_observed `18.9`
- `2026-05-21T19:51:00+02:00` tot `2026-05-21T22:01:00+02:00` | `ontladen` | duur `2.17 h` | gemiddeld vermogen `1986 W` | temp `33.0 -> 41.0 °C` = `+8.0 °C` | model zonder actieve koeling `+9.7 °C` | verschil `-1.7 °C` | model met actieve koeling `+6.6 °C` | verschil `+1.4 °C` | factor_observed `16.5`
- `2026-05-16T20:16:00+02:00` tot `2026-05-16T22:56:00+02:00` | `ontladen` | duur `2.67 h` | gemiddeld vermogen `1604 W` | temp `28.0 -> 35.0 °C` = `+7.0 °C` | model zonder actieve koeling `+8.7 °C` | verschil `-1.7 °C` | model met actieve koeling `+4.2 °C` | verschil `+2.8 °C` | factor_observed `16.1`
- `2026-05-21T11:01:00+02:00` tot `2026-05-21T12:36:00+02:00` | `laden` | duur `1.58 h` | gemiddeld vermogen `1354 W` | temp `31.0 -> 35.0 °C` = `+4.0 °C` | model zonder actieve koeling `+2.6 °C` | verschil `+1.4 °C` | model met actieve koeling `+0.9 °C` | verschil `+3.1 °C` | factor_observed `30.9`
- `2026-05-18T19:41:00+02:00` tot `2026-05-18T21:06:00+02:00` | `ontladen` | duur `1.42 h` | gemiddeld vermogen `1496 W` | temp `26.0 -> 30.0 °C` = `+4.0 °C` | model zonder actieve koeling `+5.3 °C` | verschil `-1.3 °C` | model met actieve koeling `+3.8 °C` | verschil `+0.2 °C` | factor_observed `15.1`
- `2026-05-21T09:21:00+02:00` tot `2026-05-21T10:31:00+02:00` | `laden` | duur `1.17 h` | gemiddeld vermogen `837 W` | temp `27.0 -> 29.0 °C` = `+2.0 °C` | model zonder actieve koeling `+0.7 °C` | verschil `+1.3 °C` | model met actieve koeling `-0.2 °C` | verschil `+2.2 °C` | factor_observed `56.8`
- `2026-05-21T08:46:00+02:00` tot `2026-05-21T09:01:00+02:00` | `ontladen` | duur `0.25 h` | gemiddeld vermogen `2133 W` | temp `25.0 -> 27.0 °C` = `+2.0 °C` | model zonder actieve koeling `+1.3 °C` | verschil `+0.7 °C` | model met actieve koeling `+1.1 °C` | verschil `+0.9 °C` | factor_observed `30.9`
- `2026-05-21T13:46:00+02:00` tot `2026-05-21T15:06:00+02:00` | `laden` | duur `1.33 h` | gemiddeld vermogen `1473 W` | temp `36.0 -> 38.0 °C` = `+2.0 °C` | model zonder actieve koeling `+3.0 °C` | verschil `-1.0 °C` | model met actieve koeling `+1.1 °C` | verschil `+0.9 °C` | factor_observed `13.2`
- `2026-05-21T10:36:00+02:00` tot `2026-05-21T10:56:00+02:00` | `laden` | duur `0.33 h` | gemiddeld vermogen `1321 W` | temp `30.0 -> 31.0 °C` = `+1.0 °C` | model zonder actieve koeling `+0.5 °C` | verschil `+0.5 °C` | model met actieve koeling `+0.2 °C` | verschil `+0.8 °C` | factor_observed `40.2`

## Splitsing Laden/Ontladen
- `laden`: `11` stijgende actieve blokken | historische factor `34.5 °C per C²h` | totaal gemeten `72.0 °C` | totaal model zonder actieve koeling `41.8 °C` | totaal model met actieve koeling `26.5 °C` | mediaan verschil zonder actieve koeling `+1.4 °C` | mediaan verschil met actieve koeling `+3.1 °C`
- `ontladen`: `8` stijgende actieve blokken | historische factor `16.5 °C per C²h` | totaal gemeten `42.0 °C` | totaal model zonder actieve koeling `51.0 °C` | totaal model met actieve koeling `33.9 °C` | mediaan verschil zonder actieve koeling `-1.5 °C` | mediaan verschil met actieve koeling `+0.7 °C`
## Sensorbereik
- `sensor.zendure_2400_ac_warmste_batterij_temperatuur`: `35` samples, `2026-05-23T17:38:04.853604+02:00` tot `2026-05-23T19:46:25.024882+02:00`
- `sensor.zendure_2400_ac_batterij_1_temperatuur`: `1087` samples, `2026-05-16T20:11:22.833195+02:00` tot `2026-05-23T20:09:12.210519+02:00`
- `sensor.zendure_2400_ac_batterij_2_temperatuur`: `937` samples, `2026-05-16T20:11:22.833195+02:00` tot `2026-05-23T20:05:55.232995+02:00`
- `sensor.zendure_2400_ac_batterij_3_temperatuur`: `826` samples, `2026-05-16T20:11:22.833195+02:00` tot `2026-05-23T19:46:24.945490+02:00`
- `sensor.zendure_2400_ac_vermogen_aansturing`: `47644` samples, `2026-05-16T20:11:22.833195+02:00` tot `2026-05-23T20:11:17.279041+02:00`
- `sensor.zendure_2400_ac_laadpercentage`: `1042` samples, `2026-05-16T20:11:22.833195+02:00` tot `2026-05-23T20:10:52.237574+02:00`
- `sensor.buienradar_temperature`: `781` samples, `2026-05-16T20:11:22.833195+02:00` tot `2026-05-23T20:10:48.216106+02:00`
- `sensor.dynamisch_handelsstrategie`: `1421` samples, `2026-05-16T20:11:22.833195+02:00` tot `2026-05-23T20:00:08.984699+02:00`

## Interpretatie
- Mediaan verschil zonder actieve koeling: `+0.4 °C` gemeten minus model.
- Mediaan verschil met actieve koeling: `+1.4 °C` gemeten minus model.
- De historische factor `24.6` ligt dicht bij de ingestelde factor `20.0` volgens de gewogen analyse.
- Wel zijn er `9` stijgende actieve blokken waar het model zonder actieve koeling meer dan `0,5 °C` te laag zit.
- Conclusie: de volledige history bewijst geen structurele onderschatting, maar enkele blokken laten wel forse onderschatting zien.

## Ruwe data
- Volledige analyse: `debug/ha_temperature_model_verification.json`
- Ruwe Home Assistant history: `debug/ha_temperature_history_raw.json`
