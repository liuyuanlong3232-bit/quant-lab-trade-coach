"""Simulation-only portfolio and fill accounting."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from typing import Literal


Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class PortfolioConfig:
    initial_cash: float = 100_000.0
    commission_rate: float = 0.0003
    slippage_bps: float = 0.0
    allow_short: bool = False


@dataclass(frozen=True)
class Fill:
    timestamp: datetime
    symbol: str
    side: Side
    quantity: float
    price: float
    commission: float
    reason: str = ""


@dataclass
class SimulatedPortfolio:
    config: PortfolioConfig = field(default_factory=PortfolioConfig)
    cash: float = field(init=False)
    positions: dict[str, float] = field(default_factory=dict, init=False)
    fills: list[Fill] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        values = (self.config.initial_cash, self.config.commission_rate, self.config.slippage_bps)
        if not all(isfinite(value) for value in values) or self.config.initial_cash <= 0 or self.config.commission_rate < 0 or self.config.slippage_bps < 0:
            raise ValueError("invalid portfolio configuration")
        self.cash = self.config.initial_cash

    def execute(self, *, timestamp: datetime, symbol: str, side: Side, quantity: float, price: float, reason: str = "") -> Fill:
        if not symbol.strip() or side not in ("buy", "sell") or not isfinite(quantity) or not isfinite(price) or quantity <= 0 or price <= 0:
            raise ValueError("side, quantity, and price are invalid")
        signed = quantity if side == "buy" else -quantity
        current = self.positions.get(symbol, 0.0)
        new_position = current + signed
        if not self.config.allow_short and new_position < -1e-12:
            raise ValueError("short selling is disabled")
        effective_price = price * (1 + self.config.slippage_bps / 10000 * (1 if side == "buy" else -1))
        notional = quantity * effective_price
        commission = notional * self.config.commission_rate
        cash_delta = -(notional + commission) if side == "buy" else notional - commission
        if self.cash + cash_delta < -1e-9:
            raise ValueError("insufficient simulated cash")
        self.cash += cash_delta
        self.positions[symbol] = new_position
        fill = Fill(timestamp, symbol, side, quantity, effective_price, commission, reason)
        self.fills.append(fill)
        return fill

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + sum(quantity * prices.get(symbol, 0.0) for symbol, quantity in self.positions.items())
