# Zigbee2MQTT- en USB-healthcheck

`z2m_health_nl.yaml` controleert elke twee minuten via een lege MQTT-payload op
`zigbee2mqtt/bridge/request/health_check`. De coordinator-antwoordsensor gaat op
on bij `status=ok` en `data.healthy=true`, op off bij een foutantwoord en wordt
na 300 seconden zonder antwoord unavailable. De bestaande bridge-connectiviteit
wordt afzonderlijk gecontroleerd. Alleen retained 'online' is onvoldoende.

Een probleem dat vijf minuten duurt geeft een blijvende melding en één push via
`input_text.zendure_health_notify_service` (het bestaande Zendure-healthpackage).
Na twee gezonde minuten volgt één herstelmelding. De helper
`input_boolean.z2m_health_alert_actief` onderdrukt herhaling. De periodieke
controle herstelt gemiste statustriggers na reload. Bij een stil proces dat zijn
online-status behoudt kan detectie maximaal ongeveer tien minuten duren.
De melding maakt onderscheid tussen bridge-status en health-antwoord; de melding
bewijst op zichzelf niet dat USB-passthrough de oorzaak is. HA- of NAS-uitval
vereist een externe monitor. Het pakket herstart of ontkoppelt niets automatisch.

## NAS-bevindingen op 22 oktober 2026

- Sonoff Zigbee 3.0 USB Dongle Plus, vendor/product `10c4:ea60`, serial `0001`.
- HA-VM `d67a096e-bde8-432b-aa97-2af7d5b69349` draait in Synology VMM.
- De actieve USB-hostdev bevat vendor/product en adres bus 1/device 2.
- NAS-inspectie werkt via `/usr/local/bin/virsh -r -c qemu:///system`.
- De stick is in HA zichtbaar als `/dev/serial/by-id/usb-Silicon_Labs_Sonoff_Zigbee_3.0_USB_Dongle_Plus_0001-if00-port0`.
- Z2M gebruikt nu `/dev/ttyUSB0`. Een by-id-poort voorkomt wisselende tty-nummers,
  maar herstelt geen ontbrekende USB-koppeling aan de VM. De poort is ongewijzigd.

Het gemelde rebootprobleem past bij een verouderde of niet herstelde VMM-koppeling.
De fout is niet door een reboot gereproduceerd. Een soortgelijk, maar ander
adapter-/firmwarescenario staat in
https://github.com/home-assistant/core/issues/162157.
Health-requestdocumentatie:
https://www.zigbee2mqtt.io/guide/usage/mqtt_topics_and_messages.html#zigbee2mqttbridgerequesthealth_check.

## Voorbereid herstel voor DSM Taakplanner

`scripts/synology/z2m_usb_recovery.py` inspecteert standaard en verandert niets.
De inspectiemodus is op de NAS getest: juiste VM, één stick, één koppeling.
Met `--apply` (root vereist) ontkoppelt en herkoppelt het script uitsluitend deze
stick aan deze draaiende VM met het actuele USB-adres. De persistent opgeslagen
VMM-configuratie wordt niet veranderd. Er is maximaal één poging per boot via een
atomisch aangemaakte marker met Linux boot-ID. Ook na mislukking blijft die marker
staan. Er worden geen VM, NAS, UPS of andere USB-apparaten herstart.

De muterende uitvoering en automatisch herstel na NAS-reboot zijn nog niet getest.
Rootrechten ontbraken; de automatische NAS-taak is niet geïnstalleerd.

Installatievoorstel nadat roottoegang beschikbaar is:
1. Installeer het script als root-owned bestand, bijvoorbeeld
   `/usr/local/sbin/z2m_usb_recovery.py` (mode 0755). Gebruik geen door gewone
   gebruikers schrijfbaar script voor een root-taak.
2. Maak in DSM Taakplanner een getriggerde taak voor **Opstarten**, gebruiker **root**.
   Laat de taak eerst op de VM wachten (maximaal tien minuten):

   ```sh
   for attempt in $(seq 1 60); do
     if /usr/local/bin/virsh -r -c qemu:///system domstate d67a096e-bde8-432b-aa97-2af7d5b69349 | grep -qx running; then
       sleep 30
       /usr/bin/python3 /usr/local/sbin/z2m_usb_recovery.py --apply
       exit $?
     fi
     sleep 10
   done
   exit 1
   ```

3. Controleer bij een afgesproken test dat de by-id-poort terugkomt en de HA-healthcheck
   weer on antwoordt. Mogelijk moet Z2M daarna eenmalig worden herstart; automatiseer
   die vervolgstap pas na de praktijktest. De herkoppeling onderbreekt Zigbee kort,
   ook wanneer de koppeling tijdens boot al werkte.
4. Herstel: schakel de DSM-opstarttaak uit. De bestaande VMM-instellingen blijven
   intact. Bij een mislukte herkoppeling probeert het script de oude live-koppeling
   terug te plaatsen; als dat faalt is handmatige VMM-herkoppeling nodig.

Door de USB-stick bij een zelfstandig netwerkcoordinator onder te brengen zou
USB-passthrough geheel vervallen; dat is een mogelijke hardwarewijziging en valt
buiten deze installatie.

Verificatie: 208 repositorytests slagen, inclusief 10 Zigbee-/USB-tests.
`ha core check`, Python-compilatie, Bash-syntax en `git diff --check` slagen.
Na reload ontving Home Assistant een gezond health-antwoord; de automatische
pollingautomation werd daarna om 17:38 Europe/Amsterdam uitgevoerd. De melding
gebruikt dezelfde reeds geteste pushservice als de Zendure-healthcheck.
