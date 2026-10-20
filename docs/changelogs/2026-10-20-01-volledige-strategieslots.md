---
title: "Laden en ontladen gedurende het volledige strategieslot"
summary: "De strategie kiest acties die het hele slot passen en bewaakt de ingestelde accugrenzen."
---

## Dynamisch Handelen

Voor gebruikers van Dynamisch Handelen plant de strategie laden of ontladen met de gekozen vermogensopdracht gedurende het volledige slot. Kleine restjes energie bij een volle of lege accu krijgen daardoor geen afzonderlijke gedeeltelijke actie. Het BMS kan het werkelijke laadvermogen beperken en veiligheidsgrenzen kunnen een actie stoppen.

De stop-SoC bewaakt de ingestelde accugrenzen. Opeenvolgende slots met dezelfde richting blijven de bestaande aansturing gebruiken, die vermogen bijstelt zonder extra stop/start. De thermische meetstatistiek en de berekening na opstarten blijven behouden.
