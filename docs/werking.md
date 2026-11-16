# Werking: vooruitkijken en verbruik verschuiven

## De taakverdeling

| Onderdeel | Wie regelt | Wat het EMS doet |
|---|---|---|
| Accu | de accu zelf, op zelfverbruik (nul op de meter) | lading en vermogen uitlezen, vooruitrekenen wat hij gaat doen |
| Warmtepomp | de warmtepomp zelf, op zijn stooklijn | verbruik voorspellen; bij zonoverschot de thermostaat tijdelijk wat hoger |
| Boiler | de thermostaat in het vat | het element aanzetten zolang er na de accu zon over is |
| Hottub en andere verbruikers | zichzelf | verbruik voorspellen |

Elk apparaat werkt ook zonder het EMS. Valt Home Assistant of het EMS uit, dan regelt de accu
gewoon verder en draaien warmtepomp en boiler op hun eigen instellingen. Bij herladen of
stoppen zet het EMS eerst terug wat het had aangezet. Stopt Home Assistant onverwacht terwijl de
thermostaat verhoogd is, dan pakt het EMS hem na de herstart weer op en zet hem op tijd terug;
een schema van de thermostaat zelf overschrijft de verhoging ook.

## Vooruitblik

Elk kwartier rekent het EMS de komende 36 uur door:

- **verbruik** per onderdeel, geleerd uit de statistieken van Home Assistant (zie README);
- **zon** uit de voorspelling van het Energie-dashboard, eventueel bijgesteld met de zonfactor;
- **de accu op zelfverbruik**, vanaf de lading van nu: laden met overschot, ontladen bij
  verbruik, binnen zijn vermogen en grenzen;
- **prijzen**, om aan te wijzen welke uren duur zijn: het duurste kwart, en duidelijk boven de
  mediaan. Op een vlakke dag zijn er geen dure uren.

Daaruit volgt de zin bovenaan het dashboard, bijvoorbeeld:

> Accu vol rond 12:45, daarna nog 6,1 kWh zon over. Accu leeg rond 22:15, daarna 4,3 kWh van
> het net (1,5 kWh in dure uren).

`sensor.huis_ems_vooruitblik` heeft dezelfde momenten en hoeveelheden als attributen;
`sensor.huis_ems_plan` heeft alles per kwartier.

## Hoe goed is de voorspelling

Per kwartier legt het EMS vast wat het bij het begin van dat kwartier verwachtte, en wat er
werkelijk gebeurde: netvermogen met en zonder accu (P1-meter en accuvermogen) en zonopwek (de
kWh-tellers van de omvormers). Werkelijk verbruik = net zonder accu + zon.
`sensor.huis_ems_voorspelkwaliteit` geeft over de afgelopen 7 dagen de gemiddelde afwijking per
kwartier, hoeveel het verbruik in totaal te hoog of te laag geschat was, en hoeveel zon er kwam
ten opzichte van de verwachting. Zit dat laatste structureel ernaast, stel dan de zonfactor bij.

Laat het EMS eerst een paar weken alleen meekijken (Verbruik verschuiven uit). Dan zie je of de
voorspelling goed genoeg is om op te sturen.

## Verbruik verschuiven

Een buffer neemt zonnestroom op die anders naar het net zou gaan. Er zijn twee soorten:

- **thermostaat**: de ingestelde temperatuur gaat tijdelijk een of meer graden omhoog. De
  warmtepomp blijft dan doorlopen en pompt warmte in de vloer. Daarna zet het EMS de
  temperatuur terug naar wat er stond.
- **schakelaar**: een verwarmingselement gaat aan, zoals dat in de boiler. De eigen thermostaat
  van het vat blijft de harde grens.

### Wanneer een buffer aan mag

Het overschot wordt elke meting bepaald:

> overschot = teruglevering − wat de accu ontlaadt + wat de buffers zelf al gebruiken

Wat de accu laadt telt niet als overschot: de accu gaat voor. Wat de accu ontlaadt telt als
tekort: de accu voedt nooit een buffer. Het overschot wordt over een halve minuut uitgemiddeld.

Een buffer gaat aan als er genoeg overschot is voor zijn vermogen (plus 50 W) en daarbovenop:

| Voorwaarde | Standaard vloer | Standaard boiler |
|---|---|---|
| accu minstens zo vol | 90% | 95% |
| tussen | 10:00 en 16:00 | de hele dag |
| temperatuur onder | 22 °C in de kamer | 60 °C in het vat |
| voorspelling toont genoeg overschot voor de minimale draaitijd | ja | nee |

Bij zon en wolken schakelt een buffer niet meteen: hij wacht tot het overschot een tijd stabiel
is, en heeft een minimale draai- en rusttijd. Eenmaal aan mag het overschot 10% tekortschieten.
Is er meer dan één buffer, dan gaan ze in volgorde: de vloer eerst, omdat de warmtepomp uit
1 kWh stroom meer warmte maakt dan een element.

### Wanneer hij uit gaat

- als het overschot te lang tekortschiet (na de minimale draaitijd);
- meteen, ook binnen de minimale draaitijd: na de eindtijd, als de temperatuurgrens bereikt is,
  als de accu meer dan 3 procentpunt onder zijn minimum zakt, of als de entiteit niet bereikbaar
  is;
- als hij een ingestelde tijd niets afneemt (vat op temperatuur, geen warmtevraag). Daarna
  probeert het EMS het twee uur niet opnieuw, zodat het overschot naar de volgende buffer kan.

### Met de hand ingrijpen

Zet iemand een buffer die het EMS had aangezet zelf uit, of stelt iemand de thermostaat anders
in, dan laat het EMS dat zo en begint het er twee uur niet opnieuw aan. Een buffer die met de
hand is aangezet neemt het EMS niet over en zet het niet uit.

### Alleen advies

Zolang `switch.huis_ems_verbruik_verschuiven` uit staat, schakelt het EMS niets. Per buffer is er
een binaire sensor `… vrijgegeven` die laat zien wat het zou doen, met de reden erbij. Die kan
ook dienen als signaal voor een eigen automatisering. Een buffer zonder entiteit geeft altijd
alleen advies.

### Wat het verbruiksmodel ervan ziet

Het element van de boiler heeft een eigen kWh-teller; dat verbruik blijft buiten het
verbruiksmodel. Uren waarin het EMS de thermostaat hoger had gezet tellen niet mee voor het
model van de warmtepomp, zodat het niet leert dat de warmtepomp bij zon meer gebruikt.

## Elektrische veiligheid

De fasebewaking is vervallen: de accu regelt zichzelf en de groepen in de meterkast zijn zo
verdeeld dat de buffers de hoofdzekering niet kunnen overbelasten. Wat software niet afvangt
blijft gelden:

- Een HomeWizard Energy Socket schakelt maximaal 16 A (3680 W). Een element van 2 kW zit daar
  ruim onder.
- Een stekker die vaak schakelt onder belasting slijt. De minimale draai- en rusttijd beperken
  het aantal schakelingen; reken op hooguit enkele per dag.
- De thermostaat in het vat en de beveiligingen van de warmtepomp blijven de harde grenzen.

## Lokaal werken

| Onderdeel | Lokaal | Opmerking |
|---|---|---|
| P1-meter | ja | via de HA-integratie of rechtstreeks |
| Omvormers | meestal | bijvoorbeeld via Modbus |
| Warmtepomp | afhankelijk | hangt af van merk en koppeling |
| Accu uitlezen | afhankelijk | Zendure lokaal via de zenSDK; andere merken via hun eigen integratie |
| Boiler | ja | HomeWizard Energy Socket |
| Prijzen (ENTSO-e), zonvoorspelling, weer | nee | komen per definitie van buiten |

Zonder internet valt alleen het vooruitkijken terug: zonder prijzen rekent het EMS met het
vaste tarief, zonder zonvoorspelling met wat het nog weet. Het verschuiven werkt op de meting en
blijft werken; alleen de voorwaarde "voorspelling toont genoeg overschot" kan dan niet meer
worden getoetst.

## Rekenlast

| Werk | Hoe vaak | Gemeten op een pc |
|---|---|---|
| Stap per meting (overschot, twee buffers) | elke meting | ongeveer 5 microseconde in de rekenkern |
| Vooruitblik over 36 uur | elk kwartier | minder dan 1 ms in de rekenkern |
| Leren uit een jaar statistieken | elke 6 uur | ongeveer 0,1 s per onderdeel in de rekenkern |

De planner met dynamisch programmeren draait niet meer in Home Assistant; die zit alleen nog in
de jaarsimulatie. Een Home Assistant Green heeft hier ruim genoeg aan. Wat het werkelijk kost staat
in de attributen van `sensor.huis_ems_verbruiksmodel`. `python tools/benchmark.py` meet de
rekenkern los van Home Assistant.

## Waarom de accu niet meer door het EMS wordt gestuurd

Een eerdere versie plande de accu per kwartier en regelde hem elke seconde op nul. De herhaling
in `tools/simulate.py --replay` liet zien dat slim plannen op een meetjaar weinig
oplevert boven gewoon zelfverbruik, met een realistische voorspelling. Daar stond een centrale
schakel tegenover die alles moest aansturen. De accu doet zelfverbruik zelf even goed en zonder
afhankelijkheden.
