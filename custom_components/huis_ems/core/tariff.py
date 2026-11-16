"""Opbouw van consumentenprijzen uit de kale day-ahead-prijs (EUR/kWh).

De standaardwaarden zijn schattingen: controleer ze tegen je eigen contract
en de actuele energiebelasting voordat je op de uitkomsten vertrouwt.
"""
from dataclasses import dataclass


@dataclass
class DynamicTariff:
    energy_tax: float = 0.0916   # energiebelasting excl. btw
    buy_fee: float = 0.02        # inkoopopslag leverancier excl. btw
    sell_fee: float = 0.0        # opslag/afdracht op teruglevering
    vat: float = 0.21
    sell_with_tax: bool = False  # True = salderen: teruglevering telt tegen het volle tarief

    def buy(self, spot):
        return (spot + self.energy_tax + self.buy_fee) * (1 + self.vat)

    def sell(self, spot):
        if self.sell_with_tax:
            return self.buy(spot)
        return spot - self.sell_fee


@dataclass
class FixedTariff:
    buy_price: float = 0.27
    sell_price: float = 0.05     # terugleververgoeding minus terugleverkosten

    def buy(self, spot):
        return self.buy_price

    def sell(self, spot):
        return self.sell_price
