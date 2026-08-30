"""Quant-Lab v0.1: local-only research and simulated execution primitives."""

__version__ = "0.1.0"

from .data import Bar, CSVMarketData, DataContractError
from .engine import BacktestEngine, BacktestResult
from .portfolio import PortfolioConfig, SimulatedPortfolio

__all__ = [
    "Bar",
    "CSVMarketData",
    "DataContractError",
    "BacktestEngine",
    "BacktestResult",
    "PortfolioConfig",
    "SimulatedPortfolio",
]
