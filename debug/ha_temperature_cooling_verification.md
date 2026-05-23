# Afkoelcoefficient verificatie

- Bron: `debug/ha_temperature_history_raw.json`
- Venster: `2026-05-16T20:11:22.833195+02:00` tot `2026-05-23T19:46:24.945490+02:00`
- Rustblokken gebruikt: `14`

## Modelbetekenis
- `input_number.dynamisch_warmte_afkoeling_halveringstijd_uren` is een halveringstijd in uren.
- Lagere waarde betekent sneller afkoelen.
- Hogere waarde betekent trager afkoelen.
- De formule in `strategie_dp.py` is `0.5 ** (duur_h / warmte_afkoeling_halveringstijd_h)`.

## Batterij 1 en 2 binnen
- Aangenomen omgeving: `20-23 °C`.
- Endpointmethode `20.0 °C`: n `24`, mediaan `5.54 h`, gemiddelde `6.63 h`.
- Endpointmethode `21.0 °C`: n `24`, mediaan `4.98 h`, gemiddelde `5.70 h`.
- Endpointmethode `21.5 °C`: n `24`, mediaan `4.76 h`, gemiddelde `5.18 h`.
- Endpointmethode `22.0 °C`: n `22`, mediaan `4.45 h`, gemiddelde `4.42 h`.
- Endpointmethode `23.0 °C`: n `18`, mediaan `4.23 h`, gemiddelde `4.13 h`.
- Least-squares batterij 1 bij `21.5 °C`: `6.25 h`.
- Least-squares batterij 2 bij `21.5 °C`: `6.40 h`.

## Batterij 3 in schuur
- Aangenomen omgeving: Buienradar is niet exact de schuurtemperatuur.
- De schuurvariant met buitentemperatuur direct geeft een least-squares halveringstijd van `9.46 h`.
- De schuurvariant met 4 uur vertraging en +4 °C dagopwarming geeft een least-squares halveringstijd van `8.56 h`.
- Endpointvarianten voor batterij 3 lagen grofweg rond `5.4-6.9 h` mediaan, afhankelijk van vertraging en dagopwarming.

## Opvallende rustblokken
- `2026-05-16T22:56:00+02:00` tot `2026-05-17T10:01:00+02:00` | duur `11.08 h` | bat1 `35.0->23.0` | bat2 `35.0->23.0` | bat3 `29.0->14.0` | buiten `6.5->10.8`
- `2026-05-17T15:01:00+02:00` tot `2026-05-17T20:01:00+02:00` | duur `5.00 h` | bat1 `33.0->26.0` | bat2 `33.0->26.0` | bat3 `27.0->21.0` | buiten `14.3->13.0`
- `2026-05-17T23:01:00+02:00` tot `2026-05-18T12:01:00+02:00` | duur `13.00 h` | bat1 `35.0->23.0` | bat2 `35.0->23.0` | bat3 `30.0->16.0` | buiten `10.0->14.9`
- `2026-05-18T14:11:00+02:00` tot `2026-05-18T19:41:00+02:00` | duur `5.50 h` | bat1 `33.0->26.0` | bat2 `32.0->26.0` | bat3 `26.0->22.0` | buiten `14.3->13.9`
- `2026-05-18T21:06:00+02:00` tot `2026-05-20T13:51:00+02:00` | duur `40.75 h` | bat1 `30.0->22.0` | bat2 `30.0->22.0` | bat3 `25.0->17.0` | buiten `12.0->16.4`
- `2026-05-20T16:41:00+02:00` tot `2026-05-20T18:01:00+02:00` | duur `1.33 h` | bat1 `38.0->35.0` | bat2 `38.0->35.0` | bat3 `34.0->32.0` | buiten `17.3->16.7`
- `2026-05-20T19:21:00+02:00` tot `2026-05-20T20:01:00+02:00` | duur `0.67 h` | bat1 `32.0->31.0` | bat2 `32.0->31.0` | bat3 `30.0->29.0` | buiten `15.8->15.3`
- `2026-05-21T08:16:00+02:00` tot `2026-05-21T08:46:00+02:00` | duur `0.50 h` | bat1 `25.0->25.0` | bat2 `25.0->25.0` | bat3 `22.0->22.0` | buiten `14.3->14.7`
- `2026-05-21T15:06:00+02:00` tot `2026-05-21T19:01:00+02:00` | duur `3.92 h` | bat1 `38.0->31.0` | bat2 `38.0->31.0` | bat3 `36.0->31.0` | buiten `18.5->19.3`
- `2026-05-21T22:01:00+02:00` tot `2026-05-22T11:01:00+02:00` | duur `13.00 h` | bat1 `40.0->25.0` | bat2 `38.0->25.0` | bat3 `41.0->21.0` | buiten `16.6->21.0`
- `2026-05-22T14:01:00+02:00` tot `2026-05-22T20:01:00+02:00` | duur `6.00 h` | bat1 `39.0->29.0` | bat2 `38.0->29.0` | bat3 `38.0->33.0` | buiten `23.1->23.6`
- `2026-05-22T22:01:00+02:00` tot `2026-05-23T09:01:00+02:00` | duur `11.00 h` | bat1 `39.0->26.0` | bat2 `39.0->26.0` | bat3 `43.0->24.0` | buiten `18.5->21.3`
- `2026-05-23T09:51:00+02:00` tot `2026-05-23T12:16:00+02:00` | duur `2.42 h` | bat1 `27.0->27.0` | bat2 `26.0->26.0` | bat3 `25.0->25.0` | buiten `23.3->26.1`
- `2026-05-23T15:36:00+02:00` tot `2026-05-23T19:46:00+02:00` | duur `4.17 h` | bat1 `36.0->30.0` | bat2 `36.0->31.0` | bat3 `38.0->36.0` | buiten `27.7->22.5`

## Conclusie
- Voor batterij 1 en 2 past een halveringstijd rond `5-7 h` beter dan `9 h`, afhankelijk van de echte binnentemperatuur.
- Voor batterij 3 past `8-9.5 h` redelijk als de schuurtemperatuur wordt benaderd met buitentemperatuur plus vertraging en dagopwarming.
- Eén globale afkoelhalveringstijd blijft een compromis omdat batterij 1 en 2 binnen staan en batterij 3 in de schuur staat.
- Als het algoritme één waarde houdt, is `7 h` een betere middenwaarde dan `9 h` voor de totale set.
- Een nauwkeuriger model vraagt aparte omgevingstemperaturen of aparte afkoelhalveringstijden per batterijgroep: binnen en schuur.
