# Werking: verbruik voorspellen per onderdeel

Het EMS regelt niets. Accu, warmtepomp en apparaten werken zoals ze zonder het EMS werken; het
EMS kijkt mee, voorspelt en legt vast hoe goed die voorspelling was.

## De onderdelen

| Onderdeel | Bron | Hoe het voorspeld wordt |
|---|---|---|
| Warmtepomp verwarmen | kWh-teller van de warmtepomp, buitentemperatuur, weersverwachting | profiel per uur van de dag plus extra verbruik per graad onder de stookgrens, over het 24-uursgemiddelde van de temperatuur |
| Warmtepomp tapwater | dezelfde teller, plus de melding "tapwater bezig" | cycli: hoe vaak per dag, op welk uur en hoeveel per keer; een lopende cyclus wordt afgemaakt |
| Verbruiker met eigen meter (hottub) | eigen kWh-teller, eventueel thermostaat | eigen profiel per uur en temperatuur; de stand van de thermostaat stelt de eerstkomende uren bij |
| Apparaten (wasmachine, vaatwasser, droger) | bedrijfstoestand en eindtijd uit Home Connect | wat loopt of op uitgestelde start staat, precies; daarnaast de gewoonte van de afgelopen maanden, per werkdag en weekend |
| Overig | wat overblijft van het huisverbruik | profiel per uur van de dag en de temperatuur, eventueel een eigen weekendprofiel, plus wat er nu afwijkt |
| Zon | de voorspelling in het Energie-dashboard | eventueel bijgesteld met de zonfactor |
| Accu | lading en vermogen, uitgelezen | op zelfverbruik doorgerekend vanaf de lading van nu |

### Huisverbruik

Het verbruik van het huis volgt uit de meters: afname min teruglevering plus zonopwek, min wat
een echte accu laadt. De warmtepomp en verbruikers met een eigen meter gaan eraf; wat overblijft
is "overig". Uren overdag waarin geen enkele zonneteller iets meldde, tellen niet mee voor
"overig": zonder zonmeting is het verbruik daar niet te bepalen. Uren zonder gegevens krijgen
het gemiddelde van de andere uren.

### Cycli en hun gewoonte

Tapwater en de apparaten draaien in cycli met een herkenbaar verloop: een wasmachine verbruikt
het meest bij het opwarmen aan het begin, een vaatwasser bij het begin en bij het naspoelen. Het
EMS leert per apparaat over de laatste 13 weken:

- **hoe vaak** het per dag start, apart voor werkdagen en het weekend zodra er drie weken
  historie is;
- **op welk uur** meestal (met een beetje spreiding naar de uren ernaast), ook apart voor
  werkdag en weekend;
- **hoe lang** een programma duurt (mediaan) en **hoeveel** het verbruikt.

De recorder bewaart toestanden en 5-minutenstatistieken standaard maar 10 dagen. Daarom houdt het
EMS een eigen logboek bij: per afgelopen programma of tapwaterbeurt één regel met begin, einde,
verbruik en (als je de entiteit kiest) het programma. Dat logboek staat in de opslag van de
integratie, blijft een jaar staan en is een paar kB groot. Wat de recorder nog kent, gaat voor;
wat hij vergeten is, komt uit het logboek. Het aantal regels staat in het verbruiksmodel onder
`in_logboek`.

Het verbruik per keer komt uit een eigen kWh-meter als die er is (voor tapwater: de teller van
de warmtepomp, per vijf minuten). Zonder meter schat het EMS het uit hoeveel meer het huis
verbruikt terwijl het apparaat draait, vergeleken met hetzelfde uur zonder apparaat. Tot er een
paar keer geleerd is, telt het standaardverbruik mee.

Loopt een programma of staat het op uitgestelde start, dan plant het EMS het in van begin tot
eindtijd. Is het vandaag al gestart, dan verwacht het vandaag niet nog een keer zoveel als
gewoonlijk.

De gewoonte zit ook al in de langetermijnmeting van het huis (apparaten) of van de warmtepomp
(tapwater). Daarom gaat het gemiddelde van de gewoonte op elk uur van dat model af, en komt de
eigen voorspelling ervoor in de plaats. Zo telt niets dubbel.

### Wat er nu gebeurt

Wijkt het verbruik het laatste half uur af van het model (er staat iets aan, het huis is leeg),
dan werkt die afwijking even door: voor overig met een halfwaardetijd van een uur, voor
verwarmen van drie uur. De afwijking wordt vergeleken met de verwachting zonder deze
bijsturing, zodat hij zichzelf niet versterkt. `sensor.huis_ems_verbruiksmodel` toont de
bijsturing van nu.

## Vooruitblik

Elk kwartier rekent het EMS de komende 36 uur door en vat de eerste 24 uur samen in een zin:

> Accu vol rond 12:45, daarna nog 6,1 kWh zon over. Accu leeg rond 22:15, daarna 4,3 kWh van
> het net.

`sensor.huis_ems_vooruitblik` heeft dezelfde momenten en hoeveelheden als attributen, en per
apparaat wat het nu doet. `sensor.huis_ems_plan` heeft alles per kwartier, ook per onderdeel.

## Hoe goed is de voorspelling

Per kwartier legt het EMS drie dingen vast:

- **een etmaal vooraf**: zo zag dit kwartier eruit in de voorspelling van 24 uur eerder;
- **vlak vooraf**: de verwachting bij het begin van het kwartier;
- **gemeten**: verbruik, zon, net met en zonder accu, en per onderdeel. Warmtepomp en
  verbruikers met een eigen meter worden gemeten; tapwater volgt uit de warmtepompteller zolang
  "tapwater bezig" aan stond; een apparaat zonder meter wordt geschat met het gemiddelde vermogen
  van een programma.

`sensor.huis_ems_voorspelkwaliteit` geeft over de afgelopen 7 dagen de gemiddelde afwijking per
kwartier, totaal en per onderdeel, een etmaal en vlak vooraf, en hoeveel zon er kwam ten opzichte
van de verwachting. Zit dat laatste structureel ernaast, stel dan de zonfactor bij. Het EMS
bewaart zijn kwartieren 14 dagen.

## De tijdlijn

De overzichtskaart (`custom:huis-ems-card`) en de grafieken met apexcharts-card tonen 24 uur
terug tot 36 uur vooruit op één tijdas:

- doorgetrokken: gemeten;
- gestreept: voorspeld, in het verleden zoals een etmaal vooraf voorspeld (of vlak vooraf, waar
  dat er nog niet is), daarna de voorspelling van nu;
- een punt op "nu": het gemiddelde van het lopende kwartier tot nu toe;
- daaronder het verbruik per onderdeel gestapeld (gemeten vol, vooruit lichter) en de lading
  van de accu.

## Lokaal werken

| Onderdeel | Lokaal | Opmerking |
|---|---|---|
| P1-meter | ja | via de HA-integratie of rechtstreeks |
| Omvormers | meestal | bijvoorbeeld via Modbus |
| Warmtepomp | afhankelijk | hangt af van merk en koppeling |
| Accu uitlezen | afhankelijk | Zendure lokaal via de zenSDK; andere merken via hun eigen integratie |
| Home Connect | nee | de toestand van de apparaten komt via de cloud van de fabrikant |
| Zonvoorspelling, weer | nee | komen per definitie van buiten |

Zonder internet voorspelt het EMS met wat het nog weet: de laatste temperaturen en zonder
zonvoorspelling geen zon.

## Rekenlast

| Werk | Hoe vaak |
|---|---|
| Virtuele accu en tellen per kwartier | elke meting (microseconden) |
| Vooruitblik over 36 uur | elk kwartier en bij elke start of stop van een apparaat |
| Gewoontes van de apparaten | elk uur, of na een start of stop |
| Leren uit een jaar statistieken | elke 6 uur |

Wat het werkelijk kost staat in de attributen van `sensor.huis_ems_verbruiksmodel`. Een Home
Assistant Green heeft hier ruim genoeg aan.

## Waarom het EMS niets meer schakelt

Eerdere versies stuurden de accu en daarna warmtebuffers (vloerverwarming, boiler) op
zonoverschot. Een jaar meetdata liet zien dat dat weinig opleverde boven een accu die zichzelf op
zelfverbruik regelt, en dat de winst vrijwel helemaal afhing van hoe goed het verbruik voorspeld
wordt. Daarom doet het EMS nu alleen dat: zo goed mogelijk voorspellen, en laten zien hoe goed.
Wie later toch wil sturen, heeft de voorspelling per kwartier in `sensor.huis_ems_plan`.
