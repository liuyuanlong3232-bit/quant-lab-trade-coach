"""Quant-Lab Personal Trade Coach v0.1.

This module is the local, evidence-first product layer described in
``计划按这个执行.md``.  It deliberately keeps four concerns separate:

* real observations and their source/freshness state;
* deterministic regime, risk and position-range calculations;
* append-only account, memory, narrative, advice, event and diary records;
* a localhost HTTP service consumed by the Chinese React terminal.

No function in this module places an order, contacts a broker, or turns a
missing source into a neutral value.  Network collection is explicit through
``RealMarketCollector.refresh`` or the ``POST /api/trade-coach/refresh`` route;
initialisation only imports already-local evidence.
"""

from __future__ import annotations

import csv
import asyncio
from concurrent.futures import ThreadPoolExecutor
from collections import deque
import ctypes
import ctypes.wintypes
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence
from uuid import uuid4

try:
    import aiohttp
except ImportError:  # optional until QQ Bot is configured
    aiohttp = None


CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
UTC = timezone.utc
SCHEMA_VERSION = "quant_lab_trade_coach_v0.1"
STATUSES = frozenset({"READY", "MISSING", "STALE", "CONFLICT", "UNKNOWN"})
ADVICE_ACTIONS = frozenset({"HOLD", "ADD_IN_STEPS", "REDUCE_IN_STEPS", "WAIT", "EXIT_MAJOR_RISK"})
AI_SCHEMA_VERSION = "quant_lab_ai_mentor_v1"
AI_PROVIDER_NAME = "multi_provider"
AI_PROVIDER_ORDER = ("deepseek", "mimo")
NOTIFICATION_SCHEMA_VERSION = "quant_lab_notification_v1"

QQBOT_SECRET_TARGET = "QuantLab/PersonalTradeCoach/QQBot/AppSecret/v1"

class WindowsCredentialBackend:
    """Credential Manager backend; never falls back to a file or DPAPI."""
    def __init__(self, target: str = QQBOT_SECRET_TARGET): self.target = target
    def _api(self, cred_type=None):
        api = ctypes.WinDLL("advapi32", use_last_error=True)
        api.CredWriteW.argtypes = [ctypes.POINTER(cred_type or self._credential_type()), ctypes.wintypes.DWORD]
        api.CredWriteW.restype = ctypes.wintypes.BOOL
        api.CredReadW.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        api.CredReadW.restype = ctypes.wintypes.BOOL
        api.CredDeleteW.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD]
        api.CredDeleteW.restype = ctypes.wintypes.BOOL
        api.CredFree.argtypes = [ctypes.c_void_p]
        api.CredFree.restype = None
        api.CredGetSessionTypes.argtypes = [ctypes.wintypes.DWORD, ctypes.POINTER(ctypes.wintypes.DWORD)]
        api.CredGetSessionTypes.restype = ctypes.wintypes.BOOL
        return api

    def secure_store_status(self) -> tuple[str, str]:
        """Probe capability using only a disposable, unique credential target."""
        cached = getattr(self, "_secure_store_probe", None)
        if cached is not None:
            return cached
        if os.name != "nt":
            result = ("UNAVAILABLE_CURRENT_LOGON_SESSION", "Windows 当前登录会话不可用")
        else:
            probe = WindowsCredentialBackend(self.target + "/__probe__" + uuid4().hex)
            try:
                probe.write(uuid4().hex)
                if not probe.read():
                    raise OSError("SECURE_STORE_PROBE_READ_FAILED")
                result = ("READY", "")
            except OSError:
                result = ("UNAVAILABLE_CURRENT_LOGON_SESSION", "Windows 当前登录会话不可用")
            finally:
                probe.delete()
        self._secure_store_probe = result
        return result

    @staticmethod
    def _credential_type():
        class CREDENTIALW(ctypes.Structure):
            _fields_ = [("Flags", ctypes.wintypes.DWORD), ("Type", ctypes.wintypes.DWORD),
                        ("TargetName", ctypes.wintypes.LPWSTR), ("Comment", ctypes.wintypes.LPWSTR),
                        ("LastWritten", ctypes.wintypes.FILETIME), ("CredentialBlobSize", ctypes.wintypes.DWORD),
                        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", ctypes.wintypes.DWORD),
                        ("AttributeCount", ctypes.wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                        ("TargetAlias", ctypes.wintypes.LPWSTR), ("UserName", ctypes.wintypes.LPWSTR)]
        return CREDENTIALW

    def _write_with_persist(self, secret: str, persist: int) -> None:
        if os.name != "nt": raise OSError("CREDENTIAL_MANAGER_UNAVAILABLE")
        raw = secret.encode("utf-8")
        buf = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        cred = self._credential_type()(0, 1, self.target, None, ctypes.wintypes.FILETIME(), len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)), persist, 0, None, None, "QuantLab")
        api = self._api(type(cred))
        if not api.CredWriteW(ctypes.byref(cred), 0):
            raise OSError(ctypes.get_last_error(), "CREDENTIAL_WRITE_FAILED")

    def write(self, secret: str) -> None:
        self._write_with_persist(secret, 2)
    def read(self) -> str | None:
        if os.name != "nt": return None
        p=ctypes.c_void_p(); fn=self._api().CredReadW
        if not fn(self.target,1,0,ctypes.byref(p)): return None
        try:
            c=ctypes.cast(p,ctypes.POINTER(self._credential_type())).contents
            return ctypes.string_at(c.CredentialBlob,c.CredentialBlobSize).decode("utf-8")
        finally: self._api().CredFree(p)
    def delete(self) -> None:
        if os.name=="nt": self._api().CredDeleteW(self.target,1,0)

class DockerSecretCredentialBackend:
    """Read-only Docker secrets; values never enter settings/API responses."""
    managed = True
    def __init__(self) -> None:
        self.secret_file = os.environ.get("QQBOT_APP_SECRET_FILE", "").strip()
        self.app_id_file = os.environ.get("QQBOT_APP_ID_FILE", "").strip()
        self.openid_file = os.environ.get("QQBOT_OPENID_FILE", "").strip()
    def _read_file(self, name: str) -> str | None:
        if not name: return None
        try:
            path = Path(name)
            if not path.is_file() or path.stat().st_size > 4096: return None
            if os.name != "nt" and path.stat().st_mode & 0o022: return None
            value = path.read_text(encoding="utf-8").strip()
            return value or None
        except (OSError, UnicodeError): return None
    def read(self) -> str | None: return self._read_file(self.secret_file)
    def app_id(self) -> str | None: return self._read_file(self.app_id_file)
    def openid(self) -> str | None: return self._read_file(self.openid_file)
    def secure_store_status(self) -> tuple[str, str]:
        return ("READY", "DOCKER_SECRETS_READ_ONLY") if self.read() else ("UNAVAILABLE_CURRENT_LOGON_SESSION", "QQBOT_SECRET_FILE_MISSING_OR_INSECURE")
    def write(self, secret: str) -> None: raise PermissionError("QQBOT_SECRETS_MANAGED_BY_DEPLOYMENT")
    def delete(self) -> None: raise PermissionError("QQBOT_SECRETS_MANAGED_BY_DEPLOYMENT")

def _qqbot_paths(root: Path) -> tuple[Path, Path]:
    return root / "data" / "trade_coach" / "secrets" / "settings.json", Path(QQBOT_SECRET_TARGET)


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def as_cn(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        value = datetime.fromisoformat(text)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(CN_TZ)


def iso(value: datetime | None) -> str | None:
    return as_cn(value).isoformat() if value else None


def local_path(value: str | Path) -> Path:
    """Resolve a local path and reject URL/UNC inputs."""
    raw = str(value)
    candidate = Path(value).expanduser()
    if "://" in raw or raw.startswith(("\\\\", "//")) or candidate.anchor.startswith(("\\\\", "//")):
        raise ValueError("Trade Coach storage and evidence paths must be local")
    return candidate.resolve()


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    label: str
    asset_class: str
    venue: str
    source: str
    provider_symbol: str
    contract_semantics: str
    primary_source: str
    backup_source: str
    freshness_hours: int = 72

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "label": self.label,
            "asset_class": self.asset_class,
            "venue": self.venue,
            "source": self.source,
            "provider_symbol": self.provider_symbol,
            "contract_semantics": self.contract_semantics,
            "primary_source": self.primary_source,
            "backup_source": self.backup_source,
            "freshness_hours": self.freshness_hours,
        }


INSTRUMENT_SPECS: tuple[InstrumentSpec, ...] = (
    InstrumentSpec("000426.XSHE", "兴业银锡", "stock", "深圳证券交易所", "A股", "sz000426", "A股现货；原始价格用于操作，后复权价仅用于趋势与连续收益", "mootdx", "Tencent", 72),
    InstrumentSpec("000960.XSHE", "锡业股份", "stock", "深圳证券交易所", "A股", "sz000960", "A股现货；原始价格用于操作，后复权价仅用于趋势与连续收益", "mootdx", "Tencent", 72),
    InstrumentSpec("801050.SI", "申万有色", "sector", "申万行业指数", "指数日线", "801050", "申万有色 2021 行业指数日线；不把股票均值当作主源", "TUSHARE_INDEX_DAILY local", "Eastmoney", 120),
    InstrumentSpec("SILVER", "白银", "commodity", "COMEX", "Yahoo Finance", "SI=F", "COMEX 白银连续前月；由提供方定义换月，不用于实际下单", "Yahoo Finance", "Sina mapped contract", 72),
    InstrumentSpec("GOLD", "黄金", "commodity", "COMEX", "Yahoo Finance", "GC=F", "COMEX 黄金连续前月；由提供方定义换月，不用于实际下单", "Yahoo Finance", "Sina mapped contract", 72),
    InstrumentSpec("TIN", "锡", "commodity", "上海期货交易所", "Tushare PIT main mapping", "SN.SHF", "上期所锡逐日主力映射；原始未复权合约收盘，仅作方向因子，换月合约逐日留痕", "TUSHARE_TIN_MAIN local", "Sina mapped contract", 72),
    InstrumentSpec("COPPER", "铜", "commodity", "COMEX", "Yahoo Finance", "HG=F", "COMEX 铜连续前月；由提供方定义换月，仅作方向因子", "Yahoo Finance", "Sina mapped contract", 72),
    InstrumentSpec("OIL", "原油", "commodity", "NYMEX", "Yahoo Finance", "CL=F", "NYMEX WTI 连续前月；由提供方定义换月，仅作方向因子", "Yahoo Finance", "Sina mapped contract", 72),
    InstrumentSpec("DXY", "美元指数", "macro", "ICE", "Yahoo Finance", "DX-Y.NYB", "ICE 美元指数；指数点位，非可交易账户价格", "Yahoo Finance", "VPS PIT", 120),
    InstrumentSpec("TIP", "通胀保值债券 ETF", "macro", "NYSE Arca", "Yahoo Finance", "TIP", "TIP ETF 收盘价；USD，作为通胀预期交叉观察", "Yahoo Finance", "VPS PIT", 168),
    InstrumentSpec("REAL10Y", "10年期实际利率", "rate", "美国国债/FRED", "FRED", "DFII10", "FRED DFII10；百分比，按发布日期日频", "FRED", "VPS PIT", 168),
    InstrumentSpec("US2Y", "2年期名义利率", "rate", "美国国债/FRED", "FRED", "DGS2", "FRED DGS2；百分比，按发布日期日频", "FRED", "VPS PIT", 168),
    InstrumentSpec("US10Y", "10年期名义利率", "rate", "美国国债/FRED", "FRED", "DGS10", "FRED DGS10；百分比，按发布日期日频", "FRED", "VPS PIT", 168),
    InstrumentSpec("BREAKEVEN5Y", "5年通胀预期", "rate", "美国国债/FRED", "FRED", "T5YIE", "FRED T5YIE；百分比，按发布日期日频", "FRED", "VPS PIT", 168),
)
SPEC_BY_SYMBOL = {item.symbol: item for item in INSTRUMENT_SPECS}
STOCK_SYMBOLS = ("000426.XSHE", "000960.XSHE")
COMMODITY_SYMBOLS = ("SILVER", "GOLD", "TIN", "COPPER", "OIL")


@dataclass(frozen=True)
class MarketObservation:
    instrument: str
    source: str
    observed_at: datetime
    exchange_time: datetime | None
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    adjusted_close: float | None
    status: str
    reason_codes: tuple[str, ...] = ()
    source_ref: str | None = None
    raw_hash: str | None = None
    latency_ms: float | None = None
    timestamp_precision: str = "second"
    mapping_version: str | None = None
    contract_mapping: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.instrument not in SPEC_BY_SYMBOL:
            raise ValueError(f"unknown instrument: {self.instrument}")
        if self.status not in STATUSES:
            raise ValueError(f"unsupported observation status: {self.status}")
        if as_cn(self.observed_at) is None:
            raise ValueError("observed_at is required")
        if self.exchange_time is not None:
            as_cn(self.exchange_time)
        if self.status == "READY" and self.close is not None:
            if self.close <= 0:
                raise ValueError("READY close must be positive")
            values = [value for value in (self.open, self.high, self.low) if value is not None]
            if values and (min(values) <= 0 or (self.low is not None and self.high is not None and self.low > self.high)):
                raise ValueError("invalid OHLC values")

    def to_dict(self) -> dict[str, Any]:
        spec = SPEC_BY_SYMBOL[self.instrument]
        return {
            "symbol": self.instrument,
            "label": spec.label,
            "asset_class": spec.asset_class,
            "source": self.source,
            "observed_at": iso(self.observed_at),
            "exchange_time": iso(self.exchange_time),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "adjusted_close": self.adjusted_close,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "source_ref": self.source_ref,
            "raw_hash": self.raw_hash,
            "latency_ms": self.latency_ms,
            "timestamp_precision": self.timestamp_precision,
            "mapping_version": self.mapping_version,
            "contract_mapping": dict(self.contract_mapping or {}),
        }


def _empty_observation(symbol: str, source: str, observed_at: datetime, reason: str, source_ref: str | None = None) -> MarketObservation:
    return MarketObservation(symbol, source, observed_at, None, None, None, None, None, None, None, "MISSING", (reason,), source_ref=source_ref, raw_hash=canonical_hash({"instrument": symbol, "source": source, "reason": reason, "source_ref": source_ref}))


class TradeCoachStore:
    """Append-only SQLite store for the product layer."""

    def __init__(self, path: str | Path, *, seed_candidate: bool = True):
        self.path = local_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()
        if seed_candidate:
            self.ensure_candidate_snapshot()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=20000")
        return connection

    @contextmanager
    def _session(self):
        """Yield and always close a connection (important on Windows WAL)."""
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._lock, self._session() as connection:
            # Set WAL once during initialization.  Reissuing the journal-mode
            # pragma on every read connection can wait on Windows file locks
            # and made the source-state page take tens of seconds on a large
            # local sector archive.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS market_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instrument TEXT NOT NULL,
                    source TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    exchange_time TEXT,
                    open REAL, high REAL, low REAL, close REAL, volume REAL,
                    adjusted_close REAL,
                    status TEXT NOT NULL,
                    reason_codes TEXT NOT NULL,
                    source_ref TEXT,
                    raw_hash TEXT,
                    latency_ms REAL,
                    timestamp_precision TEXT NOT NULL DEFAULT 'second',
                    mapping_version TEXT,
                    contract_mapping TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(instrument, source, exchange_time, raw_hash)
                );
                CREATE INDEX IF NOT EXISTS idx_tc_market_instrument_time ON market_observations(instrument, exchange_time, observed_at);
                CREATE INDEX IF NOT EXISTS idx_tc_market_source_status_id ON market_observations(instrument, source, status, id);
                CREATE TABLE IF NOT EXISTS account_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    shares INTEGER,
                    avg_cost REAL,
                    available_cash REAL,
                    total_assets REAL,
                    planned_cash_out REAL,
                    source TEXT NOT NULL,
                    confirmation_note TEXT,
                    is_current_fact INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_tc_accounts_time ON account_snapshots(account_id, captured_at, id);
                CREATE TABLE IF NOT EXISTS trade_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    decided_at TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL,
                    fees REAL,
                    execution_status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    source TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    effective_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    version TEXT NOT NULL,
                    supersedes_id INTEGER,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS narratives (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    as_of TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    prior_id INTEGER,
                    payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS advice (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    as_of TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    last_notified TEXT,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(event_key, fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_tc_events_seen ON events(last_seen, id);
                CREATE TABLE IF NOT EXISTS diary (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    layer TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    content TEXT NOT NULL,
                    prev_hash TEXT,
                    record_hash TEXT NOT NULL UNIQUE,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS vps_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL,
                    source_ref TEXT,
                    status TEXT NOT NULL,
                    risk_level TEXT,
                    prediction_gate_status TEXT NOT NULL,
                    macro_event_gate TEXT,
                    reason_codes TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS refresh_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ai_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_hash TEXT NOT NULL UNIQUE,
                    provider TEXT NOT NULL,
                    model TEXT,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    memory_ids TEXT NOT NULL,
                    verification TEXT NOT NULL,
                    response_hash TEXT,
                    result TEXT NOT NULL,
                    error_code TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tc_ai_runs_time ON ai_runs(completed_at, id);
                CREATE TABLE IF NOT EXISTS notification_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER,
                    adapter TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempted_at TEXT NOT NULL,
                    response_code INTEGER,
                    error_code TEXT,
                    payload_hash TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tc_notification_time ON notification_deliveries(attempted_at, id);
                """
            )

    def ensure_candidate_snapshot(self) -> None:
        with self._lock, self._session() as connection:
            exists = connection.execute("SELECT 1 FROM account_snapshots WHERE status='PENDING_USER_CONFIRMATION' LIMIT 1").fetchone()
            if exists:
                return
            captured = now_cn().isoformat()
            connection.execute(
                "INSERT INTO account_snapshots(account_id,captured_at,status,shares,avg_cost,available_cash,total_assets,planned_cash_out,source,confirmation_note,is_current_fact) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                ("personal", captured, "PENDING_USER_CONFIRMATION", 600, 34.751, 9720.25, 34668.25, 0.0, "USER_PLAN_CANDIDATE_SNAPSHOT", "计划中的截图候选值；首次启动必须由用户重新确认。"),
            )

    @staticmethod
    def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def append_observation(self, observation: MarketObservation) -> bool:
        with self._lock, self._session() as connection:
            before = connection.total_changes
            connection.execute(
                """INSERT OR IGNORE INTO market_observations
                (instrument,source,observed_at,exchange_time,open,high,low,close,volume,adjusted_close,status,reason_codes,source_ref,raw_hash,latency_ms,timestamp_precision,mapping_version,contract_mapping)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (observation.instrument, observation.source, iso(observation.observed_at), iso(observation.exchange_time), observation.open, observation.high, observation.low, observation.close, observation.volume, observation.adjusted_close, observation.status, json.dumps(observation.reason_codes, ensure_ascii=False), observation.source_ref, observation.raw_hash, observation.latency_ms, observation.timestamp_precision, observation.mapping_version, json.dumps(observation.contract_mapping or {}, ensure_ascii=False, sort_keys=True)),
            )
            return connection.total_changes > before

    def append_observations(self, observations: Iterable[MarketObservation]) -> int:
        rows = list(observations)
        if not rows:
            return 0
        with self._lock, self._session() as connection:
            before = connection.total_changes
            connection.executemany(
                """INSERT OR IGNORE INTO market_observations
                (instrument,source,observed_at,exchange_time,open,high,low,close,volume,adjusted_close,status,reason_codes,source_ref,raw_hash,latency_ms,timestamp_precision,mapping_version,contract_mapping)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        observation.instrument,
                        observation.source,
                        iso(observation.observed_at),
                        iso(observation.exchange_time),
                        observation.open,
                        observation.high,
                        observation.low,
                        observation.close,
                        observation.volume,
                        observation.adjusted_close,
                        observation.status,
                        json.dumps(observation.reason_codes, ensure_ascii=False),
                        observation.source_ref,
                        observation.raw_hash,
                        observation.latency_ms,
                        observation.timestamp_precision,
                        observation.mapping_version,
                        json.dumps(observation.contract_mapping or {}, ensure_ascii=False, sort_keys=True),
                    )
                    for observation in rows
                ],
            )
            return connection.total_changes - before

    def history(self, instrument: str, limit: int = 260) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = connection.execute("SELECT * FROM market_observations WHERE instrument=? AND close IS NOT NULL AND status='READY' ORDER BY COALESCE(exchange_time,observed_at) DESC,id DESC LIMIT ?", (instrument, limit * 4)).fetchall()
        # Do not splice unlike contracts (for example SHFE AG0 and COMEX
        # Yahoo SI=F) into one return series.  A-share history may combine the
        # local daily archive with the current Tencent quote because both are
        # the same spot instrument; factor series select a single source,
        # preferring the declared primary when it has enough history.
        spec = SPEC_BY_SYMBOL.get(instrument)
        if spec and spec.asset_class != "stock":
            by_source: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                by_source.setdefault(str(row["source"]), []).append(row)
            primary_sources = [name for name in by_source if name.lower() == spec.primary_source.lower()]
            if primary_sources:
                primary_rows = [row for name in primary_sources for row in by_source[name]]
                if len(primary_rows) >= 20:
                    rows = primary_rows
                else:
                    rows = max(by_source.values(), key=len)
            elif by_source:
                rows = max(by_source.values(), key=len)
        # One point per exchange timestamp, preferring the most recently observed source.
        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            key = str(item.get("exchange_time") or item["observed_at"])
            unique.setdefault(key, item)
        return list(reversed(list(unique.values())[:limit]))

    def latest_by_source(self, instrument: str) -> dict[str, dict[str, Any]]:
        with self._session() as connection:
            rows = connection.execute(
                """SELECT m.* FROM market_observations m
                JOIN (SELECT source, MAX(id) id FROM market_observations WHERE instrument=? GROUP BY source) x ON x.id=m.id
                WHERE m.instrument=?""", (instrument, instrument),
            ).fetchall()
        return {str(row["source"]): dict(row) for row in rows}

    def latest_usable_by_source(self, instrument: str) -> dict[str, dict[str, Any]]:
        """Return the newest timestamped close per source for stale fallback.

        ``latest_by_source`` intentionally exposes a newer failed probe so an
        outage is visible in the UI.  A transient failed probe must not erase
        the last real close, however: callers can use this read-only history
        to mark that close ``STALE`` while still showing the latest probe as
        ``MISSING``.
        """
        with self._session() as connection:
            rows = connection.execute(
                """SELECT m.* FROM market_observations m
                JOIN (SELECT source, MAX(id) id FROM market_observations
                      WHERE instrument=? AND close IS NOT NULL
                        AND status IN ('READY','STALE')
                      GROUP BY source) x ON x.id=m.id
                WHERE m.instrument=?""", (instrument, instrument),
            ).fetchall()
        return {str(row["source"]): dict(row) for row in rows}

    def latest_observations(self) -> dict[str, list[dict[str, Any]]]:
        return {symbol: list(self.latest_by_source(symbol).values()) for symbol in SPEC_BY_SYMBOL}

    def append_account_snapshot(self, *, status: str, shares: int | None, avg_cost: float | None, available_cash: float | None, total_assets: float | None, planned_cash_out: float | None = 0.0, source: str, note: str) -> int:
        if status not in {"PENDING_USER_CONFIRMATION", "CONFIRMED"}:
            raise ValueError("unsupported account snapshot status")
        if shares is not None and (shares < 0 or shares % 100 != 0):
            raise ValueError("A股持仓股数必须为非负整数手")
        captured = now_cn().isoformat()
        with self._lock, self._session() as connection:
            connection.execute(
                "INSERT INTO account_snapshots(account_id,captured_at,status,shares,avg_cost,available_cash,total_assets,planned_cash_out,source,confirmation_note,is_current_fact) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("personal", captured, status, shares, avg_cost, available_cash, total_assets, planned_cash_out, source, note, int(status == "CONFIRMED")),
            )
            return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    def account(self) -> dict[str, Any]:
        with self._session() as connection:
            candidate = connection.execute("SELECT * FROM account_snapshots WHERE status='PENDING_USER_CONFIRMATION' ORDER BY id DESC LIMIT 1").fetchone()
            confirmed = connection.execute("SELECT * FROM account_snapshots WHERE status='CONFIRMED' AND is_current_fact=1 ORDER BY id DESC LIMIT 1").fetchone()
            all_rows = connection.execute("SELECT * FROM account_snapshots ORDER BY id DESC LIMIT 20").fetchall()
        def public(row: sqlite3.Row | None) -> dict[str, Any] | None:
            if row is None:
                return None
            item = dict(row)
            item["is_current_fact"] = bool(item["is_current_fact"])
            return item
        return {"candidate": public(candidate), "confirmed": public(confirmed), "history": [public(row) for row in all_rows]}

    def append_trade(self, *, side: str, quantity: int, price: float | None, fees: float | None, execution_status: str, reason: str, source: str = "MANUAL_USER_ENTRY") -> int:
        if side not in {"BUY", "SELL"} or quantity <= 0 or quantity % 100 != 0:
            raise ValueError("成交方向或股数无效；A股必须按100股记录")
        if execution_status not in {"PLANNED", "EXECUTED_MANUALLY", "CANCELLED"}:
            raise ValueError("execution_status must represent a manual record")
        with self._lock, self._session() as connection:
            connection.execute(
                "INSERT INTO trade_records(account_id,decided_at,side,quantity,price,fees,execution_status,reason,source,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("personal", now_cn().isoformat(), side, quantity, price, fees, execution_status, reason, source, now_cn().isoformat()),
            )
            return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    def trades(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._session() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM trade_records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def append_memory(self, *, kind: str, content: str, effective_at: datetime | None = None, source: str = "USER_OR_SYSTEM", version: str = "v0.1", supersedes_id: int | None = None) -> int:
        if not content.strip():
            raise ValueError("memory content cannot be empty")
        with self._lock, self._session() as connection:
            connection.execute("INSERT INTO memories(kind,content,effective_at,source,version,supersedes_id,recorded_at) VALUES(?,?,?,?,?,?,?)", (kind, content.strip(), iso(effective_at or now_cn()), source, version, supersedes_id, now_cn().isoformat()))
            return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    def memories(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._session() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM memories ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def search_memories(self, query: str, limit: int = 12) -> list[dict[str, Any]]:
        """Retrieve relevant long-term memory without changing the append-only log.

        This is intentionally a small local retrieval layer for v0.1: memory
        rows remain the source of truth and are ranked by token overlap.  The
        AI provider receives the selected row ids and text, so an audit can
        prove which memories were available to a particular explanation.
        """
        terms = {item for item in re.findall(r"[\w\u3400-\u9fff]+", str(query).lower()) if len(item) > 1}
        rows = self.memories(max(100, limit * 8))
        if not terms:
            return rows[:limit]
        ranked: list[tuple[int, int, dict[str, Any]]] = []
        for row in rows:
            haystack = f"{row.get('kind', '')} {row.get('content', '')}".lower()
            score = sum(1 for term in terms if term in haystack)
            ranked.append((score, int(row.get("id") or 0), row))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        matched = [row for score, _row_id, row in ranked if score > 0]
        return (matched or rows)[:limit]

    def append_narrative(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(payload)
        # Clock fields change on every API poll; the semantic payload is what
        # defines a narrative revision and keeps the diary append-only without
        # emitting the same unchanged story every ten seconds.
        # `prior_id` is an audit pointer, not a semantic change.  Ignoring it
        # makes polling idempotent while still recording the pointer whenever a
        # genuinely changed narrative is appended.
        digest = canonical_hash({key: value for key, value in data.items() if key not in {"generated_at", "prior_id", "prior_narrative_id"}})
        with self._lock, self._session() as connection:
            existing = connection.execute("SELECT * FROM narratives WHERE payload_hash=?", (digest,)).fetchone()
            if existing:
                return dict(existing)
            prior = connection.execute("SELECT id FROM narratives ORDER BY id DESC LIMIT 1").fetchone()
            data["prior_id"] = prior[0] if prior else None
            digest = canonical_hash({key: value for key, value in data.items() if key not in {"generated_at", "prior_id", "prior_narrative_id"}})
            connection.execute("INSERT OR IGNORE INTO narratives(as_of,generated_at,prior_id,payload,payload_hash) VALUES(?,?,?,?,?)", (str(data.get("as_of") or now_cn().date()), str(data.get("generated_at") or now_cn().isoformat()), data.get("prior_id"), json.dumps(data, ensure_ascii=False, sort_keys=True), digest))
            row = connection.execute("SELECT * FROM narratives WHERE payload_hash=?", (digest,)).fetchone()
        return dict(row) if row else data

    def latest_narrative(self) -> dict[str, Any] | None:
        with self._session() as connection:
            row = connection.execute("SELECT * FROM narratives ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json_load(item.get("payload"), {})
        return item

    def append_advice(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        data = dict(payload)
        # The mentor chain may gain a "continuation" sentence after the first
        # poll; that is explanatory context, not a new trading judgement.
        digest = canonical_hash({key: value for key, value in data.items() if key not in {"generated_at", "mentor_chain"}})
        with self._lock, self._session() as connection:
            existing = connection.execute("SELECT * FROM advice WHERE payload_hash=?", (digest,)).fetchone()
            if existing:
                item = dict(existing)
                item["payload"] = json_load(item.get("payload"), {})
                return item
            connection.execute("INSERT OR IGNORE INTO advice(as_of,generated_at,payload,payload_hash) VALUES(?,?,?,?)", (str(data.get("as_of") or now_cn().date()), str(data.get("generated_at") or now_cn().isoformat()), json.dumps(data, ensure_ascii=False, sort_keys=True), digest))
            row = connection.execute("SELECT * FROM advice WHERE payload_hash=?", (digest,)).fetchone()
        item = dict(row) if row else data
        item["payload"] = json_load(item.get("payload"), {})
        return item

    def latest_advice(self) -> dict[str, Any] | None:
        with self._session() as connection:
            row = connection.execute("SELECT * FROM advice ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json_load(item.get("payload"), {})
        return item

    def upsert_event(self, *, event_key: str, event_type: str, payload: Mapping[str, Any], notify: bool = True) -> dict[str, Any]:
        data = dict(payload)
        # State fingerprints deliberately exclude clock text and explanatory
        # wording.  This is the condition that controls notification spam;
        # the full payload remains in the append-only row for audit context.
        fingerprint = canonical_hash({"event_key": event_key, "state_fingerprint": data["state_fingerprint"]}) if data.get("state_fingerprint") else canonical_hash(data)
        current = now_cn().isoformat()
        with self._lock, self._session() as connection:
            row = connection.execute("SELECT * FROM events WHERE event_key=? AND fingerprint=?", (event_key, fingerprint)).fetchone()
            if row:
                connection.execute("UPDATE events SET last_seen=?,status='ACTIVE' WHERE id=?", (current, row["id"]))
                row = connection.execute("SELECT * FROM events WHERE id=?", (row["id"],)).fetchone()
                item = dict(row)
                item["is_new_notification"] = False
                item["payload"] = json_load(item.get("payload"), {})
                return item
            prior = connection.execute("SELECT * FROM events WHERE event_key=? ORDER BY id DESC LIMIT 1", (event_key,)).fetchone()
            last_notified = current if notify else None
            connection.execute("INSERT INTO events(event_key,fingerprint,event_type,first_seen,last_seen,last_notified,status,payload) VALUES(?,?,?,?,?,?,?,?)", (event_key, fingerprint, event_type, current, current, last_notified, "ACTIVE", json.dumps(data, ensure_ascii=False, sort_keys=True)))
            row = connection.execute("SELECT * FROM events WHERE id=?", (connection.execute("SELECT last_insert_rowid()").fetchone()[0],)).fetchone()
        item = dict(row) if row else {"event_key": event_key, "event_type": event_type, "payload": data}
        item["is_new_notification"] = prior is None or (prior["fingerprint"] != fingerprint)
        item["payload"] = json_load(item.get("payload"), {})
        return item

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = connection.execute("SELECT * FROM events ORDER BY last_seen DESC,id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        seen_states: set[tuple[str, str]] = set()
        for row in rows:
            item = dict(row)
            item["payload"] = json_load(item.get("payload"), {})
            state_key = (str(item.get("event_key")), str(item["payload"].get("state_fingerprint") or item.get("fingerprint")))
            if state_key in seen_states:
                continue
            seen_states.add(state_key)
            result.append(item)
            if len(result) >= limit:
                break
        return result

    def latest_event(self, event_key: str) -> dict[str, Any] | None:
        """Return one event state so a reminder can describe its transition."""
        with self._session() as connection:
            row = connection.execute("SELECT * FROM events WHERE event_key=? ORDER BY id DESC LIMIT 1", (event_key,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json_load(item.get("payload"), {})
        return item

    def event_by_id(self, event_id: int) -> dict[str, Any] | None:
        with self._session() as connection:
            row = connection.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json_load(item.get("payload"), {})
        return item

    def append_diary(self, *, layer: str, content: Mapping[str, Any], event_time: datetime | None = None) -> dict[str, Any]:
        data = dict(content)
        with self._lock, self._session() as connection:
            prior = connection.execute("SELECT record_hash FROM diary ORDER BY id DESC LIMIT 1").fetchone()
            prev_hash = prior[0] if prior else None
            record = {"layer": layer, "event_time": iso(event_time or now_cn()), "content": data, "prev_hash": prev_hash}
            digest = canonical_hash(record)
            connection.execute("INSERT INTO diary(layer,event_time,content,prev_hash,record_hash,recorded_at) VALUES(?,?,?,?,?,?)", (layer, record["event_time"], json.dumps(data, ensure_ascii=False, sort_keys=True), prev_hash, digest, now_cn().isoformat()))
        record.update({"record_hash": digest, "recorded_at": now_cn().isoformat()})
        return record

    def diary(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = connection.execute("SELECT * FROM diary ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["content"] = json_load(item.get("content"), {})
            result.append(item)
        return result

    def append_vps_fact(self, payload: Mapping[str, Any]) -> bool:
        data = dict(payload)
        digest = canonical_hash(data)
        with self._lock, self._session() as connection:
            before = connection.total_changes
            connection.execute("INSERT OR IGNORE INTO vps_facts(observed_at,source_ref,status,risk_level,prediction_gate_status,macro_event_gate,reason_codes,payload,payload_hash) VALUES(?,?,?,?,?,?,?,?,?)", (str(data.get("observed_at") or now_cn().isoformat()), data.get("source_ref"), data.get("status", "MISSING"), data.get("risk_level"), data.get("prediction_gate_status", "MISSING"), data.get("macro_event_gate"), json.dumps(data.get("reason_codes", []), ensure_ascii=False), json.dumps(data, ensure_ascii=False, sort_keys=True), digest))
            return connection.total_changes > before

    def latest_vps_fact(self) -> dict[str, Any] | None:
        with self._session() as connection:
            row = connection.execute("SELECT * FROM vps_facts ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json_load(item.get("payload"), {})
        item["reason_codes"] = json_load(item.get("reason_codes"), [])
        return item

    def append_refresh_run(self, *, mode: str, status: str, payload: Mapping[str, Any], started_at: datetime, completed_at: datetime) -> int:
        with self._lock, self._session() as connection:
            connection.execute("INSERT INTO refresh_runs(started_at,completed_at,mode,status,payload) VALUES(?,?,?,?,?)", (iso(started_at), iso(completed_at), mode, status, json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)))
            return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    def refresh_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = connection.execute("SELECT * FROM refresh_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json_load(item.get("payload"), {})
            result.append(item)
        return result

    def append_ai_run(self, *, request_hash: str, provider: str, model: str | None, status: str, started_at: datetime, completed_at: datetime, memory_ids: Sequence[int], verification: Mapping[str, Any], response_hash: str | None, result: Mapping[str, Any], error_code: str | None = None) -> int:
        """Append one AI attempt; secrets and raw Authorization headers never enter this table."""
        with self._lock, self._session() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO ai_runs
                (request_hash,provider,model,status,started_at,completed_at,memory_ids,verification,response_hash,result,error_code)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (request_hash, provider, model, status, iso(started_at), iso(completed_at), json.dumps(list(memory_ids)), json.dumps(dict(verification), ensure_ascii=False, sort_keys=True), response_hash, json.dumps(dict(result), ensure_ascii=False, sort_keys=True), error_code),
            )
            row = connection.execute("SELECT id FROM ai_runs WHERE request_hash=?", (request_hash,)).fetchone()
        return int(row[0]) if row else 0

    def ai_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = connection.execute("SELECT * FROM ai_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["memory_ids"] = json_load(item.get("memory_ids"), [])
            item["verification"] = json_load(item.get("verification"), {})
            item["result"] = json_load(item.get("result"), {})
            result.append(item)
        return result

    def latest_ai_run(self) -> dict[str, Any] | None:
        rows = self.ai_runs(1)
        return rows[0] if rows else None

    def append_notification_delivery(self, *, event_id: int | None, adapter: str, status: str, attempted_at: datetime, response_code: int | None, error_code: str | None, payload: Mapping[str, Any]) -> int:
        data = dict(payload)
        digest = canonical_hash(data)
        with self._lock, self._session() as connection:
            connection.execute(
                """INSERT INTO notification_deliveries
                (event_id,adapter,status,attempted_at,response_code,error_code,payload_hash,payload)
                VALUES(?,?,?,?,?,?,?,?)""",
                (event_id, adapter, status, iso(attempted_at), response_code, error_code, digest, json.dumps(data, ensure_ascii=False, sort_keys=True)),
            )
            return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    def notification_deliveries(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._session() as connection:
            rows = connection.execute("SELECT * FROM notification_deliveries ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json_load(item.get("payload"), {})
            result.append(item)
        return result


class MentorProvider(Protocol):
    """Provider boundary: deterministic rules and an optional AI explanation stay separate."""

    name: str

    def status(self) -> dict[str, Any]:
        ...

    def explain(self, *, context: Mapping[str, Any], memories: Sequence[Mapping[str, Any]], verification: Mapping[str, Any]) -> dict[str, Any]:
        ...


def _safe_float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


class OpenAICompatibleMentorProvider:
    """Optional OpenAI-compatible explanation provider.

    The provider can explain the evidence but cannot produce or override an
    operation.  A missing key, timeout, HTTP failure, malformed JSON response,
    or schema violation is represented as a fail-closed result and never
    replaced with the deterministic chain under an ``AI`` label.
    """

    # Kept as a compatibility adapter for existing callers and fixtures. The
    # application default is MultiMentorProvider below, not this single
    # provider.
    name = "openai_compatible"
    max_attempts = 1

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None, model: str | None = None, timeout: float | None = None, opener: Callable[..., Any] | None = None):
        self._api_key = (api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")).strip()
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.model = model or os.environ.get("TRADE_COACH_AI_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-5.4-mini"
        self.timeout = timeout if timeout is not None else _safe_float_env("TRADE_COACH_AI_TIMEOUT_SECONDS", 18.0, 1.0, 60.0)
        self._opener = opener or urllib.request.urlopen

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "status": "READY" if self.configured else "NOT_CONFIGURED",
            "configured": self.configured,
            "model": self.model if self.configured else None,
            "base_url": self.base_url,
            "timeout_seconds": self.timeout,
            "is_ai": self.configured,
            "fail_closed": not self.configured,
            "reason_codes": [] if self.configured else ["OPENAI_API_KEY_UNSET"],
        }

    @staticmethod
    def _message_content(response: Mapping[str, Any]) -> str | None:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            return None
        message = choices[0].get("message")
        if isinstance(message, Mapping):
            content = message.get("content")
            if isinstance(content, str):
                return content
        return None

    @staticmethod
    def _structured_output(value: Any) -> dict[str, Any] | None:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
            try:
                value = json.loads(text)
            except (TypeError, ValueError):
                # Some compatible providers wrap an otherwise valid JSON
                # object in one sentence or markdown. Accept only a decoded
                # object that passes the complete schema below.
                value = None
                decoder = json.JSONDecoder()
                for match in re.finditer(r"\{", text):
                    try:
                        candidate, _ = decoder.raw_decode(text[match.start():])
                    except (TypeError, ValueError):
                        continue
                    if isinstance(candidate, Mapping) and all(key in candidate for key in ("summary", "drivers", "risks", "counter_evidence", "questions", "confidence", "source_references")):
                        value = candidate
                        break
                if value is None:
                    return None
        if not isinstance(value, Mapping):
            return None
        required = ("summary", "drivers", "risks", "counter_evidence", "questions", "confidence", "source_references")
        if any(key not in value for key in required):
            return None
        if not isinstance(value.get("summary"), str) or not value["summary"].strip():
            return None
        lists = {}
        for key in ("drivers", "risks", "counter_evidence", "questions", "source_references"):
            raw = value.get(key)
            if not isinstance(raw, list) or any(not isinstance(item, str) or not item.strip() for item in raw):
                return None
            lists[key] = [item.strip() for item in raw[:12]]
        confidence = str(value.get("confidence") or "").upper()
        if confidence not in {"LOW", "MEDIUM", "HIGH", "UNKNOWN"}:
            return None
        return {
            "schema_version": AI_SCHEMA_VERSION,
            "summary": value["summary"].strip(),
            **lists,
            "confidence": confidence,
            "uncertainty": str(value.get("uncertainty") or "未提供").strip(),
            "rule_action_reference": str(value.get("rule_action_reference") or "仅引用确定性规则动作；不由 AI 改写").strip(),
        }

    def explain(self, *, context: Mapping[str, Any], memories: Sequence[Mapping[str, Any]], verification: Mapping[str, Any]) -> dict[str, Any]:
        memory_ids = [int(item["id"]) for item in memories if item.get("id") is not None]
        request = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是 Quant-Lab 的可选 AI 解释器，不是交易执行器。只解释给定的真实证据、" 
                        "长期记忆和联网交叉核验结果；不得编造价格、账户、收益、事件或来源。"
                        "不得改变确定性规则已经给出的动作、仓位区间、风险门槛；缺失证据必须明确说不确定。"
                        "只返回 JSON，字段为 summary 字符串、drivers/risks/counter_evidence/questions/source_references 字符串数组、"
                        "confidence（LOW/MEDIUM/HIGH/UNKNOWN）、uncertainty 字符串、rule_action_reference 字符串。"
                    ),
                },
                {"role": "user", "content": json.dumps({"context": dict(context), "long_term_memory": [dict(item) for item in memories], "cross_validation": dict(verification)}, ensure_ascii=False, sort_keys=True)},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        request_hash = canonical_hash({"provider": self.name, "base_url": self.base_url, "request": request})
        base_result = {
            "schema_version": AI_SCHEMA_VERSION,
            "provider": self.name,
            "model": self.model if self.configured else None,
            "request_hash": request_hash,
            "memory_ids": memory_ids,
            "verification": dict(verification),
            "is_ai": False,
            "structured_output": None,
            "fail_closed": True,
        }
        if not self.configured:
            return {**base_result, "status": "NOT_CONFIGURED", "reason_codes": ["OPENAI_API_KEY_UNSET"]}
        endpoint = f"{self.base_url}/chat/completions"
        started = datetime.now(UTC)
        attempts = []
        try:
            encoded = json.dumps(request, ensure_ascii=False).encode("utf-8")
            http_request = urllib.request.Request(endpoint, data=encoded, method="POST", headers={"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {self._api_key}"})
            for attempt in range(1, self.max_attempts + 1):
                try:
                    with self._opener(http_request, timeout=self.timeout) as response:
                        body = response.read(2_000_000).decode("utf-8", errors="replace")
                        response_code = int(getattr(response, "status", 200) or 200)
                    if response_code < 200 or response_code >= 300:
                        return {**base_result, "status": "PROVIDER_ERROR", "reason_codes": [f"AI_HTTP_STATUS:{response_code}"], "attempts": [{"attempt": attempt, "reason": f"HTTP_{response_code}"}]}
                    decoded = json.loads(body)
                    content = self._message_content(decoded) if isinstance(decoded, Mapping) else None
                    structured = self._structured_output(content)
                    if structured is None:
                        return {**base_result, "status": "INVALID_RESPONSE", "reason_codes": ["AI_STRUCTURED_OUTPUT_INVALID"], "response_hash": canonical_hash(body), "attempts": [{"attempt": attempt, "reason": "STRUCTURED_OUTPUT_INVALID"}]}
                    return {**base_result, "status": "READY", "is_ai": True, "fail_closed": False, "structured_output": structured, "response_hash": canonical_hash(body), "reason_codes": [], "attempts": attempts + [{"attempt": attempt, "reason": "SUCCESS"}], "latency_ms": (datetime.now(UTC) - started).total_seconds() * 1000}
                except urllib.error.HTTPError as exc:
                    return {**base_result, "status": "PROVIDER_ERROR", "reason_codes": [f"AI_HTTP_STATUS:{exc.code}"], "attempts": attempts + [{"attempt": attempt, "reason": f"HTTP_{exc.code}"}]}
                except (TimeoutError, urllib.error.URLError, OSError) as exc:
                    is_timeout = isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)
                    reason = "TIMEOUT" if is_timeout else f"NETWORK_{type(exc).__name__}"
                    attempts.append({"attempt": attempt, "reason": reason})
                    if attempt < self.max_attempts:
                        continue
                    code = "AI_TIMEOUT" if is_timeout else f"AI_NETWORK_ERROR:{type(exc).__name__}"
                    return {**base_result, "status": "TIMEOUT" if is_timeout else "PROVIDER_ERROR", "reason_codes": [code], "attempts": attempts, "latency_ms": (datetime.now(UTC) - started).total_seconds() * 1000}
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    return {**base_result, "status": "INVALID_RESPONSE", "reason_codes": [f"AI_RESPONSE_PARSE_ERROR:{type(exc).__name__}"], "attempts": attempts + [{"attempt": attempt, "reason": "RESPONSE_PARSE_ERROR"}]}
            raise AssertionError("unreachable")
        except AssertionError:
            raise


def _safe_endpoint(url: str) -> str | None:
    """Return only a public origin for status/API responses, never URL secrets."""
    parsed = urllib.parse.urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


class DeepSeekMentorProvider(OpenAICompatibleMentorProvider):
    """DeepSeek official OpenAI-compatible provider with model discovery."""

    name = "deepseek"
    max_attempts = 2
    _preferred_models = (
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "deepseek-v4-flash-vision-exp",
        "deepseek-chat",
        "deepseek-reasoner",
    )

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None, model: str | None = None, timeout: float | None = None, opener: Callable[..., Any] | None = None):
        effective_key = api_key if api_key is not None else os.environ.get("DEEPSEEK_API_KEY", "")
        configured_model = model if model is not None else os.environ.get("DEEPSEEK_MODEL", "")
        # The product contract freezes DeepSeek v4 Flash as its explicit
        # default. Catalog discovery is diagnostics only: a temporary
        # GET /models failure must never silently promote MiMo.
        explicit_model = bool(configured_model.strip())
        if not explicit_model and str(effective_key or "").strip():
            configured_model = "deepseek-v4-flash"
        super().__init__(
            api_key=effective_key,
            base_url=base_url or "https://api.deepseek.com",
            model=configured_model.strip(),
            timeout=timeout if timeout is not None else _safe_float_env("DEEPSEEK_TIMEOUT_SECONDS", 45.0, 1.0, 60.0),
            opener=opener,
        )
        self.model = configured_model.strip()
        self._configured_model = self.model
        self._configured_model_source = "ENV_OR_ARGUMENT" if explicit_model else "FROZEN_DEFAULT"
        self._discovery: dict[str, Any] | None = None

    def discover_models(self) -> dict[str, Any]:
        """Discover and select a model from DeepSeek's live catalog.

        The response body is used only for model IDs and is never persisted or
        returned by the API.  A configured model must appear in that catalog;
        otherwise the provider remains fail-closed.
        """
        if self._discovery is not None:
            return dict(self._discovery)
        if not self.configured:
            self._discovery = {"status": "NOT_CONFIGURED", "reason_codes": ["DEEPSEEK_API_KEY_UNSET"], "models": []}
            return dict(self._discovery)
        started = datetime.now(UTC)
        try:
            request = urllib.request.Request(f"{self.base_url}/models", method="GET", headers={"Accept": "application/json", "Authorization": f"Bearer {self._api_key}"})
            with self._opener(request, timeout=min(self.timeout, 12.0)) as response:
                body = response.read(1_000_000).decode("utf-8", errors="replace")
                response_code = int(getattr(response, "status", 200) or 200)
            if response_code < 200 or response_code >= 300:
                result = {"status": "PROVIDER_ERROR", "reason_codes": [f"DEEPSEEK_MODELS_HTTP_STATUS:{response_code}"], "models": []}
            else:
                decoded = json.loads(body)
                raw_models = decoded.get("data") if isinstance(decoded, Mapping) else None
                models = sorted({str(item.get("id", "")).strip() for item in (raw_models or []) if isinstance(item, Mapping) and str(item.get("id", "")).strip()})
                if not models:
                    result = {"status": "MODEL_UNVERIFIED", "reason_codes": ["DEEPSEEK_MODELS_EMPTY"], "models": []}
                elif self._configured_model:
                    if self._configured_model not in models:
                        result = {"status": "MODEL_UNVERIFIED", "reason_codes": ["DEEPSEEK_MODEL_NOT_IN_CATALOG"], "models": models}
                    else:
                        self.model = self._configured_model
                        result = {"status": "READY", "reason_codes": [], "models": models, "model": self.model, "model_source": f"{self._configured_model_source}_AND_CATALOG"}
                else:
                    selected = next((candidate for candidate in self._preferred_models if candidate in models), models[0])
                    self.model = selected
                    result = {"status": "READY", "reason_codes": [], "models": models, "model": selected, "model_source": "LIVE_CATALOG"}
            result["latency_ms"] = (datetime.now(UTC) - started).total_seconds() * 1000
        except urllib.error.HTTPError as exc:
            result = {"status": "PROVIDER_ERROR", "reason_codes": [f"DEEPSEEK_MODELS_HTTP_STATUS:{exc.code}"], "models": [], "latency_ms": (datetime.now(UTC) - started).total_seconds() * 1000}
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            result = {"status": "PROVIDER_ERROR", "reason_codes": ["DEEPSEEK_MODELS_TIMEOUT" if isinstance(exc, TimeoutError) else f"DEEPSEEK_MODELS_ERROR:{type(exc).__name__}"], "models": [], "latency_ms": (datetime.now(UTC) - started).total_seconds() * 1000}
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            result = {"status": "MODEL_UNVERIFIED", "reason_codes": [f"DEEPSEEK_MODELS_PARSE_ERROR:{type(exc).__name__}"], "models": [], "latency_ms": (datetime.now(UTC) - started).total_seconds() * 1000}
        self._discovery = result
        return dict(result)

    def status(self) -> dict[str, Any]:
        if not self.configured:
            return {"provider": self.name, "protocol": "openai_compatible", "status": "NOT_CONFIGURED", "configured": False, "model": None, "model_source": None, "base_url": _safe_endpoint(self.base_url), "timeout_seconds": self.timeout, "is_ai": False, "fail_closed": True, "reason_codes": ["DEEPSEEK_API_KEY_UNSET"]}
        if self.model:
            discovery_status = self._discovery or {}
            source = discovery_status.get("model_source") or self._configured_model_source
            return {"provider": self.name, "protocol": "openai_compatible", "status": "CONFIGURED_PENDING_CALL", "configured": True, "model": self.model, "model_source": source, "base_url": _safe_endpoint(self.base_url), "timeout_seconds": self.timeout, "is_ai": False, "last_call_succeeded": None, "fail_closed": True, "reason_codes": list(discovery_status.get("reason_codes", []))}
        discovery = self._discovery or {}
        return {"provider": self.name, "protocol": "openai_compatible", "status": "MODEL_UNVERIFIED" if discovery.get("status") != "PROVIDER_ERROR" else "PROVIDER_ERROR", "configured": True, "model": None, "model_source": None, "base_url": _safe_endpoint(self.base_url), "timeout_seconds": self.timeout, "is_ai": False, "fail_closed": True, "reason_codes": list(discovery.get("reason_codes", ["DEEPSEEK_MODEL_DISCOVERY_REQUIRED"]))}

    def explain(self, *, context: Mapping[str, Any], memories: Sequence[Mapping[str, Any]], verification: Mapping[str, Any]) -> dict[str, Any]:
        if self.configured and not self.model:
            discovery = self.discover_models()
            if discovery.get("status") != "READY":
                return {"schema_version": AI_SCHEMA_VERSION, "provider": self.name, "model": None, "request_hash": canonical_hash({"provider": self.name, "discovery": discovery}), "memory_ids": [int(item["id"]) for item in memories if item.get("id") is not None], "verification": dict(verification), "status": discovery.get("status", "PROVIDER_ERROR"), "reason_codes": list(discovery.get("reason_codes", ["DEEPSEEK_MODEL_DISCOVERY_FAILED"])), "is_ai": False, "structured_output": None, "fail_closed": True}
        return {**super().explain(context=context, memories=memories, verification=verification), "protocol": "openai_compatible"}


class MiMoMentorProvider:
    """MiMo provider using the configured Anthropic endpoint with compatibility fallback."""

    name = "mimo"

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None, model: str | None = None, timeout: float | None = None, opener: Callable[..., Any] | None = None):
        self._api_key = (api_key if api_key is not None else os.environ.get("MIMO_API_KEY", "")).strip()
        self.base_url = (base_url if base_url is not None else (os.environ.get("MIMO_BASE_URL") or os.environ.get("MIMO_OPENAI_BASE_URL") or os.environ.get("MIMO_ANTHROPIC_BASE_URL", ""))).strip().rstrip("/")
        self.model = (model if model is not None else os.environ.get("MIMO_MODEL", "")).strip()
        self.timeout = timeout if timeout is not None else _safe_float_env("TRADE_COACH_AI_TIMEOUT_SECONDS", 18.0, 1.0, 60.0)
        self._opener = opener or urllib.request.urlopen
        explicit_protocol = os.environ.get("MIMO_PROTOCOL", "").strip().lower()
        self._protocol = "anthropic_messages_v1" if explicit_protocol == "anthropic" or (base_url is None and not explicit_protocol and "MIMO_BASE_URL" not in os.environ and bool(os.environ.get("MIMO_ANTHROPIC_BASE_URL"))) or (base_url is not None and "anthropic" in self.base_url.lower()) else "openai_compatible"

    @property
    def configured(self) -> bool:
        parsed = urllib.parse.urlparse(self.base_url)
        return bool(self._api_key and self.model and parsed.scheme in {"http", "https"} and parsed.netloc)

    def status(self) -> dict[str, Any]:
        reason_codes = []
        if not self._api_key:
            reason_codes.append("MIMO_API_KEY_UNSET")
        if not self.model:
            reason_codes.append("MIMO_MODEL_UNSET")
        if not _safe_endpoint(self.base_url):
            reason_codes.append("MIMO_ANTHROPIC_BASE_URL_UNSET")
        configured = not reason_codes
        return {"provider": self.name, "protocol": self._protocol, "status": "CONFIGURED_PENDING_CALL" if configured else "NOT_CONFIGURED", "configured": configured, "model": self.model if configured else None, "model_source": "ENV:MIMO_MODEL" if configured else None, "base_url": _safe_endpoint(self.base_url), "timeout_seconds": self.timeout, "is_ai": False, "last_call_succeeded": None, "fail_closed": True, "reason_codes": reason_codes}

    @staticmethod
    def _content(decoded: Mapping[str, Any]) -> str | None:
        content = decoded.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks = [str(item.get("text")) for item in content if isinstance(item, Mapping) and isinstance(item.get("text"), str)]
            return "".join(chunks) or None
        choices = decoded.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
            message = choices[0].get("message")
            return str(message.get("content")) if isinstance(message, Mapping) and isinstance(message.get("content"), str) else None
        return None

    def _openai_compatible_fallback(self, *, context: Mapping[str, Any], memories: Sequence[Mapping[str, Any]], verification: Mapping[str, Any]) -> dict[str, Any]:
        result = OpenAICompatibleMentorProvider(api_key=self._api_key, base_url=self._v1_base_url(), model=self.model, timeout=self.timeout, opener=self._opener).explain(context=context, memories=memories, verification=verification)
        return {**result, "provider": self.name, "protocol": "openai_compatible", "request_hash": canonical_hash({"provider": self.name, "protocol": "openai_compatible", "model": self.model, "memory_ids": [int(item["id"]) for item in memories if item.get("id") is not None], "verification": verification})}

    def _v1_base_url(self) -> str:
        return self.base_url if self.base_url.endswith("/v1") else f"{self.base_url}/v1"

    def explain(self, *, context: Mapping[str, Any], memories: Sequence[Mapping[str, Any]], verification: Mapping[str, Any]) -> dict[str, Any]:
        if self._protocol == "openai_compatible":
            result = OpenAICompatibleMentorProvider(api_key=self._api_key, base_url=self._v1_base_url(), model=self.model, timeout=self.timeout, opener=self._opener).explain(context=context, memories=memories, verification=verification)
            return {**result, "provider": self.name, "protocol": "openai_compatible"}
        memory_ids = [int(item["id"]) for item in memories if item.get("id") is not None]
        request = {
            "model": self.model,
            # MiMo may spend part of the budget on an internal thinking block;
            # leave enough room for the required JSON rather than accepting a
            # truncated, non-auditable answer.
            "max_tokens": 2400,
            "system": "你是 Quant-Lab 的可选 AI 解释器，不是交易执行器。只解释给定证据和长期记忆，不编造价格、账户、收益、事件或来源，不改变确定性动作；缺失证据必须明确不确定。只返回包含 summary、drivers、risks、counter_evidence、questions、confidence、source_references、uncertainty、rule_action_reference 的 JSON。",
            "messages": [{"role": "user", "content": json.dumps({"context": dict(context), "long_term_memory": [dict(item) for item in memories], "cross_validation": dict(verification)}, ensure_ascii=False, sort_keys=True)}],
        }
        request_hash = canonical_hash({"provider": self.name, "protocol": self._protocol, "base_url": _safe_endpoint(self.base_url), "request": request})
        base_result = {"schema_version": AI_SCHEMA_VERSION, "provider": self.name, "protocol": self._protocol, "model": self.model if self.configured else None, "request_hash": request_hash, "memory_ids": memory_ids, "verification": dict(verification), "is_ai": False, "structured_output": None, "fail_closed": True}
        if not self.configured:
            return {**base_result, "status": "NOT_CONFIGURED", "reason_codes": self.status()["reason_codes"]}
        endpoint = f"{self._v1_base_url()}/messages"
        started = datetime.now(UTC)
        encoded = json.dumps(request, ensure_ascii=False).encode("utf-8")
        http_request = urllib.request.Request(endpoint, data=encoded, method="POST", headers={"Accept": "application/json", "Content-Type": "application/json", "anthropic-version": "2023-06-01", "x-api-key": self._api_key})
        try:
            with self._opener(http_request, timeout=self.timeout) as response:
                body = response.read(2_000_000).decode("utf-8", errors="replace")
                response_code = int(getattr(response, "status", 200) or 200)
            if response_code in {404, 405}:
                return self._openai_compatible_fallback(context=context, memories=memories, verification=verification)
            if response_code < 200 or response_code >= 300:
                return {**base_result, "status": "PROVIDER_ERROR", "reason_codes": [f"MIMO_HTTP_STATUS:{response_code}"]}
            decoded = json.loads(body)
            content = self._content(decoded) if isinstance(decoded, Mapping) else None
            structured = OpenAICompatibleMentorProvider._structured_output(content)
            if structured is None:
                return {**base_result, "status": "INVALID_RESPONSE", "reason_codes": ["AI_STRUCTURED_OUTPUT_INVALID"], "response_hash": canonical_hash(body)}
            return {**base_result, "status": "READY", "is_ai": True, "fail_closed": False, "structured_output": structured, "response_hash": canonical_hash(body), "reason_codes": [], "latency_ms": (datetime.now(UTC) - started).total_seconds() * 1000}
        except urllib.error.HTTPError as exc:
            if exc.code in {404, 405}:
                return self._openai_compatible_fallback(context=context, memories=memories, verification=verification)
            return {**base_result, "status": "PROVIDER_ERROR", "reason_codes": [f"MIMO_HTTP_STATUS:{exc.code}"]}
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            return {**base_result, "status": "TIMEOUT" if isinstance(exc, TimeoutError) else "PROVIDER_ERROR", "reason_codes": ["MIMO_TIMEOUT" if isinstance(exc, TimeoutError) else f"MIMO_NETWORK_ERROR:{type(exc).__name__}"]}
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return {**base_result, "status": "INVALID_RESPONSE", "reason_codes": [f"MIMO_RESPONSE_PARSE_ERROR:{type(exc).__name__}"]}


class MultiMentorProvider:
    """DeepSeek-first, MiMo-fallback provider chain."""

    name = AI_PROVIDER_NAME

    def __init__(self, *, use_environment: bool = True, opener: Callable[..., Any] | None = None):
        if use_environment:
            self.providers = (DeepSeekMentorProvider(opener=opener), MiMoMentorProvider(opener=opener))
        else:
            self.providers = (DeepSeekMentorProvider(api_key="", opener=opener), MiMoMentorProvider(api_key="", base_url="", model="", opener=opener))
        self.discover_on_status = os.environ.get("TRADE_COACH_AI_DISCOVER_MODELS", "").strip().lower() in {"1", "true", "yes", "on"}

    def _statuses(self) -> list[dict[str, Any]]:
        deepseek = self.providers[0]
        if self.discover_on_status and isinstance(deepseek, DeepSeekMentorProvider) and deepseek.configured and not deepseek.model:
            deepseek.discover_models()
        return [dict(provider.status()) for provider in self.providers]

    def status(self) -> dict[str, Any]:
        statuses = self._statuses()
        active = next((item for item in statuses if item.get("configured") and item.get("model")), None)
        configured = any(bool(item.get("configured")) for item in statuses)
        reasons = []
        for item in statuses:
            for reason in item.get("reason_codes", []):
                if reason not in reasons:
                    reasons.append(reason)
        if active:
            return {"schema_version": AI_SCHEMA_VERSION, "provider": active["provider"], "selected_provider": active["provider"], "status": "CONFIGURED_PENDING_CALL", "configured": True, "model": active.get("model"), "model_source": active.get("model_source"), "protocol": active.get("protocol"), "base_url": active.get("base_url"), "timeout_seconds": active.get("timeout_seconds"), "is_ai": False, "last_call_succeeded": None, "fail_closed": True, "reason_codes": reasons, "providers": statuses, "fallback_order": list(AI_PROVIDER_ORDER)}
        overall_status = "MODEL_UNVERIFIED" if configured and any(item.get("status") == "MODEL_UNVERIFIED" for item in statuses) else ("PROVIDER_ERROR" if configured else "NOT_CONFIGURED")
        return {"schema_version": AI_SCHEMA_VERSION, "provider": self.name, "selected_provider": None, "status": overall_status, "configured": configured, "model": None, "model_source": None, "protocol": None, "base_url": None, "timeout_seconds": max((float(item.get("timeout_seconds") or 0) for item in statuses), default=0), "is_ai": False, "fail_closed": True, "reason_codes": reasons or ["AI_PROVIDER_NOT_CONFIGURED"], "providers": statuses, "fallback_order": list(AI_PROVIDER_ORDER)}

    def explain(self, *, context: Mapping[str, Any], memories: Sequence[Mapping[str, Any]], verification: Mapping[str, Any]) -> dict[str, Any]:
        attempts = []
        for provider in self.providers:
            provider_status = provider.status()
            if not provider_status.get("configured"):
                attempts.append({"provider": provider.name, "status": provider_status.get("status"), "model": provider_status.get("model"), "reason_codes": provider_status.get("reason_codes", [])})
                continue
            result = provider.explain(context=context, memories=memories, verification=verification)
            attempts.append({"provider": provider.name, "status": result.get("status"), "model": result.get("model"), "latency_ms": result.get("latency_ms"), "reason_codes": result.get("reason_codes", [])})
            if result.get("status") == "READY" and result.get("is_ai"):
                return {**result, "selected_provider": provider.name, "fallback_attempts": attempts, "fail_closed": False}
        all_reasons = []
        for attempt in attempts:
            for reason in attempt.get("reason_codes", []):
                if reason not in all_reasons:
                    all_reasons.append(reason)
        return {"schema_version": AI_SCHEMA_VERSION, "provider": self.name, "selected_provider": None, "model": None, "request_hash": canonical_hash({"provider": self.name, "memory_ids": [int(item["id"]) for item in memories if item.get("id") is not None], "verification": verification}), "memory_ids": [int(item["id"]) for item in memories if item.get("id") is not None], "verification": dict(verification), "status": "NOT_CONFIGURED" if not any(attempt.get("status") not in {"NOT_CONFIGURED"} for attempt in attempts) else "PROVIDER_ERROR", "reason_codes": all_reasons or ["AI_PROVIDER_UNAVAILABLE"], "is_ai": False, "structured_output": None, "fail_closed": True, "fallback_attempts": attempts}


class PublicEvidenceVerifier:
    """Read-only URL reachability check used before an optional AI explanation."""

    def __init__(self, *, timeout: float | None = None, opener: Callable[..., Any] | None = None):
        self.timeout = timeout if timeout is not None else _safe_float_env("TRADE_COACH_VERIFY_TIMEOUT_SECONDS", 6.0, 1.0, 20.0)
        self._opener = opener or urllib.request.urlopen

    def verify(self, references: Iterable[str], *, requested: bool = True) -> dict[str, Any]:
        urls = []
        for reference in references:
            text = str(reference or "").strip()
            parsed = urllib.parse.urlparse(text)
            if parsed.scheme in {"http", "https"} and parsed.netloc and text not in urls:
                urls.append(text)
        urls = urls[:8]
        if not requested:
            return {"status": "NOT_REQUESTED", "requested": False, "checked_at": now_cn().isoformat(), "checked": [], "reason_codes": ["CROSS_VALIDATION_NOT_REQUESTED"]}
        if not urls:
            return {"status": "MISSING", "requested": True, "checked_at": now_cn().isoformat(), "checked": [], "reason_codes": ["CROSS_VALIDATION_URLS_UNAVAILABLE"]}
        checked = []
        for url in urls:
            started = datetime.now(UTC)
            try:
                request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Quant-Lab-Personal-Trade-Coach/0.1", "Range": "bytes=0-4095"})
                with self._opener(request, timeout=self.timeout) as response:
                    body = response.read(4096)
                    status = int(getattr(response, "status", 200) or 200)
                checked.append({"url": url, "status": "READY" if 200 <= status < 300 else "FAILED", "http_status": status, "bytes_sampled": len(body), "latency_ms": (datetime.now(UTC) - started).total_seconds() * 1000})
            except (TimeoutError, urllib.error.URLError, OSError) as exc:
                checked.append({"url": url, "status": "FAILED", "error_code": "VERIFY_TIMEOUT" if isinstance(exc, TimeoutError) else f"VERIFY_ERROR:{type(exc).__name__}", "latency_ms": (datetime.now(UTC) - started).total_seconds() * 1000})
        healthy = sum(item["status"] == "READY" for item in checked)
        return {"status": "READY" if healthy == len(checked) else ("PARTIAL" if healthy else "FAILED"), "requested": True, "checked_at": now_cn().isoformat(), "checked": checked, "healthy_count": healthy, "reason_codes": [] if healthy == len(checked) else ["CROSS_VALIDATION_PARTIAL"]}


class NotificationAdapter(Protocol):
    name: str

    def status(self) -> dict[str, Any]:
        ...

    def send(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        ...


class WebhookNotificationAdapter:
    """Configurable real HTTP webhook adapter; absent target is explicit."""

    name = "webhook"

    def __init__(self, *, url: str | None = None, timeout: float | None = None, opener: Callable[..., Any] | None = None):
        self.url = (url if url is not None else os.environ.get("TRADE_COACH_NOTIFY_WEBHOOK_URL", "")).strip()
        self.timeout = timeout if timeout is not None else _safe_float_env("TRADE_COACH_NOTIFY_TIMEOUT_SECONDS", 8.0, 1.0, 30.0)
        self._opener = opener or urllib.request.urlopen

    @property
    def configured(self) -> bool:
        parsed = urllib.parse.urlparse(self.url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    def status(self) -> dict[str, Any]:
        parsed = urllib.parse.urlparse(self.url)
        return {"adapter": self.name, "status": "READY" if self.configured else "NOT_CONFIGURED", "configured": self.configured, "target_host": parsed.hostname if self.configured else None, "reason_codes": [] if self.configured else ["NOTIFICATION_TARGET_UNSET"]}

    def send(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not self.configured:
            return {"adapter": self.name, "status": "NOT_CONFIGURED", "response_code": None, "reason_codes": ["NOTIFICATION_TARGET_UNSET"]}
        body = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True).encode("utf-8")
        try:
            request = urllib.request.Request(self.url, data=body, method="POST", headers={"User-Agent": "Quant-Lab-Personal-Trade-Coach/0.1", "Content-Type": "application/json", "Accept": "application/json"})
            with self._opener(request, timeout=self.timeout) as response:
                response.read(4096)
                code = int(getattr(response, "status", 200) or 200)
            return {"adapter": self.name, "status": "DELIVERED" if 200 <= code < 300 else "FAILED", "response_code": code, "reason_codes": [] if 200 <= code < 300 else [f"NOTIFICATION_HTTP_STATUS:{code}"]}
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            return {"adapter": self.name, "status": "FAILED", "response_code": None, "reason_codes": ["NOTIFICATION_TIMEOUT" if isinstance(exc, TimeoutError) else f"NOTIFICATION_ERROR:{type(exc).__name__}"]}


class QQBotNotificationAdapter:
    """Official QQ Bot API C2C adapter. Credentials are passed explicitly."""

    name = "qqbot"
    token_url = "https://bots.qq.com/app/getAppAccessToken"
    message_url = "https://api.sgroup.qq.com/v2/users/{openid}/messages"

    def __init__(self, *, app_id: str | None = None, app_secret: str | None = None,
                 openid: str | None = None, timeout: float | None = None,
                 opener: Callable[..., Any] | None = None):
        self.app_id = (app_id or "").strip()
        self.app_secret = (app_secret or "").strip()
        self.openid = (openid or "").strip()
        self.timeout = timeout if timeout is not None else _safe_float_env("QQBOT_TIMEOUT_SECONDS", 8.0, 1.0, 30.0)
        self._opener = opener or urllib.request.urlopen

    @property
    def credentials_configured(self) -> bool:
        return bool(self.app_id and self.app_secret)

    @property
    def target_configured(self) -> bool:
        return bool(self.openid)

    def status(self) -> dict[str, Any]:
        if not self.credentials_configured:
            state, reasons = "NOT_CONFIGURED", ["QQBOT_CREDENTIALS_UNSET"]
        elif not self.target_configured:
            state, reasons = "WAITING_TARGET_BINDING", ["QQBOT_OPENID_UNSET", "QQBOT_BINDING_REQUIRED"]
        else:
            state, reasons = "READY", []
        return {"adapter": self.name, "status": state, "configured": self.credentials_configured,
                "target_host": "api.sgroup.qq.com" if self.target_configured else None,
                "reason_codes": reasons}

    def _request_json(self, request: urllib.request.Request) -> tuple[int, Mapping[str, Any]]:
        with self._opener(request, timeout=self.timeout) as response:
            code = int(getattr(response, "status", 200) or 200)
            raw = response.read(16384)
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = {}
        return code, body if isinstance(body, Mapping) else {}

    def send(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        state = self.status()
        if state["status"] != "READY":
            return {"adapter": self.name, "status": state["status"], "response_code": None, "reason_codes": state["reason_codes"]}
        try:
            token_body = json.dumps({"appId": self.app_id, "clientSecret": self.app_secret}).encode("utf-8")
            token_req = urllib.request.Request(self.token_url, data=token_body, method="POST", headers={"Content-Type": "application/json", "Accept": "application/json"})
            token_code, token_data = self._request_json(token_req)
            access_token = str(token_data.get("access_token") or "")
            if not (200 <= token_code < 300) or not access_token:
                reason = "QQBOT_AUTH_FAILED" if token_code in (401, 403) or not access_token else f"QQBOT_HTTP_STATUS:{token_code}"
                return {"adapter": self.name, "status": "FAILED", "response_code": token_code, "reason_codes": [reason]}
            text = str(payload.get("event", payload).get("message") if isinstance(payload.get("event", payload), Mapping) else payload.get("message") or "QQ Bot reminder")
            body = json.dumps({"content": text[:2000], "msg_type": 0}).encode("utf-8")
            url = self.message_url.format(openid=urllib.parse.quote(self.openid, safe=""))
            req = urllib.request.Request(url, data=body, method="POST", headers={"Authorization": f"QQBot {access_token}", "Content-Type": "application/json", "Accept": "application/json"})
            code, _ = self._request_json(req)
            if 200 <= code < 300:
                return {"adapter": self.name, "status": "DELIVERED", "response_code": code, "reason_codes": []}
            reason = "QQBOT_RATE_LIMITED" if code == 429 else ("QQBOT_AUTH_FAILED" if code in (401, 403) else f"QQBOT_HTTP_STATUS:{code}")
            return {"adapter": self.name, "status": "FAILED", "response_code": code, "reason_codes": [reason]}
        except (TimeoutError, urllib.error.URLError, OSError, ValueError) as exc:
            return {"adapter": self.name, "status": "FAILED", "response_code": None, "reason_codes": ["QQBOT_TIMEOUT" if isinstance(exc, TimeoutError) else f"QQBOT_ERROR:{type(exc).__name__}"]}


class QQBotGatewayTransport:
    """Official QQ Bot Gateway transport, isolated behind an injectable loop.

    The transport owns credentials and websocket I/O; it never logs protocol
    payloads.  ``start`` is non-blocking so the localhost dashboard remains
    responsive.  Tests may inject ``session_factory`` and ``sleep``.
    """
    TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
    GATEWAY_URL = "https://api.sgroup.qq.com/gateway/bot"
    INTENTS = 1 << 25  # C2C_MESSAGE_CREATE

    def __init__(self, app_id: str, app_secret: str, *, max_retries: int = 5,
                 backoff_base: float = 1.0, session_factory: Any | None = None):
        self.app_id, self.app_secret = str(app_id).strip(), str(app_secret).strip()
        self.max_retries = max(0, int(max_retries)); self.backoff_base = max(0.0, float(backoff_base))
        self.session_factory = session_factory
        self.status = "STOPPED"; self.error: str | None = None
        self._callback: Callable[[Mapping[str, Any]], Any] | None = None
        self._stop = threading.Event(); self._thread: threading.Thread | None = None
        self._token = ""; self._session_id: str | None = None; self._seq: int | None = None
        self._ws: Any = None

    @staticmethod
    def identify(token: str) -> dict[str, Any]:
        return {"op": 2, "d": {"token": f"QQBot {token}", "intents": QQBotGatewayTransport.INTENTS,
                                "shard": [0, 1], "properties": {"$os": "windows", "$browser": "quant-lab", "$device": "quant-lab"}}}

    def resume(self, token: str, session_id: str, seq: int | None) -> dict[str, Any]:
        return {"op": 6, "d": {"token": f"QQBot {token}", "session_id": session_id, "seq": seq}}

    async def _json(self, session: Any, method: str, url: str, **kwargs: Any) -> Mapping[str, Any]:
        request_method = getattr(session, method.lower(), None)
        request = request_method(url, **kwargs) if request_method else session.request(method, url, **kwargs)
        if asyncio.iscoroutine(request): request = await request
        async with request as response:
            value = response.json(content_type=None)
            data = await value if asyncio.iscoroutine(value) else value
            return data if isinstance(data, Mapping) else {}

    async def _connect_once(self) -> None:
        if aiohttp is None and self.session_factory is None:
            raise RuntimeError("AIOHTTP_UNAVAILABLE")
        factory = self.session_factory or aiohttp.ClientSession
        async with factory() as session:
            token_data = await self._json(session, "POST", self.TOKEN_URL,
                json={"appId": self.app_id, "clientSecret": self.app_secret})
            token = str(token_data.get("access_token") or "")
            if not token: raise RuntimeError("AUTH_FAILED")
            self._token = token
            gateway_data = await self._json(session, "GET", self.GATEWAY_URL,
                headers={"Authorization": f"QQBot {token}"})
            ws_url = str(gateway_data.get("url") or "")
            if not ws_url: raise RuntimeError("GATEWAY_UNAVAILABLE")
            async with session.ws_connect(ws_url, heartbeat=None) as ws:
                self._ws = ws
                hello = await ws.receive_json()
                if not isinstance(hello, Mapping) or int(hello.get("op", -1)) != 10: raise RuntimeError("HELLO_INVALID")
                interval = float((hello.get("d") or {}).get("heartbeat_interval", 45000)) / 1000.0
                if self._session_id:
                    await ws.send_json(self.resume(token, self._session_id, self._seq))
                else:
                    await ws.send_json(self.identify(token))
                next_heartbeat = asyncio.get_running_loop().time() + max(0.1, interval)
                while not self._stop.is_set():
                    timeout = max(0.05, min(interval, next_heartbeat - asyncio.get_running_loop().time()))
                    try:
                        message = await asyncio.wait_for(ws.receive_json(), timeout=timeout)
                    except asyncio.TimeoutError:
                        await ws.send_json({"op": 1, "d": self._seq}); next_heartbeat = asyncio.get_running_loop().time() + max(0.1, interval); continue
                    if not isinstance(message, Mapping): continue
                    op = int(message.get("op", -1)); data = message.get("d")
                    if op == 11: continue
                    if op == 0:
                        if message.get("s") is not None: self._seq = int(message["s"])
                        if isinstance(data, Mapping) and data.get("session_id"): self._session_id = str(data["session_id"])
                        if message.get("t") == "READY": self.status = "CONNECTED"
                        if self._callback: self._callback(message)
                    elif op in (7, 9):
                        raise RuntimeError("SESSION_RECONNECT")

    async def _run(self) -> None:
        attempts = 0
        while not self._stop.is_set():
            try:
                self.status = "CONNECTING" if attempts == 0 else "BACKOFF"
                await self._connect_once(); attempts = 0
            except Exception as exc:
                self.error = f"GATEWAY_{'AUTH_FAILED' if str(exc) == 'AUTH_FAILED' else 'ERROR'}"
                attempts += 1
                if self._stop.is_set(): break
                if attempts > self.max_retries: self.status = "ERROR"; return
                self.status = "BACKOFF"
                delay = min(60.0, self.backoff_base * (2 ** (attempts - 1)))
                # Waiting on the thread event makes stop immediate even during
                # the maximum backoff interval.
                await asyncio.to_thread(self._stop.wait, delay)
        self.status = "STOPPED"

    def start(self, callback: Callable[[Mapping[str, Any]], Any]) -> None:
        if self._thread and self._thread.is_alive(): return
        self._callback = callback; self._stop.clear(); self.error = None
        self._thread = threading.Thread(target=lambda: asyncio.run(self._run()), name="qqbot-gateway", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread(): self._thread.join(timeout=2)
        self.status = "STOPPED"

    def reply(self, data: Mapping[str, Any], text: str) -> None:
        """Send only a binding acknowledgement, including the source msg_id."""
        # Gateway reply is intentionally performed through the existing API adapter.
        openid = str((data.get("author") or {}).get("user_openid") or "").strip()
        msg_id = str(data.get("id") or data.get("msg_id") or "").strip()
        if not openid or not msg_id or not self._token: return
        url = QQBotNotificationAdapter.message_url.format(openid=urllib.parse.quote(openid, safe=""))
        body = json.dumps({"content": text, "msg_type": 0, "msg_id": msg_id}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={"Authorization": f"QQBot {self._token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8) as response: response.read(1024)
        except (OSError, urllib.error.URLError, TimeoutError):
            return


class QQBotGateway:
    """Small, transport-neutral C2C binding gate for the official Gateway.

    A WebSocket transport can call ``handle_event`` with decoded Gateway
    payloads. Keeping transport separate makes replay/credential tests fully
    deterministic and prevents message bodies entering logs.
    """
    def __init__(self, service: Any, *, transport: Any | None = None):
        self.service, self.transport = service, transport
        self.status, self.error = "DISABLED", None
        self._seen: deque[str] = deque(maxlen=4096)
        self._seen_set: set[str] = set()
        self._lock = threading.Lock()

    def start(self) -> None:
        if self.transport is None:
            self.status = "WAITING_TRANSPORT"; return
        self.status = getattr(self.transport, "status", "CONNECTING")
        try:
            self.transport.start(self.handle_event)
            self.status = getattr(self.transport, "status", self.status)
        except Exception as exc:
            self.error = f"GATEWAY_ERROR:{type(exc).__name__}"; self.status = "ERROR"

    def stop(self) -> None:
        if self.transport is not None:
            try: self.transport.stop()
            except Exception: pass
        self.status = "STOPPED"

    def handle_event(self, event: Mapping[str, Any]) -> bool:
        if str(event.get("t") or "") == "READY":
            self.status = "CONNECTED"
            return False
        if str(event.get("t") or "") != "C2C_MESSAGE_CREATE": return False
        data = event.get("d") if isinstance(event.get("d"), Mapping) else event
        author = data.get("author") if isinstance(data.get("author"), Mapping) else {}
        user_openid = str(author.get("user_openid") or "").strip()
        content = str(data.get("content") or "")
        message_id = str(data.get("id") or data.get("msg_id") or "").strip()
        if not user_openid or content != "绑定" or not message_id: return False
        with self._lock:
            if message_id in self._seen_set: return False
            if len(self._seen) == self._seen.maxlen:
                self._seen_set.discard(self._seen.popleft())
            self._seen.append(message_id); self._seen_set.add(message_id)
        try:
            self.service.bind_qqbot_openid(user_openid)
        except PermissionError:
            return False
        except ValueError:
            return False
        if self.transport is not None:
            self.transport.reply(data, "绑定成功")
        return True


class NotificationService:
    def __init__(self, store: TradeCoachStore, *, adapter: NotificationAdapter | None = None):
        self.store = store
        if adapter is not None:
            self.adapter = adapter
        elif os.environ.get("TRADE_COACH_NOTIFY_ADAPTER", "webhook").strip().lower() == "qqbot":
            self.adapter = QQBotNotificationAdapter()
        else:
            self.adapter = WebhookNotificationAdapter()

    def status(self) -> dict[str, Any]:
        return {"schema_version": NOTIFICATION_SCHEMA_VERSION, **self.adapter.status(), "delivery_audit": self.store.notification_deliveries(10)}

    def send(self, payload: Mapping[str, Any], *, event_id: int | None = None) -> dict[str, Any]:
        envelope = {"schema_version": NOTIFICATION_SCHEMA_VERSION, "event_id": event_id, "sent_at": now_cn().isoformat(), "event": dict(payload)}
        result = self.adapter.send(envelope)
        audit_id = self.store.append_notification_delivery(event_id=event_id, adapter=str(result.get("adapter") or self.adapter.name), status=str(result.get("status") or "FAILED"), attempted_at=now_cn(), response_code=result.get("response_code"), error_code=(result.get("reason_codes") or [None])[0], payload={"event_id": event_id, "event": dict(payload), "result": result})
        return {**result, "audit_id": audit_id}


def _parse_dt(value: Any, *, date_anchor: date | None = None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return as_cn(value)
    text = str(value).strip()
    if re.fullmatch(r"\d{14}", text):
        return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=CN_TZ)
    if re.fullmatch(r"\d{12}", text):
        return datetime.strptime(text, "%Y%m%d%H%M").replace(tzinfo=CN_TZ)
    if re.fullmatch(r"\d{10,13}", text):
        seconds = int(text) / (1000 if len(text) == 13 else 1)
        return datetime.fromtimestamp(seconds, UTC).astimezone(CN_TZ)
    if re.fullmatch(r"\d{8}", text):
        return datetime.strptime(text, "%Y%m%d").replace(tzinfo=CN_TZ)
    if date_anchor and re.fullmatch(r"\d{2}:\d{2}:\d{2}(?:\.\d+)?", text):
        fmt = "%H:%M:%S.%f" if "." in text else "%H:%M:%S"
        return datetime.combine(date_anchor, datetime.strptime(text, fmt).time()).replace(tzinfo=CN_TZ)
    try:
        return as_cn(text)
    except (TypeError, ValueError):
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=CN_TZ)
            except ValueError:
                continue
    return None


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader]


class RealMarketCollector:
    """Import existing evidence and explicitly fetch real public data."""

    def __init__(self, store: TradeCoachStore, *, project_root: str | Path | None = None, timeout: float = 8.0, fred_timeout: float = 20.0, fred_retries: int = 2):
        self.store = store
        self.project_root = local_path(project_root or Path.cwd())
        self.timeout = timeout
        self.fred_timeout = max(float(fred_timeout), float(timeout))
        self.fred_retries = max(0, int(fred_retries))
        # FRED series are fetched independently, but the shared manifest must
        # still be updated serially.  Without this lock parallel refreshes can
        # lose another series' manifest entry during read/modify/replace.
        self._fred_cache_lock = threading.Lock()

    def ingest_local_evidence(self) -> dict[str, int]:
        counts = {"forward": 0, "history": 0, "sector": 0, "macro": 0, "tin": 0}
        forward_db = self.project_root / "data" / "forward_probe" / "quant_lab_foundation.sqlite3"
        if forward_db.is_file():
            counts["forward"] = self._ingest_forward_db(forward_db)
        history_root = self.project_root / "data" / "tencent_2010_20260819"
        if history_root.is_dir():
            counts["history"] = self._ingest_local_history(history_root)
        sector_root = self.project_root / "data" / "tushare_801050_2010_20260819"
        if sector_root.is_dir():
            counts["sector"] = self._ingest_local_sector_history(sector_root)
        macro_root = self.project_root / "data" / "trade_coach" / "source_cache"
        if macro_root.is_dir():
            counts["macro"] = self._ingest_local_fred_history(macro_root)
            counts["tin"] = self._ingest_local_tin_history(macro_root)
        return counts

    def _ingest_local_tin_history(self, root: Path) -> int:
        """Ingest the append-only Tushare SN.SHF main-contract archive.

        Rows are never treated as back-adjusted.  Every observation retains
        the concrete mapped contract so a roll gap remains auditable.
        """
        path = root / "tushare_tin_main_history_v1.jsonl"
        if not path.is_file():
            return 0
        observed: list[MarketObservation] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("schema_version") != "tushare_tin_main_history_v1" or row.get("source") != "TUSHARE" or row.get("product") != "SN.SHF":
                    return 0
                day = str(row.get("trade_date") or "")
                stamp = _parse_dt(day)
                available = _parse_dt(row.get("available_at"))
                contract = str(row.get("mapping_ts_code") or "")
                close = _finite(row.get("close"))
                if stamp is None or available is None or close is None or not re.fullmatch(r"SN\d{4}\.SHF", contract):
                    return 0
                observed.append(MarketObservation(
                    "TIN", "TUSHARE_TIN_MAIN local", available,
                    stamp.replace(hour=15, minute=0, second=0),
                    _finite(row.get("open")), _finite(row.get("high")), _finite(row.get("low")), close,
                    _finite(row.get("volume")), close, "READY", source_ref=str(path),
                    raw_hash=canonical_hash(row), timestamp_precision="date",
                    mapping_version=str(row.get("mapping_version") or "tushare_fut_mapping_v1"),
                    contract_mapping={"product": "SN.SHF", "contract": contract, "trade_date": day, "series_semantics": row.get("series_semantics"), "roll_is_unadjusted": True},
                ))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 0
        return self.store.append_observations(observed)

    def _ingest_forward_db(self, path: Path) -> int:
        try:
            connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
        except (OSError, sqlite3.Error):
            return 0
        observations: list[MarketObservation] = []
        try:
            rows = connection.execute("SELECT * FROM probe_evidence ORDER BY id").fetchall()
        except sqlite3.Error:
            connection.close()
            return 0
        for row in rows:
            symbol = str(row["symbol"])
            mapped = {"AG0": "SILVER", "AU0": "GOLD", "SN0": "TIN", "SC0": "OIL"}.get(symbol, symbol)
            if mapped not in SPEC_BY_SYMBOL:
                continue
            observed = _parse_dt(row["run_id"] if False else row["primary_exchange_time"] or row["backup_exchange_time"]) or now_cn()
            # probe_runs contains the true receive time; retain source times in
            # the observation and use the source row's own timestamp when present.
            for prefix in ("primary", "backup"):
                source = str(row[f"{prefix}_source"] or "unknown")
                close = _finite(row[f"{prefix}_close"])
                status = str(row[f"{prefix}_status"] or "MISSING")
                exchange = _parse_dt(row[f"{prefix}_exchange_time"])
                reasons = tuple(str(item) for item in json_load(row[f"{prefix}_reason_codes"], []))
                mapping = json_load(row["contract_mapping"], {})
                if close is None and status == "READY":
                    status = "MISSING"
                    reasons = (*reasons, "CLOSE_MISSING")
                raw_hash = canonical_hash({"row_id": row["id"], "prefix": prefix, "close": close, "exchange": iso(exchange)})
                item = MarketObservation(mapped, source, observed, exchange, None, None, None, close, _finite(row.get("volume") if hasattr(row, "get") else None), None, status if status in STATUSES else "UNKNOWN", reasons, source_ref=str(path), raw_hash=raw_hash, latency_ms=_finite(row[f"{prefix}_latency_ms"]), mapping_version=str(row["mapping_version"] or "unverified"), contract_mapping=mapping)
                observations.append(item)
        connection.close()
        return self.store.append_observations(observations)

    def _ingest_local_history(self, root: Path) -> int:
        observations: list[MarketObservation] = []
        for symbol, code in (("000426.XSHE", "sz000426"), ("000960.XSHE", "sz000960")):
            raw_path = root / f"{code}_raw.csv"
            adjusted_path = root / f"{code}_post_adjusted.csv"
            if not raw_path.is_file():
                continue
            adjusted: dict[str, float] = {}
            if adjusted_path.is_file():
                for row in _csv_rows(adjusted_path)[-400:]:
                    value = _finite(row.get("close"))
                    if value is not None:
                        adjusted[str(row.get("date", "")).strip()] = value
            rows = _csv_rows(raw_path)[-400:]
            for row in rows:
                day = str(row.get("date", "")).strip()
                stamp = _parse_dt(day)
                close = _finite(row.get("close"))
                if not day or stamp is None or close is None:
                    continue
                values = {key: _finite(row.get(key)) for key in ("open", "high", "low", "volume")}
                item = MarketObservation(symbol, "Tencent historical local", stamp, stamp.replace(hour=15, minute=0, second=0), values["open"], values["high"], values["low"], close, values["volume"], adjusted.get(day), "READY", source_ref=str(raw_path), raw_hash=canonical_hash({"path": str(raw_path), "date": day, "close": close}), timestamp_precision="date")
                observations.append(item)
        return self.store.append_observations(observations)

    def _ingest_local_sector_history(self, root: Path) -> int:
        """Ingest the verified Tushare 801050 close-only archive.

        This archive is a separate index contract and is never synthesized
        from the two equity series.  The manifest is retained in source_ref so
        a UI/API consumer can trace the exact local snapshot used.
        """
        path = root / "801050.SI_daily.csv"
        if not path.is_file():
            return 0
        manifest = root / "manifest.json"
        manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.is_file() else None
        observations: list[MarketObservation] = []
        for row in _csv_rows(path):
            day = str(row.get("date", "")).strip()
            stamp = _parse_dt(day)
            close = _finite(row.get("close"))
            if not day or stamp is None or close is None:
                continue
            values = {key: _finite(row.get(key)) for key in ("open", "high", "low", "volume")}
            source_ref = f"{path}#manifest_sha256={manifest_hash}" if manifest_hash else str(path)
            item = MarketObservation("801050.SI", "TUSHARE_INDEX_DAILY local", stamp, stamp.replace(hour=15, minute=0, second=0), values["open"], values["high"], values["low"], close, values["volume"], close, "READY", source_ref=source_ref, raw_hash=canonical_hash({"path": str(path), "date": day, "close": close}), timestamp_precision="date", mapping_version="quant_lab_sector_snapshot_v1", contract_mapping={"source": "TUSHARE_INDEX_DAILY", "semantics": "close_only_index_series", "manifest_sha256": manifest_hash})
            observations.append(item)
        return self.store.append_observations(observations)

    def _ingest_local_fred_history(self, root: Path) -> int:
        """Ingest a hash-manifested FRED cache when the public endpoint is slow.

        A cache is accepted only with an explicit fetch timestamp and a
        matching file hash.  This makes the fallback a proven local snapshot,
        not a value silently stamped with the current clock.
        """
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            return 0
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            fetched_at = _parse_dt(manifest.get("fetched_at"))
            files = manifest.get("files")
            if fetched_at is None or not isinstance(files, Mapping):
                return 0
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 0
        observations: list[MarketObservation] = []
        for spec in INSTRUMENT_SPECS:
            if spec.source != "FRED":
                continue
            meta = files.get(spec.provider_symbol)
            if not isinstance(meta, Mapping):
                continue
            relative = str(meta.get("path") or f"fred_{spec.provider_symbol}.csv")
            path = (root / relative).resolve()
            try:
                if not path.is_file() or path.parent != root.resolve():
                    continue
                expected_hash = str(meta.get("sha256") or "")
                actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                if expected_hash and expected_hash != actual_hash:
                    continue
                source_ref = str(meta.get("source_ref") or f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={spec.provider_symbol}")
                for row in _csv_rows(path):
                    day = str(row.get("observation_date", "") or row.get("date", "")).strip()
                    stamp = _parse_dt(day)
                    close = _finite(row.get(spec.provider_symbol) or row.get("value"))
                    if stamp is None or close is None:
                        continue
                    item = MarketObservation(spec.symbol, "FRED", fetched_at, stamp, None, None, None, close, None, close, "READY", source_ref=f"{source_ref}#local_sha256={actual_hash}", raw_hash=canonical_hash({"path": str(path), "date": day, "close": close, "sha256": actual_hash}), timestamp_precision="date", mapping_version="fred_local_cache_v1", contract_mapping={"source": "FRED", "fetched_at": iso(fetched_at), "sha256": actual_hash})
                    observations.append(item)
            except (OSError, ValueError, TypeError, csv.Error):
                continue
        return self.store.append_observations(observations)

    def _http(self, url: str, *, headers: Mapping[str, str] | None = None, timeout: float | None = None) -> tuple[str, float]:
        request = urllib.request.Request(url, headers={"User-Agent": "Quant-Lab-Personal-Trade-Coach/0.1", **dict(headers or {})}, method="GET")
        started = datetime.now(UTC)
        with urllib.request.urlopen(request, timeout=self.timeout if timeout is None else timeout) as response:  # nosec B310 fixed public providers
            body = response.read().decode("utf-8", errors="replace")
        return body, (datetime.now(UTC) - started).total_seconds() * 1000

    def _yahoo_history(self, spec: InstrumentSpec, observed_at: datetime) -> tuple[list[MarketObservation], dict[str, Any]]:
        encoded = urllib.parse.quote(spec.provider_symbol, safe="=")
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range=6mo&interval=1d&includePrePost=false&events=div%2Csplits"
        try:
            text, latency = self._http(url)
            payload = json.loads(text)
            result = (((payload.get("chart") or {}).get("result") or [None])[0])
            if not isinstance(result, dict):
                return [_empty_observation(spec.symbol, "Yahoo Finance", observed_at, "YAHOO_EMPTY_RESPONSE", url)], {"source": "Yahoo Finance", "url": url}
            timestamps = result.get("timestamp") or []
            quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
            adj = ((result.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or []
            observations: list[MarketObservation] = []
            for index, epoch in enumerate(timestamps):
                stamp = _parse_dt(epoch)
                if stamp is None:
                    continue
                values = {key: _finite((quote.get(key) or [None] * len(timestamps))[index]) for key in ("open", "high", "low", "close", "volume")}
                if values["close"] is None:
                    continue
                adjusted = _finite(adj[index]) if index < len(adj) else None
                observations.append(MarketObservation(spec.symbol, "Yahoo Finance", observed_at, stamp, values["open"], values["high"], values["low"], values["close"], values["volume"], adjusted, "READY", source_ref=url, raw_hash=canonical_hash({"url": url, "epoch": epoch, "close": values["close"]}), latency_ms=latency, timestamp_precision="day"))
            if not observations:
                return [_empty_observation(spec.symbol, "Yahoo Finance", observed_at, "YAHOO_NO_VALID_BARS", url)], {"source": "Yahoo Finance", "url": url}
            return observations, {"source": "Yahoo Finance", "url": url, "bars": len(observations), "latency_ms": latency}
        except (OSError, urllib.error.URLError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return [_empty_observation(spec.symbol, "Yahoo Finance", observed_at, f"YAHOO_FETCH_ERROR:{type(exc).__name__}", url)], {"source": "Yahoo Finance", "url": url, "error": type(exc).__name__}

    def _fred_history(self, spec: InstrumentSpec, observed_at: datetime) -> tuple[list[MarketObservation], dict[str, Any]]:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={urllib.parse.quote(spec.provider_symbol)}"
        last_exc: Exception | None = None
        for attempt in range(self.fred_retries + 1):
          try:
            text, latency = self._http(url, headers={"Accept": "text/csv"}, timeout=self.fred_timeout)
            rows = list(csv.DictReader(text.splitlines()))
            observations: list[MarketObservation] = []
            for row in rows[-260:]:
                day = str(row.get("observation_date", "")).strip()
                stamp = _parse_dt(day)
                close = _finite(row.get(spec.provider_symbol))
                if stamp is None or close is None:
                    continue
                observations.append(MarketObservation(spec.symbol, "FRED", observed_at, stamp.astimezone(UTC), None, None, None, close, None, close, "READY", source_ref=url, raw_hash=canonical_hash({"url": url, "date": day, "close": close}), latency_ms=latency, timestamp_precision="date"))
            if not observations:
                return [_empty_observation(spec.symbol, "FRED", observed_at, "FRED_NO_VALID_OBSERVATIONS", url)], {"source": "FRED", "url": url}
            # Publish only validated observations; cache and manifest each use
            # atomic replacement and retain the existing other series.
            try:
                from .source_cache import update_fred_cache
                cache_rows = [{"observation_date": row.get("observation_date"), "value": row.get(spec.provider_symbol)} for row in rows]
                with self._fred_cache_lock:
                    cache_detail = update_fred_cache(self.project_root / "data" / "trade_coach" / "source_cache", spec.provider_symbol, cache_rows, fetched_at=observed_at, source_ref=url)
            except (OSError, ValueError, TypeError, csv.Error) as exc:
                cache_detail = {"status": "CACHE_UPDATE_FAILED", "reason": type(exc).__name__}
            return observations, {"source": "FRED", "url": url, "observations": len(observations), "latency_ms": latency, "attempts": attempt + 1, "cache": cache_detail}
          except (OSError, urllib.error.URLError, ValueError, KeyError, csv.Error) as exc:
            last_exc = exc
        error = type(last_exc).__name__ if last_exc else "UnknownError"
        return [_empty_observation(spec.symbol, "FRED", observed_at, f"FRED_FETCH_ERROR:{error}", url)], {"source": "FRED", "url": url, "error": error, "attempts": self.fred_retries + 1}

    def _tencent_quote(self, spec: InstrumentSpec, observed_at: datetime) -> tuple[MarketObservation, dict[str, Any]]:
        url = f"https://qt.gtimg.cn/q={spec.provider_symbol}"
        try:
            text, latency = self._http(url, headers={"Referer": "https://gu.qq.com/"})
            fields = text.split('"', 2)[1].split("~")
            if len(fields) < 35:
                return _empty_observation(spec.symbol, "Tencent", observed_at, "TENCENT_SCHEMA_UNSUPPORTED", url), {"source": "Tencent", "url": url}
            stamp = _parse_dt(fields[30], date_anchor=date.today()) if not re.fullmatch(r"\d{8}", fields[30]) else _parse_dt(fields[30] + fields[31])
            values = [_finite(fields[index]) for index in (5, 33, 34, 3, 6)]
            if stamp is None or values[3] is None:
                return _empty_observation(spec.symbol, "Tencent", observed_at, "TENCENT_TIMESTAMP_OR_CLOSE_MISSING", url), {"source": "Tencent", "url": url}
            item = MarketObservation(spec.symbol, "Tencent", observed_at, stamp, values[0], values[1], values[2], values[3], values[4], None, "READY", source_ref=url, raw_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(), latency_ms=latency)
            return item, {"source": "Tencent", "url": url, "latency_ms": latency}
        except (OSError, urllib.error.URLError, IndexError, ValueError) as exc:
            return _empty_observation(spec.symbol, "Tencent", observed_at, f"TENCENT_FETCH_ERROR:{type(exc).__name__}", url), {"source": "Tencent", "url": url, "error": type(exc).__name__}

    def _eastmoney_sector(self, spec: InstrumentSpec, observed_at: datetime) -> tuple[MarketObservation, dict[str, Any]]:
        url = f"https://push2.eastmoney.com/api/qt/stock/get?secid=2.{spec.provider_symbol}&fields=f43,f44,f45,f46,f47,f86,f57,f58"
        try:
            text, latency = self._http(url, headers={"Referer": "https://quote.eastmoney.com/"})
            payload = json.loads(text)
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict) or data.get("f43") is None:
                return _empty_observation(spec.symbol, "Eastmoney", observed_at, "EASTMONEY_SECTOR_FIELDS_MISSING", url), {"source": "Eastmoney", "url": url}
            stamp = _parse_dt(data.get("f86")) or observed_at
            item = MarketObservation(spec.symbol, "Eastmoney", observed_at, stamp, _finite(data.get("f46")), _finite(data.get("f44")), _finite(data.get("f45")), _finite(data.get("f43")), _finite(data.get("f47")), None, "READY", source_ref=url, raw_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(), latency_ms=latency)
            return item, {"source": "Eastmoney", "url": url, "latency_ms": latency}
        except (OSError, urllib.error.URLError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return _empty_observation(spec.symbol, "Eastmoney", observed_at, f"EASTMONEY_FETCH_ERROR:{type(exc).__name__}", url), {"source": "Eastmoney", "url": url, "error": type(exc).__name__}

    def refresh(self, *, include_live: bool = True) -> dict[str, Any]:
        started = now_cn()
        local_counts = self.ingest_local_evidence()
        details: list[dict[str, Any]] = []
        appended = 0
        observed_at = now_cn()
        if include_live:
            fred_specs = [spec for spec in INSTRUMENT_SPECS if spec.source == "FRED"]
            fred_details: dict[str, tuple[list[MarketObservation], dict[str, Any]]] = {}
            # Four FRED series are independent.  Fetch them together so one
            # slow provider does not multiply the refresh wall-clock time by
            # four.  Each worker retains its own bounded timeout/retry policy;
            # failures remain per-series MISSING and local cache ingestion is
            # never promoted to a fresh observation.
            with ThreadPoolExecutor(max_workers=len(fred_specs) or 1, thread_name_prefix="fred-refresh") as pool:
                futures = {spec.symbol: pool.submit(self._fred_history, spec, observed_at) for spec in fred_specs}
                for spec in fred_specs:
                    fred_details[spec.symbol] = futures[spec.symbol].result()
            for spec in INSTRUMENT_SPECS:
                if spec.symbol in STOCK_SYMBOLS:
                    observation, detail = self._tencent_quote(spec, observed_at)
                    appended += int(self.store.append_observation(observation))
                    details.append({"symbol": spec.symbol, **detail, "status": observation.status, "reason_codes": list(observation.reason_codes)})
                elif spec.symbol == "801050.SI":
                    observation, detail = self._eastmoney_sector(spec, observed_at)
                    appended += int(self.store.append_observation(observation))
                    details.append({"symbol": spec.symbol, **detail, "status": observation.status, "reason_codes": list(observation.reason_codes)})
                elif spec.source == "FRED":
                    observations, detail = fred_details[spec.symbol]
                    appended += self.store.append_observations(observations)
                    details.append({"symbol": spec.symbol, **detail, "status": observations[-1].status, "reason_codes": list(observations[-1].reason_codes)})
                elif spec.symbol == "TIN":
                    # The verified SHFE main-contract archive is refreshed by
                    # the separate read-only VPS exporter.  Do not query the
                    # ambiguous Yahoo ``SN=F`` symbol or invent a fallback.
                    details.append({"symbol": spec.symbol, "source": "TUSHARE_TIN_MAIN local", "status": "READY" if local_counts.get("tin") else "MISSING", "reason_codes": [] if local_counts.get("tin") else ["TIN_PIT_HISTORY_UNAVAILABLE"]})
                else:
                    observations, detail = self._yahoo_history(spec, observed_at)
                    appended += self.store.append_observations(observations)
                    details.append({"symbol": spec.symbol, **detail, "status": observations[-1].status, "reason_codes": list(observations[-1].reason_codes)})
        completed = now_cn()
        payload = {"local": local_counts, "appended_observations": appended, "details": details, "network": include_live, "source_policy": "MISSING/STALE remain explicit"}
        self.store.append_refresh_run(mode="LIVE_AND_LOCAL" if include_live else "LOCAL_ONLY", status="COMPLETE", payload=payload, started_at=started, completed_at=completed)
        return {"status": "COMPLETE", "started_at": iso(started), "completed_at": iso(completed), **payload}


@dataclass(frozen=True)
class VpsFacts:
    status: str
    source_ref: str | None
    observed_at: datetime | None
    generated_at: datetime | None
    valid_until: datetime | None
    risk_level: str | None
    prediction_gate_status: str
    macro_event_gate: str | None
    reason_codes: tuple[str, ...]
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source_ref": self.source_ref,
            "observed_at": iso(self.observed_at),
            "generated_at": iso(self.generated_at),
            "valid_until": iso(self.valid_until),
            "risk_level": self.risk_level,
            "prediction_gate_status": self.prediction_gate_status,
            "macro_event_gate": self.macro_event_gate,
            "reason_codes": list(self.reason_codes),
            "payload": dict(self.payload),
        }


def load_vps_facts(path: str | Path | None = None, *, decision_at: datetime | None = None) -> VpsFacts:
    """Load only an explicit VPS export; missing calendar remains MISSING."""
    selected = path or os.environ.get("QUANT_LAB_VPS_FACT_PATH")
    if not selected:
        return VpsFacts("MISSING", None, None, None, None, None, "MISSING", None, ("VPS_FACT_PATH_UNSET",), {})
    try:
        source_path = local_path(selected)
        if not source_path.is_file():
            return VpsFacts("MISSING", str(source_path), None, None, None, None, "MISSING", None, ("VPS_FACT_FILE_MISSING",), {})
        text = source_path.read_text(encoding="utf-8")
        records: list[Mapping[str, Any]] = []
        if source_path.suffix.lower() == ".json":
            decoded = json.loads(text)
            if isinstance(decoded, dict):
                records = [decoded]
            elif isinstance(decoded, list):
                records = [item for item in decoded if isinstance(item, dict)]
        else:
            for line in text.splitlines():
                if line.strip():
                    item = json.loads(line)
                    if isinstance(item, dict):
                        records.append(item)
        # The VPS PIT publisher wraps the actual point-in-time record in a
        # ``snapshot`` envelope and keeps the publish status alongside it.
        # Normalize that envelope here while retaining the outer status and
        # error fields.  Without this step a captured MISSING snapshot would
        # look like an invalid export simply because its timestamps and gate
        # live one level deeper.
        normalized_records: list[Mapping[str, Any]] = []
        for item in records:
            snapshot = item.get("snapshot") if isinstance(item, Mapping) else None
            if isinstance(snapshot, Mapping):
                normalized = dict(snapshot)
                for key in ("status", "missing_field_list", "reason_codes", "pit_errors", "macro_event_gate", "prediction_gate_status", "valid_until"):
                    if key in item:
                        normalized[key] = item[key]
                normalized_records.append(normalized)
            else:
                normalized_records.append(item)
        records = normalized_records
        if not records:
            return VpsFacts("INVALID", str(source_path), None, None, None, None, "INVALID", None, ("VPS_FACT_NO_RECORD",), {})
        decision = as_cn(decision_at or now_cn())
        def gen(item: Mapping[str, Any]) -> datetime | None:
            return _parse_dt(item.get("generated_at") or item.get("as_of_time") or item.get("observed_at"))
        records.sort(key=lambda item: gen(item) or datetime.min.replace(tzinfo=CN_TZ))
        eligible = [item for item in records if gen(item) is not None and gen(item) <= decision]
        if not eligible:
            return VpsFacts("MISSING", str(source_path), None, None, None, None, "MISSING", None, ("VPS_FACT_NO_PIT_RECORD",), {})
        record = max(eligible, key=lambda item: gen(item) or datetime.min.replace(tzinfo=CN_TZ))
        # A dry-run snapshot or calendar cache with unavailable fields is kept
        # as a fact of absence, never upgraded to GREEN.
        missing = [str(item) for item in (record.get("missing_field_list") or [])]
        reasons = [str(item) for item in (record.get("reason_codes") or [])]
        reasons.extend(str(item) for item in (record.get("pit_errors") or []))
        event_gate = record.get("macro_event_gate")
        event_gate_text = event_gate.strip() if isinstance(event_gate, str) else ""
        raw_prediction_gate = record.get("prediction_gate_status", record.get("prediction_gate"))
        if isinstance(raw_prediction_gate, Mapping):
            raw_prediction_gate = raw_prediction_gate.get("status")
        prediction_gate_text = raw_prediction_gate.strip().upper() if isinstance(raw_prediction_gate, str) else ""
        if prediction_gate_text in {"ACTIVE", "GREEN", "PASS", "PASSED", "VALID", "ALLOW", "ALLOWED", "OPEN"}:
            prediction_gate_status = "ACTIVE"
        elif prediction_gate_text:
            prediction_gate_status = prediction_gate_text
        else:
            prediction_gate_status = "MISSING"
        if isinstance(record.get("status"), str) and record.get("status", "").upper() == "MISSING":
            reasons.append("VPS_EXPORT_STATUS_MISSING")
        if missing:
            reasons.append("VPS_EXPORT_FIELDS_MISSING")
        if any("EVENT_CALENDAR_UNAVAILABLE" in item for item in reasons) or not event_gate_text or event_gate_text.upper() == "EVENT_CALENDAR_UNAVAILABLE":
            reasons.append("EVENT_CALENDAR_UNAVAILABLE")
            return VpsFacts("MISSING", str(source_path), gen(record), gen(record), _parse_dt(record.get("valid_until")), None, "MISSING", None, tuple(dict.fromkeys(reasons)), record)
        if not prediction_gate_text or prediction_gate_text in {"MISSING", "UNKNOWN", "UNAVAILABLE", "INVALID", "ERROR"}:
            reasons.append("PREDICTION_GATE_UNAVAILABLE")
            return VpsFacts("MISSING", str(source_path), gen(record), gen(record), _parse_dt(record.get("valid_until")), None, "MISSING", event_gate_text, tuple(dict.fromkeys(reasons)), record)
        if str(record.get("status") or "").upper() == "MISSING" or missing:
            return VpsFacts("MISSING", str(source_path), gen(record), gen(record), _parse_dt(record.get("valid_until")), None, prediction_gate_status, event_gate_text, tuple(dict.fromkeys(reasons)), record)
        required = {"risk_level", "generated_at", "valid_until", "source", "model_version"}
        if not required.issubset(record):
            return VpsFacts("INVALID", str(source_path), gen(record), gen(record), _parse_dt(record.get("valid_until")), None, prediction_gate_status, event_gate_text, tuple(dict.fromkeys((*reasons, "VPS_FACT_SCHEMA_INCOMPLETE"))), record)
        generated = _parse_dt(record.get("generated_at"))
        valid_until = _parse_dt(record.get("valid_until"))
        if generated is None or valid_until is None:
            return VpsFacts("INVALID", str(source_path), gen(record), generated, valid_until, None, prediction_gate_status, event_gate_text, tuple(dict.fromkeys((*reasons, "VPS_FACT_TIME_INVALID"))), record)
        if valid_until < decision:
            return VpsFacts("STALE", str(source_path), gen(record), generated, valid_until, str(record.get("risk_level")), prediction_gate_status, event_gate_text, tuple(dict.fromkeys((*reasons, "VPS_FACT_EXPIRED"))), record)
        risk = str(record.get("risk_level"))
        if risk not in {"GREEN", "ORANGE", "RED"}:
            return VpsFacts("INVALID", str(source_path), gen(record), generated, valid_until, None, prediction_gate_status, event_gate_text, tuple(dict.fromkeys((*reasons, "VPS_RISK_LEVEL_INVALID"))), record)
        return VpsFacts("ACTIVE", str(source_path), gen(record), generated, valid_until, risk, prediction_gate_status, event_gate_text, tuple(dict.fromkeys(reasons)), record)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return VpsFacts("INVALID", str(selected), None, None, None, None, "INVALID", None, (f"VPS_FACT_PARSE_ERROR:{type(exc).__name__}",), {})


def _series_values(rows: Sequence[Mapping[str, Any]], *, adjusted: bool = False) -> list[float]:
    key = "adjusted_close" if adjusted else "close"
    values = []
    for row in rows:
        value = _finite(row.get(key))
        if value is None:
            value = _finite(row.get("close"))
        if value is not None and value > 0:
            values.append(value)
    return values


def trend_metric(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values = _series_values(rows, adjusted=True)
    if len(values) < 20:
        return {"status": "MISSING", "direction": None, "return_20d": None, "volatility": None, "sample_size": len(values), "reason_codes": ["INSUFFICIENT_20D_HISTORY"]}
    window = values[-20:]
    ret = window[-1] / window[0] - 1
    daily_returns = [window[index] / window[index - 1] - 1 for index in range(1, len(window)) if window[index - 1] > 0]
    volatility = pstdev(daily_returns) if len(daily_returns) > 1 else None
    average = mean(window)
    if ret >= 0.04 and window[-1] >= average:
        direction: int | None = 1
    elif ret <= -0.04 and window[-1] <= average:
        direction = -1
    else:
        direction = 0
    return {"status": "READY", "direction": direction, "return_20d": ret, "volatility": volatility, "sample_size": len(values), "last": window[-1], "ma20": average, "reason_codes": []}


def _fresh_status(row: Mapping[str, Any] | None, *, now: datetime, max_hours: int) -> str:
    if row is None or _finite(row.get("close")) is None:
        return "MISSING"
    if str(row.get("status")) not in {"READY", "STALE"}:
        return str(row.get("status") or "UNKNOWN")
    stamp = _parse_dt(row.get("exchange_time") or row.get("observed_at"))
    if stamp is None:
        return "MISSING"
    age = (now - stamp).total_seconds() / 3600
    if age > max_hours:
        return "STALE"
    return "READY"


def _source_public(row: Mapping[str, Any] | None, *, spec: InstrumentSpec, now: datetime) -> dict[str, Any]:
    status = _fresh_status(row, now=now, max_hours=spec.freshness_hours)
    if row is None:
        return {"source": None, "status": "MISSING", "close": None, "exchange_time": None, "observed_at": None, "latency_ms": None, "reason_codes": ["NO_SOURCE_EVIDENCE"], "source_ref": None}
    return {"source": row.get("source"), "status": status, "close": row.get("close"), "exchange_time": row.get("exchange_time"), "observed_at": row.get("observed_at"), "latency_ms": row.get("latency_ms"), "reason_codes": json_load(row.get("reason_codes"), []), "source_ref": row.get("source_ref"), "mapping_version": row.get("mapping_version"), "contract_mapping": json_load(row.get("contract_mapping"), {})}


def build_instrument_states(store: TradeCoachStore, *, now: datetime | None = None) -> list[dict[str, Any]]:
    current = as_cn(now or now_cn())
    result: list[dict[str, Any]] = []
    for spec in INSTRUMENT_SPECS:
        sources = store.latest_by_source(spec.symbol)
        usable_history = store.latest_usable_by_source(spec.symbol)
        public = [_source_public(row, spec=spec, now=current) for row in sources.values()]
        ready = [item for item in public if item["status"] == "READY" and item.get("close") is not None]
        # A newer MISSING probe must not hide an older, explicitly timestamped
        # price.  Prefer the newest usable evidence (READY, then STALE); when
        # none exists, expose the newest failure so the outage is still
        # visible.  This keeps source selection honest without turning stale
        # data into READY.
        usable = [item for item in public if item["status"] in {"READY", "STALE"} and item.get("close") is not None]
        # Keep the latest failed probe in ``sources``/primary/backup, but use
        # the newest prior close from the same source as an explicitly stale
        # fallback.  This prevents a one-off outage from turning a previously
        # evidenced price into an invented neutral state.
        seen_usable = {
            (str(item.get("source")), str(item.get("exchange_time") or item.get("observed_at")))
            for item in usable
        }
        for source, row in usable_history.items():
            item = _source_public(row, spec=spec, now=current)
            key = (str(item.get("source")), str(item.get("exchange_time") or item.get("observed_at")))
            if item.get("close") is not None and key not in seen_usable:
                usable.append(item)
                seen_usable.add(key)
        selected = max(ready or usable, key=lambda item: str(item.get("exchange_time") or item.get("observed_at") or "")) if (ready or usable) else (max(public, key=lambda item: str(item.get("exchange_time") or item.get("observed_at") or "")) if public else _source_public(None, spec=spec, now=current))
        primary_candidates = [item for item in public if str(item.get("source", "")).lower() == spec.primary_source.lower()]
        backup_candidates = [item for item in public if item not in primary_candidates]
        def latest(items: list[dict[str, Any]], fallback: dict[str, Any]) -> dict[str, Any]:
            return max(items, key=lambda item: str(item.get("exchange_time") or item.get("observed_at") or "")) if items else fallback
        primary = latest(primary_candidates, _source_public(None, spec=spec, now=current))
        backup = latest(backup_candidates, _source_public(None, spec=spec, now=current))
        # Expose both the newest probe (which may be a real failure) and the
        # last timestamped value that can be used as an explicit stale
        # fallback.  Consumers must never infer READY from this fallback.
        fallback_candidates = [item for item in usable if item.get("close") is not None]
        fallback = max(fallback_candidates, key=lambda item: str(item.get("exchange_time") or item.get("observed_at") or "")) if fallback_candidates else None
        if primary["status"] == "READY" and backup["status"] == "READY":
            reconciliation = "COMPLETE"
            selected_status = "READY"
        elif ready:
            reconciliation = "PARTIAL"
            selected_status = "READY" if selected["status"] == "READY" else "STALE"
        elif usable:
            reconciliation = "STALE"
            selected_status = "STALE"
        else:
            reconciliation = "MISSING"
            selected_status = selected["status"]
        latest_probe = max(public, key=lambda item: str(item.get("exchange_time") or item.get("observed_at") or "")) if public else _source_public(None, spec=spec, now=current)
        result.append({**spec.to_dict(), "primary": primary, "backup": backup, "sources": public,
                       "latest_probe": latest_probe, "fallback": fallback,
                       "selected": {**selected, "status": selected_status}, "reconciliation_status": reconciliation})
    return result


def evaluate_market_regime(store: TradeCoachStore) -> dict[str, Any]:
    metrics = {symbol: trend_metric(store.history(symbol, 260)) for symbol in ("SILVER", "GOLD", "TIN", "DXY", "REAL10Y", "801050.SI")}
    available = {symbol: value for symbol, value in metrics.items() if value["status"] == "READY" and value["direction"] is not None}
    missing = [symbol for symbol, value in metrics.items() if value["status"] != "READY"]
    if len(available) < 3:
        code = "UNKNOWN"
        label = "未知（证据不足）"
        confidence = "LOW"
    else:
        score = 0
        for symbol in ("SILVER", "GOLD", "TIN", "801050.SI"):
            score += int(available.get(symbol, {}).get("direction") or 0)
        score -= int(available.get("DXY", {}).get("direction") or 0)
        score -= int(available.get("REAL10Y", {}).get("direction") or 0)
        if score >= 3:
            code, label = "TREND_UP", "趋势上涨"
        elif score <= -3:
            code, label = "DOWN", "明确下跌"
        else:
            code, label = "RANGE", "区间震荡"
        confidence = "HIGH" if len(available) >= 5 else "MEDIUM"
    return {"code": code, "label": label, "confidence": confidence, "score": locals().get("score"), "metrics": metrics, "available_symbols": sorted(available), "missing_symbols": sorted(missing), "evidence_status": "COMPLETE" if len(available) >= 5 and not missing else ("PARTIAL" if available else "MISSING"), "rule": "商品与板块同向计分；DXY与实际利率方向取反；缺失不作中性替代"}


def evaluate_stock_state(store: TradeCoachStore) -> dict[str, Any]:
    own = trend_metric(store.history("000426.XSHE", 260))
    silver = trend_metric(store.history("SILVER", 260))
    sector = trend_metric(store.history("801050.SI", 260))
    if any(item["status"] != "READY" for item in (own, silver, sector)):
        return {"code": "DATA_INSUFFICIENT", "label": "数据不足", "evidence_status": "INCOMPLETE", "own": own, "silver": silver, "sector": sector, "reason_codes": ["STOCK_COMMODITY_SECTOR_COMPARISON_INCOMPLETE"]}
    own_ret, silver_ret, sector_ret = own["return_20d"], silver["return_20d"], sector["return_20d"]
    assert own_ret is not None and silver_ret is not None and sector_ret is not None
    if own_ret - max(silver_ret, sector_ret) >= 0.06:
        code, label = "STRONGER", "强于商品和板块"
    elif min(silver_ret, sector_ret) - own_ret >= 0.06:
        code, label = "WEAKER", "弱于商品和板块"
    elif abs(own_ret - silver_ret) <= 0.03 and abs(own_ret - sector_ret) <= 0.03:
        code, label = "SYNC", "与商品、板块同步"
    else:
        code, label = "ANOMALY", "个股异常"
    return {"code": code, "label": label, "evidence_status": "COMPLETE", "own": own, "silver": silver, "sector": sector, "reason_codes": []}


def risk_assessment(vps: VpsFacts, *, company_risk_confirmed: bool = False) -> dict[str, Any]:
    systemic = vps.status == "ACTIVE" and vps.risk_level == "RED" and vps.prediction_gate_status == "ACTIVE" and vps.macro_event_gate not in {None, "EVENT_CALENDAR_UNAVAILABLE"}
    if systemic and company_risk_confirmed:
        status, label = "CONFIRMED_MAJOR_RISK", "重大风险已确认"
    elif systemic or company_risk_confirmed:
        status, label = "WATCH", "重大风险观察"
    elif vps.status in {"MISSING", "STALE", "INVALID"}:
        status, label = "UNKNOWN_DATA", "风险事实不足"
    else:
        status, label = "NONE", "无清仓级重大风险"
    return {"status": status, "label": label, "company_risk_confirmed": company_risk_confirmed, "systemic_risk_confirmed": systemic, "vps": vps.to_dict(), "exit_allowed": status == "CONFIRMED_MAJOR_RISK", "rule": "只有公司风险与系统性风险均有确认事实才允许 EXIT_MAJOR_RISK；缺失不构成清仓理由"}


def _round_lot(value: float, *, minimum: int = 0) -> int:
    return max(minimum, int(math.floor(max(0.0, value) / 100.0) * 100))


def _mentor_chain(regime: Mapping[str, Any], stock: Mapping[str, Any], risk: Mapping[str, Any], previous: Mapping[str, Any] | None) -> list[dict[str, str]]:
    chain = [
        {"step": "事实", "title": "先看证据", "text": f"当前大环境为{regime.get('label')}；可用因子 {len(regime.get('available_symbols', []))} 个，缺失 {len(regime.get('missing_symbols', []))} 个。", "reasoning_kind": "DETERMINISTIC_RULES"},
        {"step": "传导", "title": "解释传导", "text": "白银先连接实际利率与美元，再观察黄金、锡和申万有色；不能把单一商品涨跌直接当成个股结论。", "reasoning_kind": "DETERMINISTIC_RULES"},
        {"step": "个股", "title": "检查个股", "text": f"兴业银锡相对商品与板块状态为：{stock.get('label')}。", "reasoning_kind": "DETERMINISTIC_RULES"},
        {"step": "风险", "title": "风险门槛", "text": f"风险状态：{risk.get('label')}。{risk.get('rule')}", "reasoning_kind": "DETERMINISTIC_RULES"},
        {"step": "反证", "title": "保留反证", "text": "若白银、板块与个股重新同步，或当前来源由 READY 变为 STALE/MISSING，现建议必须重新评估。", "reasoning_kind": "DETERMINISTIC_RULES"},
    ]
    if previous:
        prior_payload = previous.get("payload") if isinstance(previous, Mapping) else {}
        prior_label = (prior_payload or {}).get("regime_label") or "上一份判断"
        chain.insert(1, {"step": "延续", "title": "接上上一份判断", "text": f"上一份判断是：{prior_label}。本次只记录新事实与变化，不重新编造一套独立故事。", "reasoning_kind": "DETERMINISTIC_RULES"})
    return chain


def build_advice(store: TradeCoachStore, regime: Mapping[str, Any], stock: Mapping[str, Any], risk: Mapping[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    current = as_cn(now or now_cn())
    account = store.account()
    confirmed = account.get("confirmed")
    current_shares = int(confirmed["shares"]) if confirmed and confirmed.get("shares") is not None else None
    price = None
    states = build_instrument_states(store, now=current)
    own_state = next(item for item in states if item["symbol"] == "000426.XSHE")
    price = _finite((own_state.get("selected") or {}).get("close"))
    evidence_status = "COMPLETE" if regime.get("evidence_status") == "COMPLETE" and stock.get("evidence_status") == "COMPLETE" and confirmed else ("ACCOUNT_PENDING_CONFIRMATION" if not confirmed else "INCOMPLETE")
    action = "WAIT"
    share_range: list[int | None] = [None, None]
    step_size = 100
    trigger: list[str] = []
    invalidation: list[str] = []
    if risk.get("exit_allowed") and current_shares is not None:
        action = "EXIT_MAJOR_RISK"
        share_range = [0, 0]
        trigger = ["公司风险与系统性风险均已由有效时点事实确认"]
        invalidation = ["任一重大风险证据被证伪或失效；在此之前仍需人工确认"]
    elif evidence_status == "COMPLETE" and current_shares is not None:
        if regime.get("code") == "TREND_UP":
            investable_assets = _finite(confirmed.get("total_assets"))
            planned_cash_out = _finite(confirmed.get("planned_cash_out")) or 0.0
            if investable_assets is not None:
                investable_assets = max(0.0, investable_assets - planned_cash_out)
            capacity = _round_lot(investable_assets * 0.65 / price) if price and investable_assets is not None else None
            low = _round_lot(max(current_shares * 0.75, 100))
            high = max(low, _round_lot(max(current_shares, capacity or current_shares)))
            share_range = [low, high]
            action = "HOLD" if low <= current_shares <= high else ("ADD_IN_STEPS" if current_shares < low else "REDUCE_IN_STEPS")
            trigger = ["白银、申万有色与个股继续站上20日趋势", "若回撤后商品和板块重新同步，可按100股分批"]
            invalidation = ["DXY与实际利率同步上行且板块跌破20日趋势", "个股公告或交易状态出现重大风险"]
        elif regime.get("code") == "RANGE":
            low = _round_lot(max(0, current_shares - 200))
            high = _round_lot(current_shares)
            share_range = [low, high]
            action = "HOLD" if low <= current_shares <= high else ("ADD_IN_STEPS" if current_shares < low else "REDUCE_IN_STEPS")
            trigger = ["反弹但白银未恢复", "801050继续弱于20日趋势时，每次100股减仓"]
            invalidation = ["白银与板块重新同步转强", "个股重新强于商品和板块"]
        elif regime.get("code") == "DOWN":
            low = _round_lot(current_shares * 0.33)
            high = _round_lot(current_shares * 0.67)
            share_range = [low, max(low, high)]
            action = "REDUCE_IN_STEPS" if current_shares > high else "HOLD"
            trigger = ["明确下跌趋势延续时分批减仓，不以普通下跌作为清仓理由"]
            invalidation = ["商品和板块同时收复20日趋势并且个股同步转强"]
        else:
            trigger = ["等待关键因子补齐且状态稳定"]
            invalidation = ["任何新增证据必须重新评估"]
    else:
        trigger = ["先完成账户候选快照人工确认", "数据完整后才计算动态持仓区间"]
        invalidation = ["缺失或过期数据不能支持加仓、减仓或清仓"]
    mentor = _mentor_chain(regime, stock, risk, store.latest_narrative())
    advice = {
        "schema_version": SCHEMA_VERSION,
        "as_of": current.date().isoformat(),
        "generated_at": current.isoformat(),
        "symbol": "000426.XSHE",
        "market_regime": regime.get("code"),
        "market_regime_label": regime.get("label"),
        "stock_state": stock.get("code"),
        "stock_state_label": stock.get("label"),
        "current_shares": current_shares,
        "recommended_share_range": share_range,
        "action": action,
        "step_size": step_size,
        "trigger_conditions": trigger,
        "invalidation_conditions": invalidation,
        "major_risk_status": risk.get("status"),
        "major_risk_label": risk.get("label"),
        "major_risk_evidence": risk.get("vps"),
        "confidence": "HIGH" if evidence_status == "COMPLETE" and regime.get("confidence") == "HIGH" else ("MEDIUM" if evidence_status == "COMPLETE" else "LOW"),
        "evidence_status": evidence_status,
        "supporting_evidence": [f"大环境：{regime.get('label')}", f"个股状态：{stock.get('label')}", f"风险：{risk.get('label')}"],
        "opposing_evidence": [f"缺失因子：{', '.join(regime.get('missing_symbols', []))}" if regime.get("missing_symbols") else "当前无额外缺失因子", "普通下跌不满足清仓门槛"],
        "mentor_chain": mentor,
        "reasoning_kind": "DETERMINISTIC_RULES",
        "ai_override_allowed": False,
        "manual_confirmation_required": True,
        "automatic_trading": False,
    }
    if action not in ADVICE_ACTIONS:
        raise AssertionError("illegal action generated")
    return advice


def build_narrative(regime: Mapping[str, Any], stock: Mapping[str, Any], advice: Mapping[str, Any], previous: Mapping[str, Any] | None, *, now: datetime | None = None) -> dict[str, Any]:
    current = as_cn(now or now_cn())
    prior_payload = (previous or {}).get("payload", {}) if isinstance(previous, Mapping) else {}
    prior_regime = prior_payload.get("regime") if isinstance(prior_payload, Mapping) else None
    prior_stock = prior_payload.get("stock_state") if isinstance(prior_payload, Mapping) else None
    current_summary = f"当前市场为{regime.get('label')}，兴业银锡{stock.get('label')}；建议动作为{advice.get('action')}，但所有操作仍需人工确认。"
    # Polling the same facts should keep the same narrative revision.  This
    # preserves the previous wording (including its evidence/change lists)
    # instead of manufacturing a second "unchanged" revision on the next
    # dashboard read.
    if previous and isinstance(prior_payload, Mapping) and prior_regime == regime.get("code") and prior_stock == stock.get("code") and prior_payload.get("summary") == current_summary and prior_payload.get("position_adjustment") == advice.get("recommended_share_range") and prior_payload.get("evidence_status") == advice.get("evidence_status"):
        preserved = dict(prior_payload)
        preserved["generated_at"] = current.isoformat()
        preserved["as_of"] = current.date().isoformat()
        return preserved
    unchanged: list[str] = []
    new_facts: list[str] = []
    affirmed: list[str] = []
    falsified: list[str] = []
    if prior_regime == regime.get("code"):
        unchanged.append(f"市场模式仍为{regime.get('label')}")
    else:
        new_facts.append(f"市场模式由{prior_regime or '首次判断'}变为{regime.get('label')}")
        if prior_regime:
            affirmed.append("模式切换由当前可用因子重新计算")
    if prior_stock == stock.get("code"):
        unchanged.append(f"个股状态仍为{stock.get('label')}")
    else:
        new_facts.append(f"兴业银锡个股状态为{stock.get('label')}")
        if prior_stock:
            falsified.append("上一份个股相对强弱判断不再完全适用")
    if not new_facts and not unchanged:
        new_facts.append("本次没有足以改变判断的新事实")
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": current.date().isoformat(),
        "generated_at": current.isoformat(),
        "prior_narrative_id": previous.get("id") if previous else None,
        "original_judgement": prior_payload.get("summary") if isinstance(prior_payload, Mapping) else None,
        "summary": current_summary,
        "regime": regime.get("code"),
        "regime_label": regime.get("label"),
        "stock_state": stock.get("code"),
        "stock_state_label": stock.get("label"),
        "unchanged": unchanged,
        "new_facts": new_facts,
        "affirmed": affirmed,
        "falsified": falsified,
        "position_adjustment": advice.get("recommended_share_range"),
        "evidence_status": advice.get("evidence_status"),
        "reasoning_kind": "DETERMINISTIC_RULES",
        "ai_override_allowed": False,
    }


class TradeCoachService:
    """Application service used by the localhost dashboard routes."""

    def __init__(self, db_path: str | Path, *, project_root: str | Path | None = None, vps_path: str | Path | None = None, bootstrap: bool = True, mentor_provider: MentorProvider | None = None, evidence_verifier: PublicEvidenceVerifier | None = None, notification_service: NotificationService | None = None, credential_backend: Any | None = None):
        self.store = TradeCoachStore(db_path)
        self.project_root = local_path(project_root or Path(db_path).resolve().parents[1])
        self.credential_backend = credential_backend or (DockerSecretCredentialBackend() if os.environ.get("QQBOT_APP_SECRET_FILE") else WindowsCredentialBackend())
        self._qqbot_binding_lock = threading.Lock()
        self.collector = RealMarketCollector(self.store, project_root=self.project_root)
        self.vps_path = vps_path
        # Unit/fixture services created with ``bootstrap=False`` must never
        # inherit live credentials from the shell.  The real application uses
        # the explicit DeepSeek-first/MiMo-fallback chain only on bootstrap.
        self.mentor_provider = mentor_provider or MultiMentorProvider(use_environment=bootstrap)
        self.evidence_verifier = evidence_verifier or PublicEvidenceVerifier()
        self.notifications = notification_service or NotificationService(self.store)
        settings_path, _ = _qqbot_paths(self.project_root)
        if bootstrap:
            try:
                cfg = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
                secret = self.credential_backend.read()
                managed_app = getattr(self.credential_backend, "app_id", lambda: None)()
                managed_openid = getattr(self.credential_backend, "openid", lambda: None)()
                if managed_app: cfg["app_id"] = managed_app
                if managed_openid: cfg["openid"] = managed_openid
                if cfg.get("app_id") and secret:
                    self.notifications.adapter = QQBotNotificationAdapter(app_id=str(cfg["app_id"]), app_secret=secret, openid=str(cfg.get("openid") or ""))
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        self.qqbot_gateway = None
        self._configure_qqbot_gateway()
        if bootstrap:
            self.collector.ingest_local_evidence()
            self.rebuild_analysis()

    @classmethod
    def for_forward_database(cls, forward_db: str | Path) -> "TradeCoachService":
        path = local_path(forward_db)
        root = path.parent.parent
        return cls(root / "trade_coach" / "trade_coach.sqlite3", project_root=root.parent, bootstrap=True)

    def _facts(self) -> VpsFacts:
        facts = load_vps_facts(self.vps_path)
        self.store.append_vps_fact(facts.to_dict())
        return facts

    def _ai_context(self, analysis: Mapping[str, Any], instruments: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Build a bounded, evidence-only context for an optional provider."""
        source_refs = []
        for item in instruments:
            selected = item.get("selected") if isinstance(item, Mapping) else None
            if isinstance(selected, Mapping) and selected.get("source_ref"):
                source_refs.append(str(selected["source_ref"]))
        return {
            "as_of": analysis.get("advice", {}).get("as_of"),
            "market_regime": analysis.get("regime"),
            "stock_state": analysis.get("stock"),
            "risk": analysis.get("risk"),
            "deterministic_advice": analysis.get("advice"),
            "deterministic_rule_chain": analysis.get("advice", {}).get("mentor_chain", []),
            "source_references": list(dict.fromkeys(source_refs))[:12],
            "guardrails": {"action_must_remain_deterministic": True, "manual_confirmation_required": True, "automatic_trading": False, "missing_is_not_neutral": True},
        }

    def ai_status(self) -> dict[str, Any]:
        provider_status = self.mentor_provider.status()
        latest = self.store.latest_ai_run()
        last_success = bool(latest and latest.get("status") == "READY" and isinstance(latest.get("result"), Mapping) and latest["result"].get("is_ai"))
        return {"schema_version": AI_SCHEMA_VERSION, **provider_status, "latest_call_status": latest.get("status") if latest else "NOT_CALLED", "last_call_succeeded": last_success, "latest_run": latest, "memory_retrieval": "LOCAL_TOKEN_OVERLAP", "explanation_only": True, "cannot_override_rule_action": True}

    def generate_ai_mentor(self, *, verify_sources: bool = True) -> dict[str, Any]:
        """Run the explicit AI stage after rules, memory retrieval and source checks."""
        started = now_cn()
        analysis = self.rebuild_analysis()
        instruments = build_instrument_states(self.store)
        context = self._ai_context(analysis, instruments)
        query = " ".join(str(value) for value in (analysis.get("regime", {}).get("label"), analysis.get("stock", {}).get("label"), analysis.get("advice", {}).get("action"), "长期主线 反证 风险"))
        memories = self.store.search_memories(query, 12)
        references = context.get("source_references", [])
        verification = self.evidence_verifier.verify(references, requested=verify_sources)
        result = self.mentor_provider.explain(context=context, memories=memories, verification=verification)
        completed = now_cn()
        request_hash = str(result.get("request_hash") or canonical_hash({"context": context, "memory_ids": [item.get("id") for item in memories], "verification": verification}))
        audit_id = self.store.append_ai_run(request_hash=request_hash, provider=str(result.get("provider") or self.mentor_provider.name), model=result.get("model"), status=str(result.get("status") or "PROVIDER_ERROR"), started_at=started, completed_at=completed, memory_ids=[int(item["id"]) for item in memories if item.get("id") is not None], verification=verification, response_hash=result.get("response_hash"), result=result, error_code=(result.get("reason_codes") or [None])[0])
        if result.get("status") == "READY" and result.get("is_ai") and isinstance(result.get("structured_output"), Mapping):
            self.store.append_diary(layer="导师判断", content={"reasoning_kind": "AI_PROVIDER", "ai_run_id": audit_id, "provider": result.get("provider"), "model": result.get("model"), "memory_ids": result.get("memory_ids", []), "verification": verification, "structured_output": result.get("structured_output"), "rule_action_reference": analysis.get("advice", {}).get("action"), "automatic_trading": False})
        return {**result, "ai_run_id": audit_id, "generated_at": completed.isoformat(), "deterministic_advice": analysis.get("advice"), "memory_retrieval": {"query": query, "count": len(memories), "memory_ids": [item.get("id") for item in memories]}, "cross_validation": verification}

    def account_financials(self, account: Mapping[str, Any] | None = None, instruments: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        """Calculate transparent manual-account marks without inventing prices.

        The confirmed snapshot is the starting fact.  Only manually recorded
        executions after that snapshot affect the cash/realised-P&L walk; when
        a current raw price is unavailable, mark-to-market fields stay null.
        """
        state = account or self.store.account()
        confirmed = state.get("confirmed") if isinstance(state, Mapping) else None
        if not confirmed:
            return {"status": "PENDING_USER_CONFIRMATION", "reason_codes": ["ACCOUNT_NOT_CONFIRMED"], "current_price": None, "market_value": None, "cost_basis": None, "unrealized_pnl": None, "realized_pnl": None, "cash_estimate": None, "equity_estimate": None, "planned_cash_out": None}
        instrument_rows = list(instruments or build_instrument_states(self.store))
        own = next((item for item in instrument_rows if item.get("symbol") == "000426.XSHE"), {})
        selected = own.get("selected") if isinstance(own, Mapping) else {}
        current_price = _finite(selected.get("close")) if isinstance(selected, Mapping) and selected.get("status") == "READY" else None
        shares = int(confirmed.get("shares") or 0)
        avg_cost = _finite(confirmed.get("avg_cost"))
        cash = _finite(confirmed.get("available_cash"))
        baseline_time = _parse_dt(confirmed.get("captured_at"))
        realized = 0.0
        open_shares = shares
        open_cost = (avg_cost or 0.0) * shares if avg_cost is not None else None
        for trade in reversed(self.store.trades(500)):
            if str(trade.get("execution_status")) != "EXECUTED_MANUALLY":
                continue
            trade_time = _parse_dt(trade.get("recorded_at"))
            if baseline_time and trade_time and trade_time < baseline_time:
                continue
            quantity = int(trade.get("quantity") or 0)
            price = _finite(trade.get("price"))
            fees = _finite(trade.get("fees")) or 0.0
            if quantity <= 0 or price is None:
                continue
            side = str(trade.get("side") or "").upper()
            if side == "BUY":
                open_shares += quantity
                if open_cost is not None:
                    open_cost += quantity * price + fees
                if cash is not None:
                    cash -= quantity * price + fees
            elif side == "SELL":
                if open_shares <= 0:
                    continue
                sell_quantity = min(quantity, open_shares)
                average_open_cost = open_cost / open_shares if open_cost is not None and open_shares else None
                if average_open_cost is not None:
                    realized += (price - average_open_cost) * sell_quantity - fees
                    open_cost -= average_open_cost * sell_quantity
                open_shares -= sell_quantity
                if cash is not None:
                    cash += sell_quantity * price - fees
        cost_basis = open_cost if open_cost is not None else None
        market_value = current_price * open_shares if current_price is not None else None
        unrealized = market_value - cost_basis if market_value is not None and cost_basis is not None else None
        equity = market_value + cash if market_value is not None and cash is not None else None
        status = "READY" if current_price is not None else "MISSING"
        return {"status": status, "reason_codes": [] if status == "READY" else ["CURRENT_RAW_PRICE_UNAVAILABLE"], "current_price": current_price, "market_value": market_value, "cost_basis": cost_basis, "unrealized_pnl": unrealized, "realized_pnl": realized, "cash_estimate": cash, "equity_estimate": equity, "open_shares": open_shares, "planned_cash_out": _finite(confirmed.get("planned_cash_out")) or 0.0, "source": selected.get("source") if isinstance(selected, Mapping) else None, "price_exchange_time": selected.get("exchange_time") if isinstance(selected, Mapping) else None}

    def _event_state(self, event_key: str, state: Mapping[str, Any], *, label: str) -> dict[str, Any]:
        """Add a stable transition description to a deduplicated event."""
        fingerprint = canonical_hash(dict(state))
        previous = self.store.latest_event(event_key)
        previous_payload = previous.get("payload", {}) if previous else {}
        if isinstance(previous_payload, Mapping) and previous_payload.get("state_fingerprint") == fingerprint:
            transition_from = previous_payload.get("transition_from")
        elif isinstance(previous_payload, Mapping):
            transition_from = previous_payload.get("state_label") or previous_payload.get("label")
        else:
            transition_from = None
        change_summary = f"由{transition_from}变化为{label}" if transition_from and transition_from != label else (f"状态保持：{label}" if previous else f"首次记录：{label}")
        return {"state_label": label, "state_fingerprint": fingerprint, "transition_from": transition_from, "change_summary": change_summary, **dict(state)}

    @staticmethod
    def _journal_once(rows: Sequence[Mapping[str, Any]], *, layer: str, marker: str, marker_value: str) -> bool:
        for row in rows:
            if row.get("layer") != layer:
                continue
            item = row.get("content") if isinstance(row.get("content"), Mapping) else {}
            if item.get(marker) == marker_value:
                return False
        return True

    def rebuild_analysis(self) -> dict[str, Any]:
        regime = evaluate_market_regime(self.store)
        stock = evaluate_stock_state(self.store)
        risk = risk_assessment(self._facts())
        advice = build_advice(self.store, regime, stock, risk)
        previous = self.store.latest_narrative()
        narrative = build_narrative(regime, stock, advice, previous)
        narrative_row = self.store.append_narrative(narrative)
        advice["narrative_id"] = narrative_row.get("id")
        advice_row = self.store.append_advice(advice)
        # Event payloads contain the complete reminder card and are deduplicated
        # by state, not by polling time.  A changed state gets one new row;
        # unchanged polling only updates last_seen.
        range_text = advice.get("recommended_share_range")
        common_reminder = {"impact_on_stock": "影响兴业银锡的相对强弱与仓位判断", "action": advice.get("action"), "recommended_share_range": range_text, "invalidation_conditions": advice.get("invalidation_conditions", [])}
        regime_event = self._event_state("market-regime", {"code": regime.get("code"), "evidence_status": regime.get("evidence_status"), "missing": sorted(regime.get("missing_symbols", []))}, label=str(regime.get("label")))
        self.store.upsert_event(event_key="market-regime", event_type="模式变化", payload={**regime_event, **common_reminder})
        health_label = f"{regime.get('evidence_status')} / 缺失 {len(regime.get('missing_symbols', []))} 个因子"
        health_event = self._event_state("data-health", {"status": regime.get("evidence_status"), "missing": sorted(regime.get("missing_symbols", []))}, label=health_label)
        self.store.upsert_event(event_key="data-health", event_type="数据源状态", payload={**health_event, **common_reminder, "reason_codes": [f"缺失：{', '.join(regime.get('missing_symbols', []))}"] if regime.get("missing_symbols") else []})
        stock_event = self._event_state("stock-state", {"code": stock.get("code"), "evidence_status": stock.get("evidence_status")}, label=str(stock.get("label")))
        self.store.upsert_event(event_key="stock-state", event_type="个股相对强弱", payload={**stock_event, **common_reminder, "impact_on_stock": "个股相对商品和801050的强弱决定是否需要调整仓位区间"})
        risk_event = self._event_state("risk-state", {"status": risk.get("status"), "reason_codes": risk.get("vps", {}).get("reason_codes", [])}, label=str(risk.get("label")))
        self.store.upsert_event(event_key="risk-state", event_type="风险门槛", payload={**risk_event, **common_reminder, "risk_rule": risk.get("rule")})
        if advice.get("action") != "WAIT":
            advice_event = self._event_state("advice-state", {"action": advice.get("action"), "range": advice.get("recommended_share_range"), "confidence": advice.get("confidence")}, label=str(advice.get("action")))
            self.store.upsert_event(event_key="advice-state", event_type="导师建议变化", payload={**advice_event, **common_reminder})
        # The five journal layers remain replayable and append-only.  Empty
        # execution/result layers are intentionally not fabricated.
        journal_rows = self.store.diary(500)
        fact_marker = canonical_hash({"as_of": advice.get("as_of"), "evidence_status": regime.get("evidence_status"), "missing": regime.get("missing_symbols", []), "metrics": regime.get("metrics", {})})
        if self._journal_once(journal_rows, layer="市场事实", marker="fact_marker", marker_value=fact_marker):
            self.store.append_diary(layer="市场事实", content={"fact_marker": fact_marker, "as_of": advice.get("as_of"), "market_facts": regime, "source_policy": "真实行情与VPS时点事实；缺失不转中性"})
            journal_rows = self.store.diary(500)
        mode_marker = canonical_hash({"as_of": advice.get("as_of"), "regime": regime.get("code"), "score": regime.get("score"), "stock": stock.get("code")})
        if self._journal_once(journal_rows, layer="模式判断", marker="mode_marker", marker_value=mode_marker):
            self.store.append_diary(layer="模式判断", content={"mode_marker": mode_marker, "as_of": advice.get("as_of"), "regime": regime.get("code"), "regime_label": regime.get("label"), "score": regime.get("score"), "stock_state": stock.get("code")})
            journal_rows = self.store.diary(500)
        if advice_row.get("id") and self._journal_once(journal_rows, layer="导师判断", marker="advice_hash", marker_value=str(advice_row.get("payload_hash"))):
            self.store.append_diary(layer="导师判断", content={"market_facts": regime, "stock_state": stock, "advice": advice, "advice_hash": advice_row.get("payload_hash"), "execution_status": "NOT_PERFORMED_BY_QUANT_LAB"})
        return {"regime": regime, "stock": stock, "risk": risk, "narrative": narrative, "advice": advice}

    def summary(self) -> dict[str, Any]:
        analysis = self.rebuild_analysis()
        account = self.store.account()
        instruments = build_instrument_states(self.store)
        financials = self.account_financials(account, instruments)
        diary = self.store.diary(12)
        memories = self.store.memories(30)
        events = self.store.events(30)
        vps = self._facts().to_dict()
        latest_refresh = self.store.refresh_runs(1)
        return {
            "schema_version": SCHEMA_VERSION,
            "product": "Personal Trade Coach v0.1",
            "read_only": True,
            "manual_execution_only": True,
            "automatic_trading": False,
            "generated_at": now_cn().isoformat(),
            "account": {"status": "CONFIRMED" if account.get("confirmed") else "PENDING_USER_CONFIRMATION", "confirmed": account.get("confirmed"), "candidate": account.get("candidate"), "history": account.get("history", []), "financials": financials},
            "market_regime": analysis["regime"],
            "stock_state": analysis["stock"],
            "risk": analysis["risk"],
            "advice": analysis["advice"],
            "narrative": analysis["narrative"],
            "mentor_chain": analysis["advice"].get("mentor_chain", []),
            "deterministic_mentor_chain": analysis["advice"].get("mentor_chain", []),
            "ai": self.ai_status(),
            "notification": self.notifications.status(),
            "instruments": instruments,
            "events": events,
            "diary": diary,
            "memories": memories,
            "trades": self.store.trades(30),
            "vps": vps,
            "refresh": latest_refresh[0] if latest_refresh else None,
            "capabilities": {"manual_account_confirmation": True, "manual_trade_record": True, "real_market_refresh": True, "deterministic_rule_engine": True, "optional_ai_explanation": bool(self.ai_status().get("configured")), "notification_adapter": True, "broker_connection": False, "automatic_order": False, "fake_account": False, "random_market_data": False},
        }

    def timeline(self, symbol: str, limit: int = 120) -> dict[str, Any]:
        if symbol not in SPEC_BY_SYMBOL:
            raise ValueError("unknown symbol")
        spec = SPEC_BY_SYMBOL[symbol]
        rows = self.store.history(symbol, min(max(limit, 1), 500))
        return {"symbol": symbol, "label": spec.label, "contract": spec.to_dict(), "rows": rows, "status": "READY" if rows else "MISSING", "evidence_status": "COMPLETE" if rows else "MISSING"}

    def confirm_account(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            shares = int(payload.get("shares"))
            cost = float(payload.get("avg_cost"))
            cash = float(payload.get("available_cash"))
            total = float(payload.get("total_assets"))
            planned = float(payload.get("planned_cash_out", 0.0))
        except (TypeError, ValueError) as exc:
            raise ValueError("账户确认字段必须为数字") from exc
        note = str(payload.get("note") or "用户在本地终端确认候选快照")
        row_id = self.store.append_account_snapshot(status="CONFIRMED", shares=shares, avg_cost=cost, available_cash=cash, total_assets=total, planned_cash_out=planned, source="USER_EXPLICIT_CONFIRMATION", note=note)
        self.store.append_diary(layer="账户事实", content={"snapshot_id": row_id, "status": "CONFIRMED", "shares": shares, "avg_cost": cost, "available_cash": cash, "total_assets": total, "planned_cash_out": planned, "source": "USER_EXPLICIT_CONFIRMATION"})
        return self.store.account()

    def record_diary(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        layer = str(payload.get("layer") or "人工决定")
        content = payload.get("content") if isinstance(payload.get("content"), Mapping) else {"text": str(payload.get("text") or "")}
        if layer == "实际成交" and content.get("execution_status") == "EXECUTED_MANUALLY":
            self.store.append_trade(side=str(content.get("side", "")).upper(), quantity=int(content.get("quantity", 0)), price=_finite(content.get("price")), fees=_finite(content.get("fees")), execution_status="EXECUTED_MANUALLY", reason=str(content.get("reason") or ""))
        if layer == "长期记忆":
            text_value = str(content.get("text") or content.get("observation") or content.get("lesson") or "").strip()
            if text_value:
                self.store.append_memory(kind=str(content.get("kind") or "USER_OBSERVATION"), content=text_value, source="USER_DIARY")
        record = self.store.append_diary(layer=layer, content=content)
        return record

    def refresh(self, *, include_live: bool = True) -> dict[str, Any]:
        result = self.collector.refresh(include_live=include_live)
        result["analysis"] = self.rebuild_analysis()
        return result

    def _qqbot_status(self) -> dict[str, Any]:
        settings_path, _ = _qqbot_paths(self.project_root)
        data = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
        app_id = str(getattr(self.credential_backend, "app_id", lambda: None)() or data.get("app_id") or "").strip()
        status_fn = getattr(self.credential_backend, "secure_store_status", None)
        secure_status, reason = status_fn() if status_fn else ("READY", "")
        managed_openid = getattr(self.credential_backend, "openid", lambda: None)()
        bound = bool(str(managed_openid or data.get("openid") or "").strip())
        adapter_status = self.notifications.adapter.status()
        gateway = self.qqbot_gateway
        gateway_state = (getattr(getattr(gateway, "transport", None), "status", None)
                         or getattr(gateway, "status", None)
                         or getattr(self, "qqbot_gateway_status", "STOPPED"))
        if not app_id or not self.credential_backend.read(): gateway_state = "NOT_CONFIGURED"
        return {"has_secret": bool(self.credential_backend.read()), "app_id": app_id,
                "openid_bound": bound, "binding_state": "BOUND" if bound else ("WAITING_BINDING" if app_id else "NOT_CONFIGURED"),
                "gateway_status": gateway_state,
                "connection_error": getattr(gateway, "error", None),
                "secure_store_status": secure_status, "secure_store_reason": reason,
                "deployment_managed": bool(getattr(self.credential_backend, "managed", False))}

    def _configure_qqbot_gateway(self) -> None:
        """Create a gateway only when both credentials are available."""
        settings_path, _ = _qqbot_paths(self.project_root)
        try: cfg = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
        except (OSError, ValueError, json.JSONDecodeError): cfg = {}
        app_id = str(getattr(self.credential_backend, "app_id", lambda: None)() or cfg.get("app_id") or "").strip()
        secret = str(self.credential_backend.read() or "").strip()
        if app_id and secret:
            self.qqbot_gateway = QQBotGateway(self, transport=QQBotGatewayTransport(app_id, secret))
        else:
            self.qqbot_gateway = None

    def start_qqbot_gateway(self) -> None:
        self._qqbot_gateway_running = True
        if self.qqbot_gateway is None: self._configure_qqbot_gateway()
        if self.qqbot_gateway is not None: self.qqbot_gateway.start()

    def stop_qqbot_gateway(self) -> None:
        self._qqbot_gateway_running = False
        if self.qqbot_gateway is not None: self.qqbot_gateway.stop()

    def bind_qqbot_openid(self, openid: str) -> dict[str, Any]:
        """Atomically accept the first C2C OpenID; never overwrite a binding."""
        value = str(openid or "").strip()
        if getattr(self.credential_backend, "managed", False):
            raise PermissionError("QQBOT_OPENID_MANAGED_BY_DEPLOYMENT")
        if not value or len(value) > 256 or any(ord(c) < 0x20 for c in value):
            raise ValueError("QQBOT_OPENID_INVALID")
        settings_path, _ = _qqbot_paths(self.project_root)
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        with self._qqbot_binding_lock:
            current = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
            existing = str(current.get("openid") or "").strip()
            if existing and existing != value:
                raise PermissionError("QQBOT_ALREADY_BOUND")
            if existing == value:
                return self._qqbot_status()
            current["openid"] = value
            tmp = settings_path.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(current, ensure_ascii=False, sort_keys=True), encoding="utf-8")
                os.replace(tmp, settings_path)
            finally:
                if tmp.exists(): tmp.unlink()
            self.notifications.adapter.openid = value
        return self._qqbot_status()

    def clear_qqbot_binding(self) -> dict[str, Any]:
        if getattr(self.credential_backend, "managed", False):
            raise PermissionError("QQBOT_OPENID_MANAGED_BY_DEPLOYMENT")
        settings_path, _ = _qqbot_paths(self.project_root)
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        with self._qqbot_binding_lock:
            current = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
            current.pop("openid", None)
            tmp = settings_path.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(current, ensure_ascii=False, sort_keys=True), encoding="utf-8")
                os.replace(tmp, settings_path)
            finally:
                if tmp.exists(): tmp.unlink()
            self.notifications.adapter.openid = ""
        return self._qqbot_status()

    def _save_qqbot(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if getattr(self.credential_backend, "managed", False):
            raise PermissionError("QQBOT_SECRETS_MANAGED_BY_DEPLOYMENT")
        settings_path, _ = _qqbot_paths(self.project_root)
        existing = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
        secret = str(payload.get("app_secret") or "").strip(); app_id = str(payload.get("app_id") or "").strip() or str(existing.get("app_id") or "").strip()
        if not app_id: raise ValueError("首次配置必须填写 AppID")
        current = self.credential_backend.read()
        if not secret and not current: raise ValueError("首次配置必须填写 AppSecret")
        if len(secret) > 512: raise ValueError("AppSecret 长度受限")
        if len(app_id) > 128: raise ValueError("AppID 无效")
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        old_gateway = self.qqbot_gateway
        if old_gateway is not None: old_gateway.stop()
        if secret:
            try:
                self.credential_backend.write(secret)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1312 or (exc.args and exc.args[0] == 1312):
                    raise OSError("SECURE_STORE_UNAVAILABLE_CURRENT_LOGON_SESSION")
                raise
            current = secret
        current_secret = secret or current
        tmp = settings_path.with_suffix(".tmp")
        # OpenID is retained only for backwards-compatible storage; the UI has
        # no OpenID field and first binding is accepted exclusively by Gateway.
        retained_openid = str(existing.get("openid") or payload.get("openid") or "").strip()[:256]
        record = {"app_id": app_id}
        if retained_openid: record["openid"] = retained_openid
        try: tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8"); os.replace(tmp, settings_path)
        finally:
            if tmp.exists(): tmp.unlink()
        self.notifications.adapter = QQBotNotificationAdapter(app_id=app_id, app_secret=current_secret, openid=retained_openid)
        self._configure_qqbot_gateway()
        if getattr(self, "_qqbot_gateway_running", False) and self.qqbot_gateway is not None:
            self.qqbot_gateway.start()
        return self._qqbot_status()

    def handle(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        parsed = urllib.parse.urlparse(path)
        route = parsed.path.rstrip("/")
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if method == "GET" and route in {"/api/trade-coach", "/api/trade-coach/summary"}:
                return 200, self.summary()
            if method == "GET" and route == "/api/trade-coach/timeline":
                symbol = str((query.get("symbol") or ["000426.XSHE"])[0])
                limit = int((query.get("limit") or [120])[0])
                return 200, self.timeline(symbol, limit)
            if method == "GET" and route == "/api/trade-coach/holdings":
                account = self.store.account()
                return 200, {"status": "CONFIRMED" if account.get("confirmed") else "PENDING_USER_CONFIRMATION", **account, "financials": self.account_financials(account), "trades": self.store.trades(100)}
            if method == "GET" and route == "/api/trade-coach/advice":
                return 200, self.summary()["advice"]
            if method == "GET" and route == "/api/trade-coach/narrative":
                return 200, self.summary()["narrative"]
            if method == "GET" and route == "/api/trade-coach/events":
                return 200, {"events": self.store.events(100)}
            if method == "GET" and route == "/api/trade-coach/diary":
                return 200, {"diary": self.store.diary(200)}
            if method == "GET" and route == "/api/trade-coach/memory":
                return 200, {"memories": self.store.memories(200)}
            if method == "GET" and route == "/api/trade-coach/sources":
                return 200, {"instruments": build_instrument_states(self.store), "refresh_runs": self.store.refresh_runs(50), "vps": self._facts().to_dict()}
            if method == "GET" and route == "/api/trade-coach/ai":
                return 200, self.ai_status()
            if method == "GET" and route == "/api/trade-coach/notifications":
                return 200, self.notifications.status()
            if method == "GET" and route == "/api/trade-coach/qqbot/settings":
                return 200, self._qqbot_status()
            if method == "POST" and route == "/api/trade-coach/qqbot/settings":
                return 200, self._save_qqbot(body or {})
            if method == "DELETE" and route == "/api/trade-coach/qqbot/binding":
                return 200, self.clear_qqbot_binding()
            if method == "POST" and route == "/api/trade-coach/refresh":
                return 200, self.refresh(include_live=bool((body or {}).get("include_live", True)))
            if method == "POST" and route == "/api/trade-coach/ai/mentor":
                return 200, self.generate_ai_mentor(verify_sources=bool((body or {}).get("verify_sources", True)))
            if method == "POST" and route == "/api/trade-coach/notifications/test":
                return 200, self.notifications.send({"event_type": "TEST", "title": "Quant-Lab 通知适配层测试", "message": "这是一次人工触发的通知测试；不代表交易信号。", "action": "WAIT", "automatic_trading": False})
            if method == "POST" and route == "/api/trade-coach/notifications/send":
                event_id = int((body or {}).get("event_id"))
                event = self.store.event_by_id(event_id)
                if event is None:
                    raise ValueError("event_id not found")
                return 200, self.notifications.send({"event_type": event.get("event_type"), "event_key": event.get("event_key"), "payload": event.get("payload")}, event_id=event_id)
            if method == "POST" and route == "/api/trade-coach/account/confirm":
                return 200, self.confirm_account(body or {})
            if method == "POST" and route == "/api/trade-coach/diary":
                return 200, self.record_diary(body or {})
            if method == "POST" and route == "/api/trade-coach/trade":
                payload = body or {}
                row_id = self.store.append_trade(side=str(payload.get("side", "")).upper(), quantity=int(payload.get("quantity", 0)), price=_finite(payload.get("price")), fees=_finite(payload.get("fees")), execution_status=str(payload.get("execution_status", "PLANNED")), reason=str(payload.get("reason") or ""))
                diary_layer = "实际成交" if str(payload.get("execution_status", "PLANNED")) == "EXECUTED_MANUALLY" else "实际决定"
                self.store.append_diary(layer=diary_layer, content={"trade_record_id": row_id, **dict(payload), "automatic_trading": False})
                return 200, {"trade_record_id": row_id, "execution_status": str(payload.get("execution_status", "PLANNED")), "automatic_trading": False}
            return 404, {"error": "TRADE_COACH_ROUTE_NOT_FOUND"}
        except (ValueError, TypeError, OSError, sqlite3.Error) as exc:
            return 400, {"error": type(exc).__name__, "message": str(exc), "fail_closed": True}


__all__ = [
    "ADVICE_ACTIONS",
    "AI_SCHEMA_VERSION",
    "CN_TZ",
    "INSTRUMENT_SPECS",
    "InstrumentSpec",
    "MarketObservation",
    "DeepSeekMentorProvider",
    "MiMoMentorProvider",
    "MentorProvider",
    "MultiMentorProvider",
    "NotificationService",
    "OpenAICompatibleMentorProvider",
    "PublicEvidenceVerifier",
    "RealMarketCollector",
    "STATUSES",
    "TradeCoachService",
    "TradeCoachStore",
    "VpsFacts",
    "WebhookNotificationAdapter",
    "QQBotNotificationAdapter",
    "QQBotGatewayTransport",
    "QQBotGateway",
    "build_advice",
    "build_instrument_states",
    "build_narrative",
    "evaluate_market_regime",
    "evaluate_stock_state",
    "load_vps_facts",
    "risk_assessment",
    "trend_metric",
]
