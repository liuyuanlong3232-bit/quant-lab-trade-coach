"""Strategies are pure local functions: they return intents, never place orders."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .data import Bar


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    quantity: float
    reason: str


class Strategy(Protocol):
    def on_bar(self, bar: Bar, history: list[Bar]) -> list[OrderIntent]: ...


class SMACrossover:
    """Long-only moving-average crossover; intended for demonstration/testing."""

    def __init__(self, fast: int = 5, slow: int = 20, quantity: float = 1.0):
        if not 1 <= fast < slow or quantity <= 0:
            raise ValueError("require 1 <= fast < slow and positive quantity")
        self.fast, self.slow, self.quantity = fast, slow, quantity
        self.in_market: dict[str, bool] = {}

    def on_bar(self, bar: Bar, history: list[Bar]) -> list[OrderIntent]:
        closes = [item.close for item in history if item.symbol == bar.symbol]
        if len(closes) < self.slow:
            return []
        fast_avg = sum(closes[-self.fast:]) / self.fast
        slow_avg = sum(closes[-self.slow:]) / self.slow
        held = self.in_market.get(bar.symbol, False)
        if fast_avg > slow_avg and not held:
            self.in_market[bar.symbol] = True
            return [OrderIntent(bar.symbol, "buy", self.quantity, "SMA golden cross")]
        if fast_avg < slow_avg and held:
            self.in_market[bar.symbol] = False
            return [OrderIntent(bar.symbol, "sell", self.quantity, "SMA death cross")]
        return []
