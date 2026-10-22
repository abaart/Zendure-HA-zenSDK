# Zendure healthcheck

Home Assistant bewaakt de strategie ook wanneer AppDaemon uitvalt. Het package
`Dutch (NL) Integration/packages/zendure_health_nl.yaml` maakt
`binary_sensor.zendure_health_probleem` en de automation
`automation.zendure_healthcheck_en_herstelmelding` aan.

Een probleemmelding volgt wanneer een van deze omstandigheden vijf minuten aanhoudt:
- de strategie ontbreekt of `bijgewerkt` is meer dan 90 minuten oud;
- de berekening staat langer dan 10 minuten op bezig;
- de laatste berekening heeft status `fout`;
- de proxy-uptime blijft onder 120 seconden of ontbreekt;
- na 15:00 ontbreken geldige Nord Pool-prijzen met de datum van morgen;
- `sensor.memory_free` meldt minder dan 300 MiB of is niet beschikbaar.

De RAM-drempel betreft vrij RAM, geen meting van AppDaemon-geheugen. Linux kan
vrij RAM voor cache gebruiken; pas de drempel aan als legitiem cachegebruik
meldingen veroorzaakt. De proxycontrole gebruikt de bestaande
`sensor.zendure_proxy_uptime`. De Nord Pool-bron wordt gelezen uit
`input_text.dynamisch_nordpool_sensor`.

Bij een incident verschijnen een blijvende Home Assistant-melding en een push.
`input_text.zendure_health_notify_service` bepaalt de pushservice; de beginwaarde
is `notify.mobile_app_bbf`. Pas de beginwaarde in YAML aan voor een andere
ontvanger. De notification-tag is `zendure_health`. Het helperveld
`input_boolean.zendure_health_alert_actief` onderdrukt herhaling en bewaart de
incidentstatus over een herstart. De automation controleert ook iedere vijf
minuten om een gemiste statusovergang na reload te herstellen. Een fout in de
pushservice verhindert de blijvende melding en incidentregistratie niet.

Na twee minuten zonder gedetecteerde problemen volgt één herstelpush en verdwijnt
de blijvende melding. De timers beginnen opnieuw na een Home Assistant-herstart
of template-reload. Er worden geen diensten automatisch herstart. Een volledig
uitgevallen Home Assistant kan zelf geen melding sturen; daarvoor is een externe
monitor nodig. Een tweede probleem tijdens een bestaand incident levert geen
extra push op; de actuele lijst staat in het attribuut `problemen` van de sensor.

Het deployscript kopieert het package bij volgende deployments. Bij een gerichte
installatie: kopieer het package, voer `ha core check` uit en reload achtereenvolgens
`input_boolean`, `input_text`, `template` en `automation` via hun `reload`-services.

Tests: `python3 -m pytest -q tests/test_zendure_health.py tests/test_dynamisch_handelen.py`
(88 geslaagd). De templates zijn ook via Home Assistant `/api/template` gerenderd:
`False` en een lege probleemlijst bij een gezonde installatie.

Live installatie op 22 oktober 2026: `ha core check` geslaagd; helpers, template
sensor en automation geladen via reload. De testpushservice accepteerde de
melding aan `notify.mobile_app_bbf`; ontvangst op het toestel is niet gemeten.
De volledige repositorysuite slaagt: 198 tests. Bash-syntax en `git diff --check`
slagen eveneens.
