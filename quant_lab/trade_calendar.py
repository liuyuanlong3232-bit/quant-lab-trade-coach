"""Safe Tushare SSE trading-calendar updater.

Credentials are input-only.  Results, exceptions, manifests and audits never
contain the token or a raw provider response.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
TUSHARE_API = "https://api.tushare.pro"


def _protected_file(path_text: str) -> str | None:
    try:
        path = Path(path_text)
        if not path.is_file() or path.stat().st_size > 4096:
            return None
        if os.name != "nt" and path.stat().st_mode & 0o022:
            return None
        return path.read_text(encoding="utf-8").strip() or None
    except (OSError, UnicodeError):
        return None


def read_tushare_token() -> tuple[str | None, str]:
    file_name = os.environ.get("TUSHARE_TOKEN_FILE", "").strip()
    if file_name:
        token = _protected_file(file_name)
        return (token, "PROTECTED_FILE") if token else (None, "TUSHARE_TOKEN_FILE_MISSING_OR_INSECURE")
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    return (token, "ENVIRONMENT") if token else (None, "TUSHARE_TOKEN_NOT_CONFIGURED")


def _atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


class TushareTradeCalendarUpdater:
    def __init__(self, project_root: str | Path, *, timeout: float = 15.0, http: Callable[[bytes, float], bytes] | None = None, now: Callable[[], datetime] | None = None):
        self.root = Path(project_root).resolve() / "data" / "trade_coach" / "source_cache"
        self.timeout = max(1.0, float(timeout))
        self.http = http or self._http
        self.now = now or (lambda: datetime.now(CN_TZ))
        self._lock = threading.Lock()

    @staticmethod
    def _http(body: bytes, timeout: float) -> bytes:
        request = urllib.request.Request(TUSHARE_API, data=body, headers={"Content-Type": "application/json", "User-Agent": "Quant-Lab-Trade-Calendar/1"}, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 fixed HTTPS provider
            return response.read(2 * 1024 * 1024)

    @staticmethod
    def _validated_rows(payload: Mapping[str, Any], *, start: date, end: date) -> list[dict[str, str]]:
        if payload.get("code") != 0:
            raise ValueError("TUSHARE_PROVIDER_ERROR")
        data = payload.get("data")
        if not isinstance(data, Mapping) or not isinstance(data.get("fields"), list) or not isinstance(data.get("items"), list):
            raise ValueError("TUSHARE_CALENDAR_SCHEMA_INVALID")
        fields = [str(item) for item in data["fields"]]
        if "cal_date" not in fields or "is_open" not in fields:
            raise ValueError("TUSHARE_CALENDAR_FIELDS_MISSING")
        rows: dict[str, dict[str, str]] = {}
        for values in data["items"]:
            if not isinstance(values, list) or len(values) != len(fields):
                raise ValueError("TUSHARE_CALENDAR_ROW_INVALID")
            item = dict(zip(fields, values))
            raw_day = str(item.get("cal_date") or "")
            day = datetime.strptime(raw_day, "%Y%m%d").date()
            is_open = str(item.get("is_open"))
            if is_open not in {"0", "1"} or day < start or day > end or raw_day in rows:
                raise ValueError("TUSHARE_CALENDAR_VALUE_INVALID")
            rows[raw_day] = {"cal_date": raw_day, "is_open": is_open, "pretrade_date": str(item.get("pretrade_date") or "")}
        expected = {(start + timedelta(days=offset)).strftime("%Y%m%d") for offset in range((end - start).days + 1)}
        if set(rows) != expected:
            raise ValueError("TUSHARE_CALENDAR_COVERAGE_INCOMPLETE")
        return [rows[key] for key in sorted(rows)]

    def update(self) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return {"status": "SKIPPED", "reason_codes": ["TUSHARE_CALENDAR_UPDATE_BUSY"], "credential_source": None}
        try:
            token, credential_source = read_tushare_token()
            if not token:
                return {"status": "SKIPPED", "reason_codes": [credential_source], "credential_source": None}
            retrieved = self.now().astimezone(CN_TZ)
            start = retrieved.date() - timedelta(days=31)
            end = retrieved.date() + timedelta(days=370)
            request_payload = {"api_name": "trade_cal", "token": token, "params": {"exchange": "SSE", "start_date": start.strftime("%Y%m%d"), "end_date": end.strftime("%Y%m%d")}, "fields": "cal_date,is_open,pretrade_date"}
            try:
                body = json.dumps(request_payload, separators=(",", ":")).encode("utf-8")
                response = json.loads(self.http(body, self.timeout).decode("utf-8"))
                if not isinstance(response, Mapping):
                    raise ValueError("TUSHARE_CALENDAR_RESPONSE_INVALID")
                rows = self._validated_rows(response, start=start, end=end)
                stream = io.StringIO(newline="")
                writer = csv.DictWriter(stream, fieldnames=["cal_date", "is_open", "pretrade_date"], lineterminator="\n")
                writer.writeheader(); writer.writerows(rows)
                content = stream.getvalue().encode("utf-8")
                digest = hashlib.sha256(content).hexdigest()
                versioned_name = f"tushare_trade_calendar.{digest[:16]}.csv"
                versioned_path = self.root / versioned_name
                manifest = {"schema_version": "quant_lab_tushare_trade_calendar_v1", "source": "TUSHARE", "calendar": "SSE", "path": versioned_name, "retrieved_at": retrieved.isoformat(), "coverage_start": start.isoformat(), "coverage_end": end.isoformat(), "row_count": len(rows), "sha256": digest}
                # The immutable CSV is published first.  The small manifest is
                # the atomic pointer; a failed update leaves its old target valid.
                if not versioned_path.exists():
                    _atomic(versioned_path, content)
                _atomic(self.root / "tushare_trade_calendar_manifest.json", (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
                return {"status": "UPDATED", "reason_codes": [], "credential_source": credential_source, "retrieved_at": retrieved.isoformat(), "coverage_start": start.isoformat(), "coverage_end": end.isoformat(), "row_count": len(rows), "sha256": digest, "source": "TUSHARE", "calendar": "SSE"}
            except Exception as exc:  # return only the class; never provider text or request/token
                return {"status": "FAILED", "reason_codes": [f"TUSHARE_CALENDAR_UPDATE_ERROR:{type(exc).__name__}"], "credential_source": credential_source}
        finally:
            self._lock.release()
