# TODO

## Winst/kosten tracking

Winstregistratie via historische sensordata + historische prijzen, zodat het reproduceerbaar is zonder extra state bij te houden in de batterij simulatie.

Benodigde inputs:
- Import/export vermogen sensoren (reeds aanwezig)
- Historische Nordpool prijs op tijdstip van im/export
- `sensor.zendure_2400_ac_energie_import` / `_export` voor RTE controle

Aanpak: koppel HA recorder data aan historische prijzen via een periodieke Python berekening (bijv. dagelijks, of on-demand).
