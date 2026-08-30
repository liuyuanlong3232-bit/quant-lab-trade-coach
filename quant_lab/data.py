"""Strict, local CSV market-data input.

The module deliberately has no HTTP, broker, or data-vendor integration. A run
can therefore only consume a file the operator explicitly supplies.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator


class DataContractError(ValueError):
    """Raised when a market-data file violates the v0.1 contract."""


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float


REQUIRED_COLUMNS = ("timestamp", "symbol", "open", "high", "low", "close", "volume")


def _number(raw: str, field: str, row: int) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise DataContractError(f"row {row}: {field} must be numeric") from exc
    if value != value or value in (float("inf"), float("-inf")):
        raise DataContractError(f"row {row}: {field} must be finite")
    return value


class CSVMarketData:
    """Load and validate local OHLCV CSV data, preserving source provenance."""

    def __init__(self, path: str | Path):
        candidate = Path(path).expanduser()
        raw_path = str(path)
        if candidate.as_posix().startswith(("http://", "https://")) or "://" in raw_path:
            raise DataContractError("only local files are allowed; URLs are forbidden")
        # UNC paths can reach a remote host even though they look like files.
        if raw_path.startswith(("\\\\", "//")) or candidate.anchor.startswith(("\\\\", "//")):
            raise DataContractError("network/UNC paths are forbidden")
        self.path = candidate.resolve()
        if self.path.suffix.lower() != ".csv":
            raise DataContractError("market data must be a .csv file")
        if not self.path.is_file():
            raise FileNotFoundError(self.path)

    @property
    def sha256(self) -> str:
        digest = hashlib.sha256()
        with self.path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def read(self) -> list[Bar]:
        bars = list(self)
        if not bars:
            raise DataContractError("market data CSV is empty")
        return bars

    def __iter__(self) -> Iterator[Bar]:
        with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise DataContractError("CSV must have a header")
            fields = {name.strip().lower() for name in reader.fieldnames if name}
            missing = [name for name in REQUIRED_COLUMNS if name not in fields]
            if missing:
                raise DataContractError(f"missing columns: {', '.join(missing)}")
            previous: tuple[datetime, str] | None = None
            for row_number, row in enumerate(reader, start=2):
                # Normalize header spelling while retaining a strict, explicit contract.
                normalized = {(key or "").strip().lower(): value for key, value in row.items() if key}
                try:
                    stamp = datetime.fromisoformat(normalized["timestamp"].strip())
                except (AttributeError, TypeError, ValueError) as exc:
                    raise DataContractError(f"row {row_number}: invalid timestamp") from exc
                except KeyError as exc:
                    raise DataContractError(f"row {row_number}: missing timestamp") from exc
                try:
                    symbol = normalized["symbol"].strip()
                except (AttributeError, KeyError) as exc:
                    raise DataContractError(f"row {row_number}: symbol is required") from exc
                if not symbol:
                    raise DataContractError(f"row {row_number}: symbol is required")
                try:
                    values = {field: _number(normalized[field], field, row_number) for field in REQUIRED_COLUMNS[2:]}
                except KeyError as exc:
                    raise DataContractError(f"row {row_number}: missing column {exc.args[0]}") from exc
                if values["low"] > values["high"] or not (
                    values["low"] <= values["open"] <= values["high"]
                    and values["low"] <= values["close"] <= values["high"]
                ):
                    raise DataContractError(f"row {row_number}: OHLC violates low <= open/close <= high")
                if values["volume"] < 0:
                    raise DataContractError(f"row {row_number}: volume cannot be negative")
                key = (stamp, symbol)
                if previous is not None and key <= previous:
                    raise DataContractError("rows must be strictly ordered by timestamp and symbol")
                previous = key
                yield Bar(timestamp=stamp, symbol=symbol, **values)


def bars_for_symbol(bars: Iterable[Bar], symbol: str) -> list[Bar]:
    selected = [bar for bar in bars if bar.symbol == symbol]
    if not selected:
        raise DataContractError(f"no bars found for symbol {symbol!r}")
    return selected
