"""Fail-closed, append-only synchronization for Personal Trade Coach facts.

Only two reviewed remote facts are consumed: the published VPS macro-risk
JSONL and Tushare's SHFE tin mapping/daily response executed read-only on the
VPS.  A failed or suspicious fetch never replaces the last known local file.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
RISK_SCHEMA = "vps_macro_risk_point_v1"
TIN_SCHEMA = "tushare_tin_main_history_v1"
RISK_LEVELS = frozenset({"GREEN", "ORANGE", "RED"})


class SyncRejected(ValueError):
    """Remote material failed its point-in-time or append-only contract."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _aware(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise SyncRejected(f"{field}:INVALID_TIMESTAMP") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SyncRejected(f"{field}:TIMEZONE_REQUIRED")
    return parsed


def _jsonl(data: bytes, *, label: str) -> list[dict[str, Any]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SyncRejected(f"{label}:INVALID_UTF8") from exc
    rows: list[dict[str, Any]] = []
    for number, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SyncRejected(f"{label}:TRUNCATED_OR_INVALID_JSON:{number}") from exc
        if not isinstance(row, dict):
            raise SyncRejected(f"{label}:NON_OBJECT:{number}")
        rows.append(row)
    if not rows:
        raise SyncRejected(f"{label}:EMPTY")
    return rows


def validate_risk_rows(data: bytes, *, fetched_at: datetime) -> list[dict[str, Any]]:
    rows = _jsonl(data, label="RISK")
    maximum = fetched_at.astimezone(timezone.utc) + timedelta(minutes=5)
    previous: datetime | None = None
    for row in rows:
        required = {
            "schema_version", "as_of_trade_date", "generated_at", "valid_until",
            "source", "model_version", "risk_level", "prediction_gate_status",
            "macro_event_gate", "reason_codes", "usd_score", "rate_score",
            "liquidity_score", "demand_score",
        }
        if not required.issubset(row):
            raise SyncRejected("RISK:REQUIRED_FIELDS_MISSING")
        if row["schema_version"] != RISK_SCHEMA or row["source"] != "HERMES" or row["model_version"] != "vps_macro_risk_v1":
            raise SyncRejected("RISK:CONTRACT_CONSTANT_INVALID")
        if row["risk_level"] not in RISK_LEVELS:
            raise SyncRejected("RISK:INVALID_LEVEL")
        generated = _aware(row["generated_at"], "generated_at").astimezone(timezone.utc)
        valid_until = _aware(row["valid_until"], "valid_until").astimezone(timezone.utc)
        if generated > maximum:
            raise SyncRejected("RISK:FUTURE_GENERATED_AT")
        if valid_until < generated:
            raise SyncRejected("RISK:VALID_UNTIL_BEFORE_GENERATED")
        try:
            as_of = datetime.strptime(str(row["as_of_trade_date"]), "%Y-%m-%d").date()
        except ValueError as exc:
            raise SyncRejected("RISK:INVALID_TRADE_DATE") from exc
        if as_of > generated.astimezone(CN_TZ).date():
            raise SyncRejected("RISK:FUTURE_TRADE_DATE")
        if previous is not None and generated < previous:
            raise SyncRejected("RISK:GENERATED_AT_NOT_APPEND_ORDERED")
        previous = generated
        for field in ("usd_score", "rate_score", "liquidity_score", "demand_score"):
            try:
                value = float(row[field])
            except (TypeError, ValueError) as exc:
                raise SyncRejected(f"RISK:{field.upper()}_INVALID") from exc
            if not -1.0 <= value <= 1.0:
                raise SyncRejected(f"RISK:{field.upper()}_OUT_OF_RANGE")
    return rows


def validate_tin_rows(data: bytes, *, fetched_at: datetime) -> list[dict[str, Any]]:
    rows = _jsonl(data, label="TIN")
    maximum = fetched_at.astimezone(timezone.utc) + timedelta(minutes=5)
    dates: set[str] = set()
    prior_date = ""
    for row in rows:
        if row.get("schema_version") != TIN_SCHEMA or row.get("source") != "TUSHARE" or row.get("product") != "SN.SHF":
            raise SyncRejected("TIN:CONTRACT_CONSTANT_INVALID")
        trade_date = str(row.get("trade_date") or "")
        try:
            parsed_date = datetime.strptime(trade_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise SyncRejected("TIN:INVALID_TRADE_DATE") from exc
        available = _aware(row.get("available_at"), "available_at")
        if available.astimezone(timezone.utc) > maximum:
            raise SyncRejected("TIN:FUTURE_AVAILABLE_AT")
        if parsed_date > available.astimezone(CN_TZ).date():
            raise SyncRejected("TIN:FUTURE_TRADE_DATE")
        if trade_date in dates or (prior_date and trade_date < prior_date):
            raise SyncRejected("TIN:DUPLICATE_OR_UNORDERED_TRADE_DATE")
        dates.add(trade_date)
        prior_date = trade_date
        contract = str(row.get("mapping_ts_code") or "")
        if not (contract.startswith("SN") and contract.endswith(".SHF") and len(contract) == 10):
            raise SyncRejected("TIN:INVALID_CONCRETE_CONTRACT")
        if row.get("series_semantics") != "SHFE_TIN_DAILY_MAIN_CONTRACT_UNADJUSTED":
            raise SyncRejected("TIN:INVALID_SERIES_SEMANTICS")
        for field in ("open", "high", "low", "close", "settle"):
            try:
                if float(row[field]) <= 0:
                    raise ValueError
            except (KeyError, TypeError, ValueError) as exc:
                raise SyncRejected(f"TIN:{field.upper()}_INVALID") from exc
    return rows


def canonical(row: Mapping[str, Any]) -> str:
    return json.dumps(dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _atomic_replace(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def merge_append_only(path: Path, remote_rows: Iterable[Mapping[str, Any]], *, schema: str) -> dict[str, Any]:
    old_bytes = path.read_bytes() if path.is_file() else b""
    local_rows = _jsonl(old_bytes, label="LOCAL") if old_bytes.strip() else []
    remote = [dict(row) for row in remote_rows]
    local_canonical = [canonical(row) for row in local_rows]
    remote_canonical = [canonical(row) for row in remote]
    if len(remote) < len(local_rows) or remote_canonical[:len(local_rows)] != local_canonical:
        raise SyncRejected(f"{schema}:REMOTE_TRUNCATED_OR_DIVERGED")
    pending = remote[len(local_rows):]
    if pending:
        normalized = ("\n".join(remote_canonical) + "\n").encode("utf-8")
        _atomic_replace(path, normalized)
    new_bytes = path.read_bytes() if path.is_file() else old_bytes
    return {
        "status": "UPDATED" if pending else "UNCHANGED",
        "appended": len(pending),
        "rows": len(remote),
        "before_sha256": sha256_bytes(old_bytes),
        "after_sha256": sha256_bytes(new_bytes),
    }


def merge_tin_history(path: Path, remote_rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Merge a rolling remote Tushare window into the permanent local PIT log."""
    old_bytes = path.read_bytes() if path.is_file() else b""
    local_rows = _jsonl(old_bytes, label="LOCAL_TIN") if old_bytes.strip() else []
    remote = [dict(row) for row in remote_rows]
    local_by_date = {str(row.get("trade_date")): row for row in local_rows}
    stable = ("mapping_ts_code", "open", "high", "low", "close", "settle", "volume", "open_interest")
    pending: list[dict[str, Any]] = []
    latest = max(local_by_date, default="")
    for row in remote:
        day = str(row.get("trade_date"))
        old = local_by_date.get(day)
        if old is not None:
            if any(old.get(field) != row.get(field) for field in stable):
                raise SyncRejected(f"TIN:REMOTE_DIVERGED:{day}")
            continue
        if latest and day <= latest:
            raise SyncRejected(f"TIN:HISTORICAL_GAP_OR_LATE_REWRITE:{day}")
        pending.append(row)
    if pending:
        combined = [*local_rows, *pending]
        normalized = ("\n".join(canonical(row) for row in combined) + "\n").encode("utf-8")
        _atomic_replace(path, normalized)
    new_bytes = path.read_bytes() if path.is_file() else old_bytes
    return {
        "status": "UPDATED" if pending else "UNCHANGED",
        "appended": len(pending),
        "rows": len(local_rows) + len(pending),
        "before_sha256": sha256_bytes(old_bytes),
        "after_sha256": sha256_bytes(new_bytes),
    }


def append_audit(path: Path, row: Mapping[str, Any]) -> None:
    before = path.read_bytes() if path.is_file() else b""
    line = (canonical(row) + "\n").encode("utf-8")
    _atomic_replace(path, before + line)
