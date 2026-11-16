# huis-ems

Energiemanagement voor thuis als Home Assistant-integratie. Doel: zoveel mogelijk eigen
zonnestroom zelf gebruiken. Geen handel en geen centrale schakel: de accu regelt zichzelf op
zelfverbruik, de warmtepomp op zijn stooklijn. Het EMS kijkt vooruit en verschuift verbruik
naar momenten met zonoverschot.

## Wat het doet

**Vooruitkijken.** Elk kwartier rekent het EMS de komende 36 uur door:

- **verbruik** per onderdeel (rest van het huis, warmtepomp, hottub), geleerd uit de
  langetermijnstatistieken van je bestaande meters en de weersverwachting;
- **zon** uit de voorspelling die al in je Energie-dashboard staat;
- **de accu**: vanaf de lading van nu, zoals hij op zelfverbruik gaat laden en ontladen;
- **prijzen**: vast of dynamisch, om de dure uren aan te wijzen.

Daaruit volgt bijvoorbeeld: "Accu vol rond 12:45, daarna nog 6,1 kWh zon over. Accu leeg rond
22:15, daarna 4,3 kWh van het net (1,5 kWh in dure uren)."

**Verbruik verschuiven.** Is de accu vol genoeg en gaat er zon naar het net, dan mogen
warmtebuffers die zon opnemen:

- de **vloerverwarming**: de thermostaat tijdelijk een graad hoger, zodat de warmtepomp blijft
  doorlopen in plaats van af te slaan op zoninstraling. De stooklijn blijft ongemoeid;
- de **boiler**: het element aan zolang er genoeg over is; de thermostaat van het vat blijft
  de grens.

De accu gaat altijd voor en voedt nooit een buffer. Standaard geeft het EMS alleen advies; het
schakelt pas als je `switch.huis_ems_verbruik_verschuiven` aanzet. Zie `docs/werking.md`.

**Meten hoe goed het voorspelt.** Per kwartier legt het EMS vast wat het verwachtte en wat er
gebeurde. `sensor.huis_ems_voorspelkwaliteit` vat de afgelopen week samen.

## Zien waarom

- `sensor.huis_ems_vooruitblik`: de zin hierboven, met de momenten en hoeveelheden als attributen.
- `sensor.huis_ems_plan`: per kwartier vooruit zon, verbruik, warmtepompdeel, accu, net, lading
  en prijs.
- `sensor.huis_ems_verschuifbaar_verbruik`: per buffer de stand en de reden.
- `sensor.huis_ems_terugblik`: de laatste 48 uur per kwartier, verwacht tegenover werkelijk.
- `sensor.huis_ems_verbruiksmodel`: wat het EMS over je verbruik en warmtepomp geleerd heeft.

`homeassistant/dashboard.yaml` zet dit in één dashboard. Bovenaan staat de overzichtskaart
die met de integratie meekomt (`custom:huis-ems-card`): de vooruitblik, de kerncijfers, de
buffers en een tijdlijn van 24 uur terug tot 36 uur vooruit, met verwacht (gestreept) en
werkelijk (doorgetrokken). Een tweede tabblad geeft alles per uur als tabel.

## Simuleren zonder accu

Voeg een **virtuele accu** toe. Die bestaat alleen in de berekening, maar volgt je echte
P1-meter zoals een accu op zelfverbruik. Je ziet zijn lading en vermogen, het netvermogen met
en zonder accu, wat hij vandaag en in totaal bespaard zou hebben, en wat de buffers ermee doen.

## Verbruik voorspellen

Elke 6 uur leert het EMS opnieuw uit het laatste jaar aan statistieken. Per onderdeel (rest van
het huis, warmtepomp, elke verbruiker die je toevoegt) leert het een profiel per uur van de dag
en per uur hoeveel het verbruik stijgt per graad kou. Daarbovenop:

- de laatste week stelt het niveau bij;
- werkdag en weekend krijgen alleen een eigen profiel als dat op je eigen gegevens minstens 3%
  beter voorspelt. Het EMS toetst dat zelf op weken die het niet heeft meegeleerd;
- heeft een verbruiker een thermostaat (ingestelde en gemeten temperatuur), dan leert het EMS
  hoeveel hij verbruikt als hij op temperatuur is en hoeveel als hij warmte vraagt, en stelt het
  de eerstkomende uren daarop bij.

`sensor.huis_ems_verbruiksmodel` toont per onderdeel wat er geleerd is.

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
3. Via **Configureren** stel je de rest in: tarieven, warmtepomp en weer, accu's en de
   buffers (Verschuiven: thermostaat of schakelaar). Elke parameter heeft daar een uitleg.
4. Plak `homeassistant/dashboard.yaml` in een nieuw dashboard. Er is geen HACS voor nodig.
5. Heb je [apexcharts-card](https://github.com/RomRider/apexcharts-card), plak dan ook
   `homeassistant/grafieken.yaml` in een dashboard: zon en verbruik, net, lading van de accu,
   verschuiven en prijs, van 24 uur terug tot 36 uur vooruit onder elkaar op één tijdas.
   Gemeten is doorgetrokken, verwacht gestreept of lichter.

Alles is achteraf te wijzigen via Configureren; er zijn geen YAML-bestanden nodig.

## Bediening

| Entiteit | Betekenis |
|---|---|
| `switch.huis_ems_verbruik_verschuiven` | buffers echt schakelen (standaard uit: alleen advies) |
| `binary_sensor.huis_ems_<buffer>_vrijgegeven` | per buffer: mag hij nu zon opnemen, met de reden |
| `number.huis_ems_zonvoorspelling_factor` | bijsturen als de zonvoorspelling structureel afwijkt |

De parameters per buffer (minimale lading van de accu, tijdvenster, temperatuurgrens, hoeveel
graden hoger, wachttijden, minimale draai- en rusttijd) stel je in onder Configureren.

## Prijzen: bekend, geschat of terugval

Het EMS gebruikt een dynamische prijs alleen als de prijssensor het huidige kwartier echt
dekt. Een oude of onderbroken reeks wordt niet hergebruikt; dan rekent het EMS met het vaste
tarief. Voorbij de laatste bekende prijs herhaalt het hooguit één etmaal, gemarkeerd als
geschat. Het plan
(`prijs_herkomst`) en het dashboard laten per kwartier zien wat bekend en wat geschat is.

## Jaarsimulatie op je P1-export

```
python tools/fetch_prices.py 2025-10-31 2026-11-02 data/prijzen.csv
python tools/simulate.py --p1 data/p1.csv --prices data/prijzen.csv --capacity 8 --power 3
```

`data/` staat in `.gitignore`: meetdata komt niet in de repository.

Standaard kent de simulator verbruik en zon exact; dat is een bovengrens. Met `--replay` plant
hij daarnaast elk uur opnieuw met alleen wat op dat moment bekend was (gepubliceerde prijzen en
een voorspelling uit het verleden) en voert hij de opdracht uit tegen wat er werkelijk gebeurde.
Alle varianten worden op dezelfde manier afgerekend, inclusief de lading die aan het eind
overblijft.

Op een meetjaar van één huishouden leverde slim plannen van de accu met een realistische
voorspelling weinig op boven gewoon zelfverbruik: enkele tientallen euro's per jaar. Daarom
regelt de accu in Home Assistant zichzelf en richt het EMS zich op voorspellen en op het
verschuiven van verbruik. De planner zit nog in de simulator om
varianten te vergelijken.

### Wat leveren de buffers op?

```
python tools/buffers.py --p1 data/p1.csv --hp data/wp_jaar.csv --prices data/prijzen.csv
```

Rekent per kwartier de accu op zelfverbruik door, met de vloer (thermostaat hoger tussen 10 en 16
uur) en de boiler (element 2 kW) zoals het EMS ze schakelt, en vergelijkt de netkosten met en
zonder buffers bij dezelfde accu. Op een meetjaar van één huishouden met een accu van 8 kWh
leverden vloer en boiler samen enkele tientallen euro's per jaar op. De boiler het minst: in de
zomer is de tapwatervraag klein en dekt de accu die al. Alle aannames (tapwatervraag,
COP, hoeveel warmte de vloer kan bufferen) zijn met vlaggen te variëren.

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
bijvoorbeeld "boiler: gaat uit als de accu hem zou moeten voeden".

De tweede test stelt de integratie in via dezelfde schermen als de gebruikersinterface, met een
virtuele accu, een boiler, een thermostaat (generic_thermostat als stand-in) en een nagebootste
Zendure die alleen gelezen mag worden. Als ze in `data/` staan, laadt hij je eigen meetgegevens
als historie in om te controleren wat het EMS ervan leert.

## Opbouw

```
custom_components/huis_ems/        de integratie
  core/                            rekenkern zonder afhankelijkheden
    balance.py                     vooruitblik: de accu op zelfverbruik doorgerekend
    buffers.py                     voorwaarden voor warmtebuffers, overschot na de accu
    stage2.py                      schakelen op overschot: wachttijden, draai- en rusttijd, volgorde
    loadmodel.py                   verbruik per onderdeel als functie van uur en temperatuur
    fleet.py                       accu's optellen, virtuele accu
    optimizer.py                   zelfverbruik en (voor de simulator) het goedkoopste schema
    forecast.py, tariff.py         prijzen en voorspellingen inlezen
    timeline.py                    kwartieren op een UTC-tijdas
  coordinator.py                   gegevens uit HA, vooruitblik, buffers schakelen
  meter.py                         P1-meter rechtstreeks of via HA
  drivers.py                       Zendure lokaal uitlezen
  config_flow.py                   instelschermen
homeassistant/dashboard.yaml       dashboard
homeassistant/grafieken.yaml       grafieken met apexcharts-card
tools/                             jaarsimulatie, benchmark van de rekenlast, pictogram tekenen
docs/werking.md                    vooruitblik, verschuiven, veiligheid, rekenlast
docs/accu-adapters.md              accu's uitlezen
```

## Aannames die je moet controleren

- Energiebelasting, opslagen en btw zijn schattingen; stel ze in onder Tarieven.
- Rendement 86% heen en terug is een aanname voor AC-gekoppelde accu's; het telt alleen mee
  in de vooruitblik.
- De terugleverprijs na het salderen verschilt per leverancier.
