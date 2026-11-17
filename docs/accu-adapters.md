# Accu's koppelen

Het EMS stuurt accu's niet aan. Stel de accu in zijn eigen app in op zelfverbruik (nul op de
meter). Het EMS leest alleen de lading en het vermogen uit, voor twee dingen:

- de vooruitblik: vanaf de lading van nu doorrekenen wanneer de accu vol en leeg raakt;
- het verbruiksmodel: wat de accu laadt en ontlaadt hoort niet bij het huisverbruik.

Bij Configureren > Accu toevoegen kies je de soort accu.

## Virtueel

Bestaat alleen in de berekening en volgt je echte P1-meter zoals een accu op zelfverbruik:
laden met overschot, ontladen bij verbruik, binnen zijn vermogen en grenzen. Bedoeld om te zien
wat een accu zou doen voordat je er een koopt.

## Zendure, lokaal

Het EMS leest de accu uit via zijn lokale API (`GET /properties/report`), binnen je eigen
netwerk. Er is geen andere integratie nodig en geen cloud.

- **Instellen:** alleen het IP-adres. Lading, vermogen en serienummer leest het EMS elke 3
  seconden uit; het maakt er sensoren van (`... lading`, `... vermogen`).
- **In de Zendure-app:** zet de lokale API aan en laat de accu zelf op zelfverbruik regelen met
  zijn eigen meterkoppeling.
- Het EMS schrijft nooit naar de accu. De rooktest controleert dat.

De koppeling is gebouwd op de documentatie van Zendure en getest tegen een nagebootste accu,
niet tegen een echt apparaat. De SolarFlow 3000 Mix AC+ staat (nog) niet in Zendure's eigen
lijst van ondersteunde modellen; de 2400-serie wel. Controleer na aanschaf of lading en vermogen
kloppen.

Bron: https://github.com/Zendure/zenSDK

## Andere accu, via entiteiten

Voor elke accu met een eigen Home Assistant-integratie, zoals die van Anker. Je vult in:

- de laadtoestand in procenten (verplicht);
- het actuele vermogen (aanbevolen; + is laden, of zet "omgekeerd" aan).

Een laadtoestand die langer dan een kwartier niet is gemeld telt niet. Dan rekent de vooruitblik
zonder accu en meldt hij dat.
