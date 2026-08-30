"""Leakage-aware local backtest engine with next-bar-open fills."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .audit import AuditLog
from .data import Bar, CSVMarketData
from .portfolio import PortfolioConfig, SimulatedPortfolio
from .strategy import OrderIntent, Strategy


@dataclass(frozen=True)
class EquityPoint:
    timestamp: datetime
    equity: float


@dataclass(frozen=True)
class BacktestResult:
    initial_cash: float
    final_equity: float
    fills: tuple
    equity_curve: tuple[EquityPoint, ...]


class BacktestEngine:
    def __init__(self, portfolio_config: PortfolioConfig | None = None, audit: AuditLog | None = None):
        self.portfolio_config = portfolio_config or PortfolioConfig()
        self.audit = audit

    def run(self, data: CSVMarketData | list[Bar], strategy: Strategy) -> BacktestResult:
        source = data if isinstance(data, CSVMarketData) else None
        bars = data.read() if source else list(data)
        if not bars:
            raise ValueError("backtest requires at least one bar")
        portfolio = SimulatedPortfolio(self.portfolio_config)
        if self.audit:
            self.audit.append("backtest_started", source=str(source.path) if source else "in_memory", source_sha256=source.sha256 if source else None)
        history: list[Bar] = []
        pending: list[OrderIntent] = []
        curve: list[EquityPoint] = []
        prices: dict[str, float] = {}
        for bar in bars:
            remaining: list[OrderIntent] = []
            for intent in pending:
                if intent.symbol != bar.symbol:
                    remaining.append(intent)
                    continue
                fill = portfolio.execute(timestamp=bar.timestamp, symbol=bar.symbol, side=intent.side, quantity=intent.quantity, price=bar.open, reason=intent.reason)
                if self.audit:
                    self.audit.append("simulated_fill", **fill.__dict__)
            pending = remaining
            history.append(bar)
            pending.extend(strategy.on_bar(bar, history.copy()))
            prices[bar.symbol] = bar.close
            curve.append(EquityPoint(bar.timestamp, portfolio.equity(prices)))
        result = BacktestResult(self.portfolio_config.initial_cash, portfolio.equity(prices), tuple(portfolio.fills), tuple(curve))
        if self.audit:
            self.audit.append("backtest_completed", final_equity=result.final_equity, fill_count=len(result.fills))
        return result
