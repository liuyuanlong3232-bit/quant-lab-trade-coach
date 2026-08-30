"""Point-in-time loader for the VPS-exported Tushare futures mapping.

The Tushare token stays on the VPS.  Quant-Lab only consumes an append-only
local JSONL contract produced by a reviewed read-only export.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping


CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
MAPPING_VERSION = "tushare_fut_mapping_v1"
ALIASES = {"AG0": "AG.SHF", "AU0": "AU.SHF", "SN0": "SN.SHF", "SC0": "SC.INE"}
ALLOWED_STATUSES = frozenset({"VERIFIED", "MISSING", "INVALID"})
MAX_MAPPING_AGE_DAYS = 4


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("time must be ISO text")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() != timedelta(hours=8):
        raise ValueError("time must use +08:00")
    return result.astimezone(CN_TZ)


def _normalize_date(value: object) -> str:
    text = str(value)
    if re.fullmatch(r"20\d{6}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", text):
        return text
    raise ValueError("as_of_trade_date must be YYYYMMDD or YYYY-MM-DD")


@dataclass(frozen=True)
class TushareMappingRecord:
    product_alias: str
    as_of_trade_date: str
    available_at: datetime
    mapping_ts_code: str | None
    vol: float | None
    oi: float | None
    source: str
    model_version: str
    status: str

    @property
    def mapping_version(self) -> str:
        return self.model_version

    @property
    def verified(self) -> bool:
        return self.status == "VERIFIED" and self.mapping_ts_code is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "product_alias": self.product_alias,
            "as_of_trade_date": self.as_of_trade_date,
            "available_at": self.available_at.isoformat(),
            "mapping_ts_code": self.mapping_ts_code,
            "vol": self.vol,
            "oi": self.oi,
            "source": self.source,
            "model_version": self.model_version,
            "status": self.status,
        }

    def as_contract_mapping(self) -> dict[str, object]:
        return {
            "contract": self.mapping_ts_code,
            "selected_at": self.available_at.isoformat(),
            "open_interest": self.oi,
            "volume": self.vol,
            "roll_reason": "TUSHARE_FUT_MAPPING",
            "source": self.source,
            "source_time": self.available_at.isoformat(),
            "status": self.status,
            "mapping_version": self.model_version,
            "as_of_trade_date": self.as_of_trade_date,
        }


def parse_record(raw: Mapping[str, object]) -> TushareMappingRecord:
    alias = str(raw.get("product_alias", "")).upper()
    if alias not in ALIASES:
        raise ValueError("unsupported product_alias")
    date = _normalize_date(raw.get("as_of_trade_date"))
    available_at = _parse_time(raw.get("available_at"))
    source = str(raw.get("source", ""))
    model_version = str(raw.get("model_version", ""))
    status = str(raw.get("status", ""))
    if source != "TUSHARE" or model_version != MAPPING_VERSION or status not in ALLOWED_STATUSES:
        raise ValueError("invalid source, version, or status")
    mapping = raw.get("mapping_ts_code")
    vol = raw.get("vol")
    oi = raw.get("oi")
    if status == "VERIFIED":
        if not isinstance(mapping, str) or not mapping or mapping.upper() == alias:
            raise ValueError("verified record requires concrete mapping_ts_code")
        product, exchange = ALIASES[alias].split(".", 1)
        suffix = "SHF" if exchange == "SHF" else "INE"
        if not re.fullmatch(rf"{product}\d{{4}}\.{suffix}", mapping.upper()):
            raise ValueError("mapping_ts_code does not match product and exchange")
        if not isinstance(vol, (int, float)) or isinstance(vol, bool) or float(vol) < 0:
            raise ValueError("verified record requires non-negative vol")
        if not isinstance(oi, (int, float)) or isinstance(oi, bool) or float(oi) < 0:
            raise ValueError("verified record requires non-negative oi")
    return TushareMappingRecord(alias, date, available_at, mapping, float(vol) if vol is not None else None, float(oi) if oi is not None else None, source, model_version, status)


class TushareMappingStore:
    """Read-only PIT access to local append-only mapping JSONL."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def records(self) -> tuple[TushareMappingRecord, ...]:
        if not self.path.is_file():
            return ()
        output: list[TushareMappingRecord] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    return ()
                output.append(parse_record(value))
            except (json.JSONDecodeError, ValueError, TypeError):
                return ()
        return tuple(output)

    def latest(self, alias: str, *, observed_at: datetime) -> TushareMappingRecord | None:
        alias = alias.upper()
        if alias not in ALIASES:
            return None
        if observed_at.tzinfo is None or observed_at.utcoffset() != timedelta(hours=8):
            raise ValueError("observed_at must use +08:00")
        eligible = [
            row for row in self.records()
            if row.product_alias == alias
            and row.available_at <= observed_at
            and row.as_of_trade_date <= observed_at.date().isoformat()
            and row.status == "VERIFIED"
            and 0 <= (observed_at.date() - datetime.fromisoformat(row.as_of_trade_date).date()).days <= MAX_MAPPING_AGE_DAYS
        ]
        if not eligible:
            return None
        return max(eligible, key=lambda row: (row.available_at, row.as_of_trade_date))


class TushareMappingProvider:
    """Adapter shape consumed by ``ProbeRunner``."""

    def __init__(self, path: str | Path):
        self.store = TushareMappingStore(path)

    def fetch(self, symbol: str, *, observed_at: datetime) -> TushareMappingRecord:
        row = self.store.latest(symbol, observed_at=observed_at)
        if row is not None:
            return row
        return TushareMappingRecord(symbol, observed_at.date().isoformat(), observed_at, None, None, None, "TUSHARE", MAPPING_VERSION, "MISSING")


def append_records(path: str | Path, records: Iterable[Mapping[str, object]]) -> int:
    """Append validated records under a local lock; never replace history."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    validated = [parse_record(record) for record in records]
    if not validated:
        return 0
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            # Validate the existing append-only file while holding the lock.  A
            # corrupt line must not be silently treated as an empty history,
            # otherwise a retry could hide the original evidence problem.
            existing: dict[tuple[str, str], TushareMappingRecord] = {}
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        raw = json.loads(line)
                        parsed = parse_record(raw)
                    except (json.JSONDecodeError, TypeError, ValueError) as exc:
                        raise ValueError("existing mapping JSONL is corrupt") from exc
                    key = (parsed.as_of_trade_date, parsed.product_alias)
                    previous = existing.get(key)
                    if previous is not None and _mapping_facts(previous) != _mapping_facts(parsed):
                        raise ValueError("existing mapping JSONL has conflicting duplicate key")
                    existing[key] = parsed

            to_append: list[TushareMappingRecord] = []
            for parsed in validated:
                key = (parsed.as_of_trade_date, parsed.product_alias)
                previous = existing.get(key)
                if previous is not None:
                    if _mapping_facts(previous) != _mapping_facts(parsed):
                        raise ValueError("mapping duplicate key conflicts with existing record")
                    continue
                existing[key] = parsed
                to_append.append(parsed)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                original_size = handle.tell()
                try:
                    for record in to_append:
                        handle.write(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                except Exception:
                    # Do not leave a logically incomplete four-product batch
                    # if the local append fails after one record.  The lock is
                    # still held while truncating, so a retry is deterministic.
                    handle.seek(original_size)
                    handle.truncate()
                    handle.flush()
                    os.fsync(handle.fileno())
                    raise
        finally:
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return len(to_append)


def _mapping_facts(record: TushareMappingRecord) -> tuple[object, ...]:
    """Fields that must remain stable when the same trade date is retried.

    ``available_at`` is observation metadata and naturally changes on a retry;
    it is not allowed to turn an otherwise identical append into a conflict.
    """

    return (
        record.product_alias,
        record.as_of_trade_date,
        record.mapping_ts_code,
        record.vol,
        record.oi,
        record.source,
        record.model_version,
        record.status,
    )
