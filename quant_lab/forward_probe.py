"""Sprint 1 market-data probe contracts and source adapters.

This module is deliberately probe-only.  It does not create signals, orders,
fills, or positions.  Network access happens only when ``ProbeRunner.run`` is
called explicitly by the ``quant-lab probe`` command.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Mapping, Protocol

from .tushare_futures_mapping import ALIASES as TUSHARE_FUTURES_ALIASES
from .tushare_futures_mapping import MAPPING_VERSION as TUSHARE_MAPPING_VERSION
from .tushare_futures_mapping import TushareMappingProvider


CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
STATUSES = frozenset({"READY", "MISSING", "STALE", "CONFLICT"})
EASTMONEY_FUTURES_SECIDS = {"AG0": "113.AG0", "AU0": "113.AU0", "SN0": "113.SN0", "SC0": "142.SC0"}
# This allowlist is intentionally empty in the frozen Sprint 1A baseline.  A
# symbol/version may be added only after a real-source mapping review; a
# caller-provided boolean alone must never promote a continuous alias.
DEFAULT_MAPPING_VERSION = "unverified"
VERIFIED_FUTURES_MAPPING_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    (symbol, TUSHARE_MAPPING_VERSION) for symbol in TUSHARE_FUTURES_ALIASES
)


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def parse_exchange_time(value: str | datetime) -> datetime:
    """Parse a source timestamp and require an explicit +08:00 offset."""
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() != timedelta(hours=8):
        raise ValueError("exchange_time must be timezone-aware Asia/Shanghai (+08:00)")
    return stamp.astimezone(CN_TZ)


def _parse_provider_time(value: str | datetime, second: str | None = None) -> datetime:
    """Parse a known Chinese provider's local timestamp without guessing fields."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=CN_TZ) if value.tzinfo is None else parse_exchange_time(value)
    text = str(value).strip()
    if re.fullmatch(r"\d{14}", text):
        return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=CN_TZ)
    if re.fullmatch(r"\d{8}", text) and second and re.fullmatch(r"\d{2}:\d{2}:\d{2}", second.strip()):
        return datetime.strptime(text + second.strip(), "%Y%m%d%H:%M:%S").replace(tzinfo=CN_TZ)
    if second and re.fullmatch(r"(?:\d{2}:\d{2}:\d{2}|\d{6})", second.strip()):
        time_text = second.strip()
        if re.fullmatch(r"\d{6}", time_text):
            time_text = f"{time_text[:2]}:{time_text[2:4]}:{time_text[4:]}"
        for date_pattern in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                date_value = datetime.strptime(text, date_pattern).date()
                time_value = datetime.strptime(time_text, "%H:%M:%S").time()
                return datetime.combine(date_value, time_value).replace(tzinfo=CN_TZ)
            except ValueError:
                continue
    if "+08:00" in text or text.endswith("Z"):
        return parse_exchange_time(text)
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=CN_TZ)
        except ValueError:
            continue
    raise ValueError("unsupported provider exchange timestamp")


def _parse_epoch_or_iso(value: object) -> datetime:
    text = str(value).strip()
    if re.fullmatch(r"20\d{10}", text):
        return datetime.strptime(text, "%Y%m%d%H%M").replace(tzinfo=CN_TZ)
    if re.fullmatch(r"20\d{12}", text):
        return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=CN_TZ)
    if re.fullmatch(r"\d{10,13}", text):
        number = int(text)
        seconds = number / 1000 if len(text) == 13 else number
        return datetime.fromtimestamp(seconds, CN_TZ)
    return parse_exchange_time(text)


def _parse_mootdx_bar_time(value: object) -> datetime:
    """Parse the explicit datetime returned by mootdx ``bars``."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=CN_TZ) if value.tzinfo is None else parse_exchange_time(value)
    text = str(value).strip().replace(" ", "T")
    if "+" not in text and not text.endswith("Z"):
        text += "+08:00"
    return parse_exchange_time(text)


def _parse_mootdx_quote_time(value: object, *, trade_date) -> datetime:
    """Combine only a strict HHMMSS(.fff) quote time with a bars date anchor."""
    text = str(value).strip()
    if not re.fullmatch(r"\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?", text):
        raise ValueError("unsupported mootdx quote time")
    pattern = "%H:%M:%S.%f" if "." in text else "%H:%M:%S"
    return datetime.combine(trade_date, datetime.strptime(text, pattern).time()).replace(tzinfo=CN_TZ)


@dataclass(frozen=True)
class Observation:
    """Immutable normalized OHLCV observation."""

    symbol: str
    source: str
    observed_at: datetime
    exchange_time: datetime | None
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    status: str
    reason_codes: tuple[str, ...] = field(default_factory=tuple)
    raw_ref: str | None = None
    latency_ms: float | None = None
    price_deviation_bps: float | None = None
    mapping_verified: bool = True
    mapping_version: str = DEFAULT_MAPPING_VERSION
    contract_mapping: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unsupported observation status: {self.status}")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() != timedelta(hours=8):
            raise ValueError("observed_at must be timezone-aware Asia/Shanghai (+08:00)")
        if self.exchange_time is not None:
            parse_exchange_time(self.exchange_time)
        if self.status == "READY":
            values = (self.open, self.high, self.low, self.close, self.volume)
            if self.exchange_time is None:
                raise ValueError("READY observation requires exchange_time")
            if any(value is None for value in values):
                raise ValueError("READY observation requires complete OHLCV")
            if any(value <= 0 for value in (self.open, self.high, self.low, self.close)):
                raise ValueError("READY observation requires positive prices")
            if self.volume < 0:
                raise ValueError("READY observation requires non-negative volume")
            if self.low > self.high or not self.low <= self.open <= self.high or not self.low <= self.close <= self.high:
                raise ValueError("invalid OHLC bounds")

    @property
    def ohlcv(self) -> dict[str, float | None]:
        return {"open": self.open, "high": self.high, "low": self.low, "close": self.close, "volume": self.volume}

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "source": self.source,
            "observed_at": self.observed_at.astimezone(CN_TZ).isoformat(),
            "exchange_time": self.exchange_time.astimezone(CN_TZ).isoformat() if self.exchange_time else None,
            **self.ohlcv,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "raw_ref": self.raw_ref,
            "latency_ms": self.latency_ms,
            "price_deviation_bps": self.price_deviation_bps,
            "mapping_verified": self.mapping_verified,
            "mapping_version": self.mapping_version,
            "contract_mapping": dict(self.contract_mapping) if self.contract_mapping else None,
        }


class SourceAdapter(Protocol):
    name: str

    def fetch(self, symbol: str, *, observed_at: datetime) -> tuple[Observation, str]:
        """Return normalized observation and the unmodified response text."""


def missing_observation(symbol: str, source: str, observed_at: datetime, reason: str, raw: str = "") -> tuple[Observation, str]:
    return Observation(symbol, source, observed_at, None, None, None, None, None, None, "MISSING", (reason,)), raw


class MootdxAdapter:
    """mootdx primary adapter; absent/unusable dependency fails closed."""

    name = "mootdx"

    def fetch(self, symbol: str, *, observed_at: datetime) -> tuple[Observation, str]:
        try:
            from mootdx.quotes import Quotes  # type: ignore  # optional operator-installed dependency
        except ImportError:
            return missing_observation(symbol, self.name, observed_at, "MOOTDX_NOT_INSTALLED")
        code = symbol.split(".", 1)[0]
        client = None
        try:
            client = Quotes.factory(market="std", multithread=False)
            # ``stocks`` enumerates the market and does not accept a symbol in
            # mootdx 0.11.7. ``quotes`` is the verified single-symbol L1 API.
            frame = client.quotes(symbol=code)
            if frame is None or len(frame) == 0:
                return missing_observation(symbol, self.name, observed_at, "MOOTDX_EMPTY_RESPONSE")
            row = frame.iloc[-1] if hasattr(frame, "iloc") else frame[-1]

            # quotes() exposes only HH:MM:SS(.fff). Anchor it to an explicit
            # full datetime from bars(); observed_at is never used as a date.
            bars = client.bars(symbol=code, frequency=0, start=0, offset=2)
            if bars is None or len(bars) == 0:
                return missing_observation(symbol, self.name, observed_at, "MOOTDX_BARS_EMPTY", repr(frame))
            bar_row = bars.iloc[-1] if hasattr(bars, "iloc") else bars[-1]

            def get(*names: str):
                for name in names:
                    try:
                        value = row[name]
                    except (KeyError, IndexError, TypeError):
                        continue
                    if value is not None:
                        return value
                raise KeyError(names[0])

            # The real-time quote response uses ``servertime``.  Do not use
            # observed_at or infer a timestamp when the source omits it.
            try:
                raw_time = get("servertime")
            except KeyError:
                return missing_observation(symbol, self.name, observed_at, "MOOTDX_TIMESTAMP_MISSING", repr(frame))
            try:
                bar_time_raw = bar_row["datetime"]
            except (KeyError, IndexError, TypeError):
                return missing_observation(symbol, self.name, observed_at, "MOOTDX_BARS_TIMESTAMP_MISSING", repr(bars))
            bar_time = _parse_mootdx_bar_time(bar_time_raw)
            if bar_time.date() != observed_at.astimezone(CN_TZ).date():
                return missing_observation(symbol, self.name, observed_at, "MOOTDX_BARS_DATE_MISMATCH", repr(bars))
            exchange_time = _parse_mootdx_quote_time(raw_time, trade_date=bar_time.date())
            if exchange_time.date() != bar_time.date():
                return missing_observation(symbol, self.name, observed_at, "MOOTDX_TIMESTAMP_DATE_MISMATCH", repr({"quotes": frame, "bars": bars}))
            values = [float(get("open")), float(get("high")), float(get("low")), float(get("price")), float(get("vol"))]
            # mootdx exposes a parsed frame rather than the wire payload; retain
            # the complete returned frame representation as the source evidence.
            return Observation(symbol, self.name, observed_at, exchange_time, *values, "READY"), repr({"quotes": frame, "bars": bars})
        except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            return missing_observation(symbol, self.name, observed_at, f"MOOTDX_FETCH_ERROR:{type(exc).__name__}")
        finally:
            closer = getattr(client, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    # Cleanup failures must not leak credentials or headers and
                    # do not turn an otherwise auditable source response into
                    # a fabricated observation.
                    pass


DEFAULT_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Quant-Lab-Local-Forward/0.1",
    "Accept": "*/*",
}


def _http_text(url: str, *, headers: Mapping[str, str] | None = None, timeout: float = 8.0) -> str:
    merged = {**DEFAULT_HTTP_HEADERS, **dict(headers or {})}
    request = urllib.request.Request(url, headers=merged, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 - URL is fixed by adapter
        return response.read().decode("gb18030", errors="replace")


def _provider_error(prefix: str, exc: BaseException) -> str:
    """Expose HTTP status/type only; never persist response headers."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"{prefix}_HTTP_ERROR:{exc.code}"
    return f"{prefix}_FETCH_ERROR:{type(exc).__name__}"


def _tencent_http_text(url: str) -> str:
    return _http_text(url, headers={"Referer": "https://gu.qq.com/"})


def _sina_http_text(url: str) -> str:
    return _http_text(url, headers={"Referer": "https://finance.sina.com.cn/"})


def _eastmoney_http_text(url: str) -> str:
    return _http_text(url, headers={"Referer": "https://quote.eastmoney.com/"})


class TencentAdapter:
    """Tencent quote endpoint used as the A-share backup source."""

    name = "tencent"

    def __init__(self, fetch_text: Callable[[str], str] = _tencent_http_text):
        self._fetch_text = fetch_text

    def fetch(self, symbol: str, *, observed_at: datetime) -> tuple[Observation, str]:
        code = symbol.lower().replace(".xshe", "").replace(".xshg", "")
        if not code.startswith(("sz", "sh")):
            code = ("sz" if code in {"000426", "000960"} else "sz") + code
        url = f"https://qt.gtimg.cn/q={code}"
        try:
            raw = self._fetch_text(url)
            quoted = raw.split('"', 2)
            fields = quoted[1].split("~") if len(quoted) > 1 else []
            # Tencent's stable quote layout: price/previous/open/volume and
            # high/low/date/time are fields 3/4/5/6/33/34/30/31.
            if len(fields) < 35:
                return missing_observation(symbol, self.name, observed_at, "TENCENT_SCHEMA_UNSUPPORTED", raw)
            exchange_time = _parse_provider_time(fields[30], fields[31] if len(fields) > 31 else None)
            values = [float(fields[index]) for index in (5, 33, 34, 3, 6)]
            observation = Observation(symbol, self.name, observed_at, exchange_time, *values, "READY", raw_ref=None)
            return observation, raw
        except (OSError, ValueError, IndexError, urllib.error.URLError) as exc:
            return missing_observation(symbol, self.name, observed_at, _provider_error("TENCENT", exc))


class SinaFuturesAdapter:
    """Sina domestic futures primary adapter (strict parser, no fallback guessing)."""

    name = "sina_futures"

    def __init__(self, fetch_text: Callable[[str], str] = _sina_http_text):
        self._fetch_text = fetch_text

    def fetch(self, symbol: str, *, observed_at: datetime) -> tuple[Observation, str]:
        contract = symbol.upper()
        url = f"https://hq.sinajs.cn/list=nf_{contract}"
        try:
            raw = self._fetch_text(url)
            fields = raw.split('"', 2)[1].split(",")
            # nf_ domestic layout (as returned by Sina): field 1 is HHMMSS,
            # field 17 is the YYYY-MM-DD exchange date. Unknown/short layouts
            # remain MISSING rather than guessing a date from observed_at.
            if len(fields) < 18:
                return missing_observation(symbol, self.name, observed_at, "SINA_SCHEMA_UNSUPPORTED", raw)
            exchange_time = _parse_provider_time(fields[17], fields[1])
            current, high, low, open_price, volume = (float(fields[i]) for i in (8, 3, 4, 2, 14))
            return Observation(symbol, self.name, observed_at, exchange_time, open_price, high, low, current, volume, "READY"), raw
        except (OSError, ValueError, IndexError, urllib.error.URLError) as exc:
            return missing_observation(symbol, self.name, observed_at, _provider_error("SINA", exc))


class EastmoneyFuturesAdapter(SinaFuturesAdapter):
    """Eastmoney futures backup adapter; strict schema and fail-closed errors."""

    name = "eastmoney_futures"

    def __init__(self, fetch_text: Callable[[str], str] = _eastmoney_http_text):
        super().__init__(fetch_text)

    def fetch(self, symbol: str, *, observed_at: datetime) -> tuple[Observation, str]:
        contract = symbol.upper()
        secid = EASTMONEY_FUTURES_SECIDS.get(contract)
        if secid is None:
            return missing_observation(symbol, self.name, observed_at, "EASTMONEY_FUTURES_SECID_UNVERIFIED")
        url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f43,f44,f45,f46,f47,f86,f57,f58"
        try:
            raw = self._fetch_text(url)
            payload = json.loads(raw)
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                return missing_observation(symbol, self.name, observed_at, "EASTMONEY_SCHEMA_UNSUPPORTED", raw)
            keys = {"f43": "close", "f44": "high", "f45": "low", "f46": "open", "f47": "volume"}
            if not all(key in data and data[key] is not None for key in keys):
                return missing_observation(symbol, self.name, observed_at, "EASTMONEY_FIELDS_MISSING", raw)
            exchange_raw = data.get("f86")
            if exchange_raw is None:
                return missing_observation(symbol, self.name, observed_at, "EASTMONEY_TIME_MISSING", raw)
            exchange_time = _parse_epoch_or_iso(exchange_raw)
            values = {name: float(data[key]) for key, name in keys.items()}
            return Observation(symbol, self.name, observed_at, exchange_time, values["open"], values["high"], values["low"], values["close"], values["volume"], "READY"), raw
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, IndexError, OverflowError, urllib.error.URLError) as exc:
            return missing_observation(symbol, self.name, observed_at, _provider_error("EASTMONEY", exc))


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    asset_class: str
    primary: SourceAdapter
    backup: SourceAdapter
    contract_semantics: str | None = None
    mapping_verified: bool = False
    mapping_version: str = DEFAULT_MAPPING_VERSION
    official_mapping_provider: object | None = None


def default_specs() -> tuple[InstrumentSpec, ...]:
    mapping_provider = TushareMappingProvider("data/forward/tushare_futures_mapping.jsonl")
    return (
        InstrumentSpec("000426.XSHE", "stock", MootdxAdapter(), TencentAdapter()),
        InstrumentSpec("000960.XSHE", "stock", MootdxAdapter(), TencentAdapter()),
        InstrumentSpec("AG0", "future_factor", SinaFuturesAdapter(), EastmoneyFuturesAdapter(), "SOURCE_CONTINUOUS_ALIAS_UNVERIFIED", official_mapping_provider=mapping_provider),
        InstrumentSpec("AU0", "future_factor", SinaFuturesAdapter(), EastmoneyFuturesAdapter(), "SOURCE_CONTINUOUS_ALIAS_UNVERIFIED", official_mapping_provider=mapping_provider),
        InstrumentSpec("SN0", "future_factor", SinaFuturesAdapter(), EastmoneyFuturesAdapter(), "SOURCE_CONTINUOUS_ALIAS_UNVERIFIED", official_mapping_provider=mapping_provider),
        InstrumentSpec("SC0", "future_factor", SinaFuturesAdapter(), EastmoneyFuturesAdapter(), "SOURCE_CONTINUOUS_ALIAS_UNVERIFIED", official_mapping_provider=mapping_provider),
    )


def _fresh(observation: Observation, observed_at: datetime, max_age: timedelta) -> bool:
    if observation.exchange_time is None:
        return False
    age = observed_at - observation.exchange_time
    return timedelta(0) <= age <= max_age


def reconcile(primary: Observation, backup: Observation, *, max_age: timedelta, max_deviation_bps: float) -> Observation:
    """Select a source only when primary/backup evidence is fresh and consistent."""
    at = primary.observed_at
    p_ready = primary.status == "READY" and _fresh(primary, at, max_age)
    b_ready = backup.status == "READY" and _fresh(backup, at, max_age)
    if p_ready and b_ready:
        p = primary.close or 0.0
        b = backup.close or 0.0
        deviation = abs(p - b) / max(abs(p), abs(b), 1e-12) * 10000
        if deviation > max_deviation_bps:
            return Observation(primary.symbol, "primary+backup", at, None, None, None, None, None, None, "CONFLICT", (f"PRICE_DEVIATION_BPS:{deviation:.3f}",), price_deviation_bps=deviation)
        return replace_observation(primary, price_deviation_bps=deviation)
    if p_ready:
        return primary
    if b_ready:
        return backup
    reasons = tuple(dict.fromkeys(primary.reason_codes + backup.reason_codes)) or ("NO_FRESH_SOURCE",)
    status = "STALE" if any(item.status == "STALE" or (item.status == "READY" and item.exchange_time is not None and not _fresh(item, at, max_age)) for item in (primary, backup)) else "MISSING"
    return Observation(primary.symbol, "primary+backup", at, None, None, None, None, None, None, status, reasons)


class ProbeRunner:
    def __init__(self, specs: Iterable[InstrumentSpec] | None = None, *, max_age_seconds: int = 900, max_deviation_bps: float = 50.0):
        self.specs = tuple(specs or default_specs())
        self.max_age = timedelta(seconds=max_age_seconds)
        self.max_deviation_bps = max_deviation_bps

    def run(self, *, observed_at: datetime | None = None) -> tuple[tuple[Observation, Observation, Observation, str, str], ...]:
        at = observed_at or now_cn()
        if at.tzinfo is None or at.utcoffset() != timedelta(hours=8):
            raise ValueError("observed_at must be timezone-aware Asia/Shanghai (+08:00)")
        results = []
        for spec in self.specs:
            started = time.perf_counter()
            primary, primary_raw = spec.primary.fetch(spec.symbol, observed_at=at)
            primary = replace_observation(primary, latency_ms=(time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            backup, backup_raw = spec.backup.fetch(spec.symbol, observed_at=at)
            backup = replace_observation(backup, latency_ms=(time.perf_counter() - started) * 1000)
            selected = reconcile(primary, backup, max_age=self.max_age, max_deviation_bps=self.max_deviation_bps)
            if spec.contract_semantics:
                mapping = None
                provider = spec.official_mapping_provider
                if provider is not None and hasattr(provider, "fetch"):
                    mapping = provider.fetch(spec.symbol, observed_at=at)
                mapping_is_verified = bool(
                    mapping is not None
                    and getattr(mapping, "verified", False)
                    and (spec.symbol, getattr(mapping, "mapping_version", DEFAULT_MAPPING_VERSION)) in VERIFIED_FUTURES_MAPPING_ALLOWLIST
                )
                if not mapping_is_verified:
                    marker = spec.contract_semantics or "SOURCE_CONTINUOUS_ALIAS_UNVERIFIED"
                    selected = replace_observation(
                        selected,
                        reason_codes=tuple(dict.fromkeys(selected.reason_codes + (marker, "OFFICIAL_CONTRACT_MAPPING_UNVERIFIED"))),
                        mapping_verified=False,
                        mapping_version=DEFAULT_MAPPING_VERSION,
                        contract_mapping=_mapping_payload(mapping),
                    )
                else:
                    selected = replace_observation(
                        selected,
                        mapping_verified=True,
                        mapping_version=getattr(mapping, "mapping_version", spec.mapping_version),
                        contract_mapping=_mapping_payload(mapping),
                    )
            results.append((primary, backup, selected, primary_raw, backup_raw))
        return tuple(results)


def replace_observation(observation: Observation, **changes: object) -> Observation:
    """Keep adapter results immutable while recording run-level measurements."""
    from dataclasses import replace

    return replace(observation, **changes)


def _mapping_payload(mapping: object | None) -> dict[str, object] | None:
    if mapping is None:
        return None
    contract = getattr(mapping, "as_contract_mapping", None)
    if callable(contract):
        return dict(contract())
    to_dict = getattr(mapping, "to_dict", None)
    return dict(to_dict()) if callable(to_dict) else None
