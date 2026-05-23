# C-waarde warmteverlies en DP-penalty advies

Bron: `debug/ha_temperature_model_verification.json`.

## Fysieke interpretatie

Bij ohmse verliezen geldt bij gelijke accutemperatuur en weerstand ongeveer:

- warmtevermogen is evenredig met `C²`;
- warmte per vaste tijd is evenredig met `C² × duur_h`;
- warmte per vaste hoeveelheid energie is ongeveer evenredig met `C`.

Het temperatuurmodel gebruikt daarom `C² × duur_h` voor temperatuurstijging per tijdslot.

## Historisch fitresultaat uit packtemperaturen

- Laden: `34.5 °C per C²h`.
- Ontladen: `16.5 °C per C²h`.
- Laden produceerde dus ongeveer `2.1×` zoveel temperatuurstijging per `C²h` als ontladen.

## Betekenis van de bestaande DP-penalty

De code gebruikt `WARMTE_PENALTY_EUR_PER_KWH_C2 = 0.05` en:

`penalty_eur = factor × 0.05 × energie_accu_kwh × C²`

Omgerekend naar ct/kWh:

`penalty_ct_per_kwh = 5 × factor × C²`

Voorbeelden:

| C | factor 1 | factor 1.6 | factor 5 | factor 10 |
|---|---:|---:|---:|---:|
| 0.2 | 0.20 ct/kWh | 0.32 ct/kWh | 1.00 ct/kWh | 2.00 ct/kWh |
| 0.3 | 0.45 ct/kWh | 0.72 ct/kWh | 2.25 ct/kWh | 4.50 ct/kWh |
| 0.4 | 0.80 ct/kWh | 1.28 ct/kWh | 4.00 ct/kWh | 8.00 ct/kWh |
| 0.5 | 1.25 ct/kWh | 2.00 ct/kWh | 6.25 ct/kWh | 12.50 ct/kWh |
| 0.6 | 1.80 ct/kWh | 2.88 ct/kWh | 9.00 ct/kWh | 18.00 ct/kWh |

## Historische C-waardes

Laden:

- Mediaan C: `0.267`.
- Maximum C: `0.425`.
- Factor 1 geeft mediaan `0.36 ct/kWh` en max `0.90 ct/kWh`.
- Factor 1.6 geeft mediaan `0.57 ct/kWh` en max `1.45 ct/kWh`.
- Factor 5 geeft mediaan `1.78 ct/kWh` en max `4.52 ct/kWh`.

Ontladen:

- Mediaan C: `0.374`.
- Maximum C: `0.563`.
- Factor 1 geeft mediaan `0.70 ct/kWh` en max `1.59 ct/kWh`.
- Factor 1.6 geeft mediaan `1.12 ct/kWh` en max `2.54 ct/kWh`.
- Factor 5 geeft mediaan `3.49 ct/kWh` en max `7.93 ct/kWh`.

## Advies

- Zet `input_number.dynamisch_warmte_penalty_laden_factor` op `5.0`.
- Zet `input_number.dynamisch_warmte_penalty_ontladen_factor` op `2.5`.

Waarom:

- De relatieve verhouding `5.0 / 2.5 = 2.0` sluit aan bij de gemeten verhouding tussen laden en ontladen: `34.5 / 16.5 = 2.1`.
- Bij laden rond `0.4C` kost factor `5.0` ongeveer `4 ct/kWh`. Dat is groot genoeg om 2400 W laden minder snel te kiezen wanneer een lager vermogen bijna even winstgevend is.
- Bij ontladen rond `0.5C` kost factor `2.5` ongeveer `3.1 ct/kWh`. Dat remt maximaal ontladen, maar niet zo hard als laden, omdat ontladen historisch minder temperatuurstijging per `C²h` liet zien.
- De minimale spread staat rond `8 ct/kWh`; factoren `1.0-1.6` geven bij de gemeten C-waardes meestal minder dan `2 ct/kWh` penalty en sturen daardoor zwak.

Praktische startwaarden:

- Voor conservatief batterijbehoud: laden `5.0`, ontladen `2.5`.
- Voor agressiever handelen: laden `3.0`, ontladen `1.5`.
- Voor maximale temperatuurbescherming bij warme dagen: laden `7.0`, ontladen `3.5`.
