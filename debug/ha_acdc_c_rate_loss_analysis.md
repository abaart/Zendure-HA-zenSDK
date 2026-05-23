# AC/DC verlies versus C-waarde

- Venster: `2026-05-16T20:35:48.269851+02:00` tot `2026-05-23T20:35:43.155981+02:00`
- Algoritme-capaciteit voor C-waarde: `4.613 kWh`
- Fysieke capaciteit ter referentie: `5.76 kWh`
- Verlies laden: `AC import - DC import`.
- Verlies ontladen: `DC export - AC export`.
- Factorvertaling: `factor = stroomprijs * extra_verliesfractie_per_C² / 0.05`.

## Laden
- Samples: `1292`
- Batterijzijde energie: `29.47 kWh`
- Gemeten verlies: `1.57 kWh`
- Verlies t.o.v. batterijzijde: `5.32 %`
- Fit `verliesfractie = basis + slope * C²`: basis `8.33 %`, slope `-20.09 procentpunt per C²`.
- Penaltyfactor bij 30 ct/kWh: `0.00`
- Penaltyfactor bij 25-35 ct/kWh: `0.00` tot `0.00`
- Laagste band `[0.2, 0.35]`: C `0.27`, verlies `5.99 %`.
- Hoogste band `[0.5, 0.7]`: C `0.51`, verlies `1.75 %`.
- Verschil hoogste minus laagste band: `-4.23` procentpunt.

| C-band | Duur h | Energie kWh | Gem. DC W | Eff % | Verlies % | Verlies W |
|---:|---:|---:|---:|---:|---:|---:|
| 0.00-0.20 | 5.98 | 3.17 | 530 | 91.4 | 9.43 | 50 |
| 0.20-0.35 | 6.35 | 7.67 | 1207 | 94.4 | 5.99 | 72 |
| 0.35-0.50 | 7.38 | 14.35 | 1943 | 95.1 | 5.12 | 99 |
| 0.50-0.70 | 1.82 | 4.28 | 2357 | 98.3 | 1.75 | 41 |

## Ontladen
- Samples: `1474`
- Batterijzijde energie: `28.44 kWh`
- Gemeten verlies: `1.94 kWh`
- Verlies t.o.v. batterijzijde: `6.81 %`
- Fit `verliesfractie = basis + slope * C²`: basis `7.91 %`, slope `-5.14 procentpunt per C²`.
- Penaltyfactor bij 30 ct/kWh: `0.00`
- Penaltyfactor bij 25-35 ct/kWh: `0.00` tot `0.00`
- Laagste band `[0.2, 0.35]`: C `0.28`, verlies `6.62 %`.
- Hoogste band `[0.5, 0.7]`: C `0.55`, verlies `6.54 %`.
- Verschil hoogste minus laagste band: `-0.08` procentpunt.

| C-band | Duur h | Energie kWh | Gem. DC W | Eff % | Verlies % | Verlies W |
|---:|---:|---:|---:|---:|---:|---:|
| 0.00-0.20 | 13.03 | 3.82 | 293 | 91.0 | 9.04 | 26 |
| 0.20-0.35 | 1.95 | 2.38 | 1223 | 93.4 | 6.62 | 81 |
| 0.35-0.50 | 3.85 | 7.79 | 2023 | 93.7 | 6.27 | 127 |
| 0.50-0.70 | 5.73 | 14.45 | 2520 | 93.5 | 6.54 | 165 |

## Advies
- Alleen op basis van extra AC/DC-verlies door C² komt laden uit rond `0.00` bij 30 ct/kWh.
- Alleen op basis van extra AC/DC-verlies door C² komt ontladen uit rond `0.00` bij 30 ct/kWh.
- Deze berekening meet omvormer/converterverlies aan AC/DC-kant. Batterij-interne warmte, levensduur en temperatuurveiligheid zitten hier niet volledig in.
