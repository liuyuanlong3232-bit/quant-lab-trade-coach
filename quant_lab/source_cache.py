"""Validated, append-only local source cache writers.

These helpers deliberately separate fetching from persistence: callers may fetch
through a read-only provider (including SSH) and only validated rows reach disk.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass


def _day(value: Any) -> date:
    text = str(value or "")
    if len(text) == 8 and text.isdigit(): text = f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return date.fromisoformat(text)


def merge_sector_801050(csv_path: Path, manifest_path: Path, rows: Iterable[Mapping[str, Any]], *, fetched_at: datetime) -> dict[str, Any]:
    """Append new 801050 rows, rejecting rewrites, duplicates and future dates."""
    existing: list[dict[str, str]] = []
    if csv_path.is_file():
        with csv_path.open(encoding="utf-8-sig", newline="") as handle: existing = list(csv.DictReader(handle))
    by_day = {str(row.get("date")): row for row in existing}
    incoming = []
    for raw in rows:
        row = {str(k): "" if v is None else str(v) for k, v in raw.items()}
        day = _day(row.get("date") or row.get("trade_date"))
        if day > fetched_at.date(): raise ValueError("SECTOR:FUTURE_TRADE_DATE")
        close = float(row.get("close", "nan"))
        if not math.isfinite(close) or close <= 0: raise ValueError("SECTOR:INVALID_CLOSE")
        row["date"] = day.isoformat()
        prior = by_day.get(row["date"])
        if prior is not None:
            if prior.get("close") != row.get("close"): raise ValueError(f"SECTOR:REMOTE_DIVERGED:{row['date']}")
            continue
        incoming.append(row); by_day[row["date"]] = row
    incoming.sort(key=lambda item: item["date"])
    combined = sorted(existing + incoming, key=lambda item: item.get("date", ""))
    fields = list(dict.fromkeys(["date", "open", "close", "high", "low", "volume", "pre_close", "change", "pct_chg", "amount"] + [k for r in combined for k in r]))
    import io
    stream = io.StringIO(); writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore"); writer.writeheader(); writer.writerows(combined)
    csv_bytes = stream.getvalue().encode("utf-8")
    if incoming: _atomic(csv_path, csv_bytes)
    manifest = {}
    if manifest_path.is_file():
        try: manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError): manifest = {}
    manifest.update({"schema_version": "quant_lab_sector_snapshot_v1", "source": "TUSHARE_INDEX_DAILY", "requested_symbol": "801050.SI", "retrieved_at": fetched_at.isoformat(), "row_count": len(combined), "last_trade_date": combined[-1]["date"].replace("-", "") if combined else None, "normalized_sha256": hashlib.sha256(csv_bytes).hexdigest(), "retrieval_boundary": "read-only SSH; Tushare token remained on VPS", "adjustment": "none; index point series"})
    if incoming: _atomic(manifest_path, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return {"status": "UPDATED" if incoming else "UNCHANGED", "appended": len(incoming), "rows": len(combined), "sha256": hashlib.sha256(csv_bytes).hexdigest()}


def validate_fred_rows(rows: Iterable[Mapping[str, Any]], *, today: date) -> list[dict[str, Any]]:
    result = []
    seen: set[str] = set()
    for row in rows:
        day = _day(row.get("observation_date") or row.get("date"))
        if day > today: raise ValueError("FRED:FUTURE_OBSERVATION_DATE")
        if day.isoformat() in seen: raise ValueError("FRED:DUPLICATE_DATE")
        value = float(row.get("value", row.get("close", "nan")))
        if not math.isfinite(value): continue
        seen.add(day.isoformat())
        result.append({"observation_date": day.isoformat(), "value": value})
    if not result: raise ValueError("FRED:NO_VALID_OBSERVATIONS")
    return result


def update_fred_cache(root: Path, symbol: str, rows: Iterable[Mapping[str, Any]], *, fetched_at: datetime, source_ref: str) -> dict[str, Any]:
    """Atomically publish one validated FRED CSV and its manifest entry."""
    valid = validate_fred_rows(rows, today=fetched_at.date())
    import io
    stream = io.StringIO(); writer = csv.DictWriter(stream, fieldnames=["observation_date", symbol]); writer.writeheader()
    for row in valid: writer.writerow({"observation_date": row["observation_date"], symbol: row["value"]})
    content = stream.getvalue().encode("utf-8"); path = root / f"fred_{symbol}.csv"; _atomic(path, content)
    manifest_path = root / "manifest.json"; manifest = {"schema_version": "quant_lab_source_cache_v1", "files": {}}
    if manifest_path.is_file():
        try: manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError): pass
    manifest.setdefault("files", {})[symbol] = {"path": path.name, "source": "FRED", "source_ref": source_ref, "sha256": hashlib.sha256(content).hexdigest()}
    manifest["schema_version"] = "quant_lab_source_cache_v1"; manifest["fetched_at"] = fetched_at.isoformat()
    _atomic(manifest_path, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return {"path": str(path), "rows": len(valid), "sha256": hashlib.sha256(content).hexdigest()}
