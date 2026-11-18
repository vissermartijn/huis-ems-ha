# huis-ems

Verbruiksvoorspelling voor thuis als Home Assistant-integratie. Het EMS voorspelt per kwartier,
36 uur vooruit, wat elk onderdeel van het huis gaat verbruiken, wat de zonnepanelen opwekken en
wat de accu daarmee doet. Het regelt niets: de accu regelt zichzelf op zelfverbruik, de
warmtepomp op zijn stooklijn.

## Wat het voorspelt

| Onderdeel | Waaruit |
|---|---|
| **warmtepomp verwarmen** | eigen kWh-teller, buitentemperatuur en weersverwachting: verbruik per uur van de dag plus extra per graad kou |
| **warmtepomp tapwater** | dezelfde teller en de melding "tapwater bezig": hoe vaak, wanneer en hoeveel per keer, en een lopende cyclus |
| **verbruikers met een eigen meter**, zoals een hottub | eigen profiel per uur en temperatuur, bijgesteld op de thermostaat |
| **apparaten met programma's** (Home Connect: wasmachine, vaatwasser, droger) | wat loopt of op uitgestelde start staat, tot de eindtijd; daarnaast de gewoonte van de afgelopen maanden, per werkdag en weekend |
| **overig** | het patroon per uur van de dag en de temperatuur, en wat er het laatste half uur anders ging |
| **zon** | de voorspelling die al in je Energie-dashboard staat |
| **accu** | vanaf de lading van nu, zoals hij op zelfverbruik laadt en ontlaadt: wanneer vol, wanneer leeg, hoeveel van het net |

Daaruit volgt bijvoorbeeld: "Accu vol rond 12:45, daarna nog 6,1 kWh zon over. Accu leeg rond
22:15, daarna 4,3 kWh van het net." Zie `docs/werking.md` voor hoe elk onderdeel geleerd wordt.

## Zien hoe goed het is

Per kwartier legt het EMS vast wat het **een etmaal vooraf** en **vlak vooraf** verwachtte, en wat
er **gemeten** is, totaal en per onderdeel. De overzichtskaart toont dat op één tijdlijn van 24 uur
terug tot 36 uur vooruit: gemeten doorgetrokken, voorspeld gestreept, het lopende kwartier als
punt op "nu", en daaronder het verbruik per onderdeel en de lading van de accu.

- `sensor.huis_ems_vooruitblik`: de zin hierboven, met de momenten, hoeveelheden en de stand van
  elk apparaat als attributen.
- `sensor.huis_ems_plan`: per kwartier vooruit zon, verbruik, verbruik per onderdeel, accu, net
  en lading.
- `sensor.huis_ems_terugblik`: de laatste 48 uur per kwartier, voorspeld tegenover gemeten.
- `sensor.huis_ems_verbruik_dit_kwartier`, `sensor.huis_ems_zon_dit_kwartier`: het lopende
  kwartier tot nu toe.
- `sensor.huis_ems_voorspelkwaliteit`: de gemiddelde afwijking over de afgelopen week, een etmaal
  en vlak vooraf, ook per onderdeel.
- `sensor.huis_ems_verbruiksmodel`: wat het EMS geleerd heeft, per onderdeel en per apparaat.

## Simuleren zonder accu

Voeg een **virtuele accu** toe. Die bestaat alleen in de berekening, maar volgt je echte
P1-meter zoals een accu op zelfverbruik. Je ziet zijn lading en vermogen, het netvermogen met en
zonder accu, en de lading in de vooruitblik.

## Accu's

Een Zendure-accu leest het EMS lokaal uit via zijn API; andere accu's via de entiteiten van hun
eigen integratie. Meerdere accu's tellen samen op. Het EMS schrijft nooit naar een accu. Zie
`docs/accu-adapters.md`.

## Installeren

1. Zet de integratie in Home Assistant, op een van twee manieren:
   - **Via de Marketplace** (ingebouwd sinds Home Assistant 2026.11, voorheen HACS): open de
     Marketplace, kies **Voeg toe via link** en vul `https://github.com/vissermartijn/huis-ems-ha`
     in. De eerste keer vraagt de Marketplace een GitHub-koppeling (een code op
     github.com/login/device). Open daarna Huis EMS en kies **Installeren**; een herstart is niet
     nodig. Nieuwe versies verschijnen vanzelf als update.
   - **Met de hand**: kopieer de map `custom_components/huis_ems` naar
     `/config/custom_components/` (bijvoorbeeld via de add-on Samba share) en herstart Home
     Assistant. Bijwerken gaat op dezelfde manier.
2. Instellingen > Apparaten en diensten > Integratie toevoegen > **Huis EMS**. De meters uit
   je Energie-dashboard worden voorgesteld.
3. Via **Configureren** stel je de rest in: warmtepomp en weer (met de melding "tapwater
   bezig"), accu's, verbruikers met een eigen meter en apparaten met programma's. Elke parameter
   heeft daar een uitleg.
4. Plak `homeassistant/dashboard.yaml` in een nieuw dashboard. Er is geen HACS voor nodig.
5. Heb je [apexcharts-card](https://github.com/RomRider/apexcharts-card), dan zijn er ook
   grafieken: verbruik en opwek, verbruik per onderdeel en lading van de accu onder elkaar op
   één tijdas. Twee manieren:
   - als eigen dashboard: maak een nieuw dashboard, open de **ruwe configuratie-editor** en
     plak `homeassistant/grafieken.yaml` erin;
   - als kaart in een bestaand dashboard: **Kaart toevoegen → Handmatig** en plak
     `homeassistant/grafieken-kaart.yaml` erin. (Plak je het hele dashboard in een kaart, dan
     meldt Home Assistant "geen type opgegeven".)

   Pas de namen van de onderdelen aan als je ze anders genoemd hebt.

Alles is achteraf te wijzigen via Configureren; er zijn geen YAML-bestanden nodig.

### Apparaten via Home Connect

Kies bij **Apparaat toevoegen** de bedrijfstoestand (in een Nederlandstalige HA
`sensor.<apparaat>_status`, in een Engelstalige `sensor.<apparaat>_operation_state`) en de
eindtijd (`sensor.<apparaat>_programma_eindtijd` of `..._program_finish_time`). Loopt een
programma (`run`, `pause`, `actionrequired`) of staat het op uitgestelde start (`delayedstart`),
dan plant het EMS het verbruik in. Is het apparaat onbereikbaar, dan telt het als uit: veel
apparaten gaan van de wifi zodra ze uit staan. Zonder eigen
kWh-meter schat het EMS het verbruik per keer uit hoeveel meer het huis verbruikt terwijl het
apparaat draait. Een binaire sensor die aan is zolang het apparaat draait, werkt ook.

## Bediening

| Entiteit | Betekenis |
|---|---|
| `number.huis_ems_zonvoorspelling_factor` | bijsturen als de zonvoorspelling structureel afwijkt |

## Testen

```
python -m unittest tests.test_core   # rekenkern
python tests/apex_generators.py      # rekencode in de apexcharts-grafieken (heeft node nodig)
sh tests/ha_test.sh                  # wegwerp-Home Assistant in Docker, doorloopt alles (ruim tien minuten)
sh tests/ha_test.sh --serve          # idem, en laat hem daarna draaien op http://localhost:8124
```

De Home Assistant-test draait tegen een vaste, geteste versie: 2026.11.1. Een andere kies je met
`HA_VERSION=2026.12.0 sh tests/ha_test.sh`. GitHub Actions draait de kerntests bij elke push en de
Home Assistant-test elke maandagnacht of met de hand. Een mislukte controle noemt het scenario,
bijvoorbeeld "vaatwasser: niet vóór de uitgestelde start".

De tweede test stelt de integratie in via dezelfde schermen als de gebruikersinterface, met een
virtuele accu, tapwater van de warmtepomp, een vaatwasser die zich gedraagt als Home Connect en
een nagebootste Zendure die alleen gelezen mag worden. Als ze in `data/` staan, laadt hij je eigen
meetgegevens als historie in om te controleren wat het EMS ervan leert. `data/` staat in
`.gitignore`: meetdata komt niet in de repository.

## Opbouw

```
custom_components/huis_ems/        de integratie
  core/                            rekenkern zonder afhankelijkheden
    loadmodel.py                   verbruik per onderdeel als functie van uur en temperatuur
    cycles.py                      cycli: tapwater en apparaten, gewoonte en aangekondigde programma's
    battery.py                     accu's optellen, virtuele accu, vooruitblik op zelfverbruik
    series.py                      kWh-tellers samenvoegen
    forecast.py, timeline.py       zonvoorspelling inlezen, kwartieren op een UTC-tijdas
  coordinator.py                   gegevens uit HA, leren, voorspellen, vastleggen
  meter.py                         P1-meter rechtstreeks of via HA
  drivers.py                       Zendure lokaal uitlezen
  config_flow.py                   instelschermen
  frontend/huis-ems-card.js        overzichtskaart met tijdlijn
homeassistant/dashboard.yaml       dashboard
homeassistant/grafieken.yaml       grafieken met apexcharts-card (een heel dashboard)
homeassistant/grafieken-kaart.yaml dezelfde grafieken als één kaart
tools/make_icon.py                 pictogram tekenen
docs/werking.md                    hoe elk onderdeel voorspeld wordt
docs/accu-adapters.md              accu's uitlezen
```

## Aannames die je moet controleren

- De standaardverdeling van het verbruik over een programma (wasmachine vooral aan het begin,
  vaatwasser aan het begin en bij het naspoelen) is een aanname; het totaal per keer leert het
  EMS zelf.
- Rendement 86% heen en terug is een aanname voor AC-gekoppelde accu's; het telt alleen mee in
  de vooruitblik.
- De recorder van Home Assistant bewaart standaard 10 dagen. Voor de gewoontes van de apparaten
  en het tapwater houdt het EMS daarom een eigen logboek bij (een jaar, een paar kB); de
  recorder hoeft niet langer te bewaren. Vlak na de installatie kent het EMS dus alleen de
  laatste 10 dagen, en het verschil tussen werkdag en weekend pas na drie weken.
