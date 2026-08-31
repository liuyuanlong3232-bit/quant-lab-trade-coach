"""Evidence-gated, audit-only scheduler for public market-data refreshes.

This module schedules data collection only.  It has no broker, order, signal or
execution capability, and never treats a weekday as proof of an A-share session.
"""
from __future__ import annotations

import csv
import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence


CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
DEFAULT_REFRESH_POINTS = ("09:35", "11:25", "13:35", "14:50", "15:10")


class SchedulerStore(Protocol):
    def claim_auto_refresh_slot(self, *, schedule_key: str, scheduled_for: datetime, claimed_at: datetime) -> bool: ...
    def append_auto_refresh_audit(self, *, schedule_key: str, status: str, reason_codes: Sequence[str], payload: Mapping[str, Any], event_at: datetime) -> int: ...
    def auto_refresh_audit(self, limit: int = 20) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class TradingDayEvidence:
    status: str
    reason_codes: tuple[str, ...]
    source_ref: str | None = None


class AShareTradingCalendar:
    """Read a hash-manifested Tushare trade calendar; otherwise fail closed."""

    def __init__(self, project_root: str | Path):
        root = Path(project_root).resolve() / "data" / "trade_coach" / "source_cache"
        self.root = root
        self.csv_path = root / "tushare_trade_calendar.csv"
        self.manifest_path = root / "tushare_trade_calendar_manifest.json"

    def _rows(self) -> tuple[dict[str, bool], str] | None:
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if manifest.get("source") != "TUSHARE" or manifest.get("calendar") not in {"SSE", "A_SHARE"}:
                return None
            retrieved = datetime.fromisoformat(str(manifest.get("retrieved_at") or "").replace("Z", "+00:00"))
            if retrieved.tzinfo is None or retrieved.utcoffset() is None:
                return None
            relative = str(manifest.get("path") or self.csv_path.name)
            csv_path = (self.root / relative).resolve()
            if csv_path.parent != self.root.resolve() or not csv_path.name.startswith("tushare_trade_calendar"):
                return None
            content = csv_path.read_bytes()
            if hashlib.sha256(content).hexdigest() != str(manifest.get("sha256") or ""):
                return None
            rows: dict[str, bool] = {}
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    raw_day = str(row.get("cal_date") or row.get("trade_date") or row.get("date") or "").strip()
                    if len(raw_day) == 8 and raw_day.isdigit():
                        raw_day = f"{raw_day[:4]}-{raw_day[4:6]}-{raw_day[6:]}"
                    date.fromisoformat(raw_day)
                    raw_open = str(row.get("is_open") or "").strip()
                    if raw_open not in {"0", "1"}:
                        return None
                    rows[raw_day] = raw_open == "1"
            return rows, f"{csv_path}#sha256={manifest['sha256']}"
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None

    def status(self, day: date) -> TradingDayEvidence:
        if day.weekday() >= 5:
            return TradingDayEvidence("CLOSED", ("A_SHARE_WEEKEND",))
        loaded = self._rows()
        if loaded is None:
            return TradingDayEvidence("UNKNOWN", ("A_SHARE_TRADING_CALENDAR_UNAVAILABLE",))
        rows, source_ref = loaded
        if day.isoformat() not in rows:
            return TradingDayEvidence("UNKNOWN", ("A_SHARE_TRADING_DAY_NOT_COVERED",), source_ref)
        return TradingDayEvidence("OPEN" if rows[day.isoformat()] else "CLOSED", ("TUSHARE_TRADE_CALENDAR",), source_ref)


class AutoRefreshScheduler:
    """Run each evidenced schedule slot at most once and never catch it up."""

    def __init__(self, store: SchedulerStore, project_root: str | Path, refresh: Callable[[], Mapping[str, Any]], *, calendar_update: Callable[[], Mapping[str, Any]] | None = None, points: Sequence[str] = DEFAULT_REFRESH_POINTS, grace_minutes: int = 4, poll_seconds: float = 20.0, now: Callable[[], datetime] | None = None):
        self.store = store
        self.calendar = AShareTradingCalendar(project_root)
        self.refresh = refresh
        self.calendar_update = calendar_update
        self.points = tuple(points)
        self.grace = timedelta(minutes=max(1, int(grace_minutes)))
        self.poll_seconds = max(1.0, float(poll_seconds))
        self.now = now or (lambda: datetime.now(CN_TZ))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick_lock = threading.Lock()

    def _calendar_maintenance(self, current: datetime) -> list[str]:
        if self.calendar_update is None:
            return []
        due: list[tuple[str, datetime]] = []
        if self.calendar.status(current.date()).status == "UNKNOWN":
            due.append((f"calendar-init:{current.date().isoformat()}", current))
        daily = self._scheduled(current.date(), "15:20")
        if daily <= current <= daily + self.grace:
            due.append((f"calendar-update:{current.date().isoformat()}@15:20", daily))
        completed = []
        for key, scheduled in due:
            if not self.store.claim_auto_refresh_slot(schedule_key=key, scheduled_for=scheduled, claimed_at=current):
                continue
            try:
                result = dict(self.calendar_update())
                result.pop("token", None)
                updater_status = str(result.get("status") or "FAILED")
                reasons = tuple(str(item) for item in result.get("reason_codes", []))
                audit_status = "COMPLETE" if updater_status in {"UPDATED", "UNCHANGED"} else ("SKIPPED" if updater_status == "SKIPPED" else "FAILED")
                self.store.append_auto_refresh_audit(schedule_key=key, status=audit_status, reason_codes=reasons, payload={"calendar_update": result, "automatic_trading": False}, event_at=self.now())
            except Exception as exc:
                self.store.append_auto_refresh_audit(schedule_key=key, status="FAILED", reason_codes=(f"TUSHARE_CALENDAR_UPDATE_ERROR:{type(exc).__name__}",), payload={"automatic_trading": False}, event_at=self.now())
            completed.append(key)
        return completed

    @staticmethod
    def _scheduled(day: date, point: str) -> datetime:
        hour, minute = (int(part) for part in point.split(":", 1))
        return datetime.combine(day, time(hour, minute), tzinfo=CN_TZ)

    def tick(self, at: datetime | None = None) -> list[str]:
        current = (at or self.now()).astimezone(CN_TZ)
        completed: list[str] = []
        if not self._tick_lock.acquire(blocking=False):
            return completed
        try:
            completed.extend(self._calendar_maintenance(current))
            for point in self.points:
                scheduled = self._scheduled(current.date(), point)
                # A missed slot is never replayed, including after a restart.
                if current < scheduled or current > scheduled + self.grace:
                    continue
                key = f"{current.date().isoformat()}@{point}"
                if not self.store.claim_auto_refresh_slot(schedule_key=key, scheduled_for=scheduled, claimed_at=current):
                    continue
                evidence = self.calendar.status(current.date())
                base = {"scheduled_for": scheduled.isoformat(), "calendar_status": evidence.status, "calendar_source_ref": evidence.source_ref, "automatic_trading": False, "network_backfill": False}
                if evidence.status != "OPEN":
                    reason = evidence.reason_codes or ("A_SHARE_TRADING_DAY_UNKNOWN",)
                    self.store.append_auto_refresh_audit(schedule_key=key, status="SKIPPED", reason_codes=reason, payload=base, event_at=current)
                    completed.append(key)
                    continue
                try:
                    result = dict(self.refresh())
                    scheduler_status = str(result.pop("scheduler_status", "COMPLETE"))
                    if scheduler_status == "SKIPPED_BUSY":
                        self.store.append_auto_refresh_audit(schedule_key=key, status="SKIPPED", reason_codes=("AUTO_REFRESH_CONCURRENT_RUN",), payload={**base, **result}, event_at=self.now())
                    else:
                        self.store.append_auto_refresh_audit(schedule_key=key, status="COMPLETE", reason_codes=(), payload={**base, "refresh": result}, event_at=self.now())
                except Exception as exc:  # scheduler must survive provider/runtime failure
                    self.store.append_auto_refresh_audit(schedule_key=key, status="FAILED", reason_codes=(f"AUTO_REFRESH_ERROR:{type(exc).__name__}",), payload=base, event_at=self.now())
                completed.append(key)
        finally:
            self._tick_lock.release()
        return completed

    def status(self, at: datetime | None = None) -> dict[str, Any]:
        current = (at or self.now()).astimezone(CN_TZ)
        audit = self.store.auto_refresh_audit(100)
        claimed = {str(item.get("schedule_key")) for item in audit}
        next_plan = None
        next_evidence: TradingDayEvidence | None = None
        for offset in range(0, 32):
            day = current.date() + timedelta(days=offset)
            evidence = self.calendar.status(day)
            if evidence.status != "OPEN":
                continue
            for point in self.points:
                scheduled = self._scheduled(day, point)
                key = f"{day.isoformat()}@{point}"
                if scheduled + self.grace >= current and key not in claimed:
                    next_plan, next_evidence = scheduled, evidence
                    break
            if next_plan is not None:
                break
        latest = next((item for item in audit if item.get("status") != "CLAIMED" and not str(item.get("schedule_key", "")).startswith("calendar-")), None)
        latest_calendar = next((item for item in audit if item.get("status") != "CLAIMED" and str(item.get("schedule_key", "")).startswith("calendar-")), None)
        today = self.calendar.status(current.date())
        return {
            "enabled": True,
            "running": bool(self._thread and self._thread.is_alive()),
            "timezone": "Asia/Shanghai",
            "points": list(self.points),
            "status": "READY" if today.status == "OPEN" else today.status,
            "reason_codes": list(today.reason_codes),
            "calendar_source_ref": today.source_ref,
            "last_auto_refresh": latest,
            "last_calendar_update": latest_calendar,
            "next_planned_at": next_plan.isoformat() if next_plan else None,
            "next_plan_calendar_source_ref": next_evidence.source_ref if next_evidence else None,
            "missed_slots_are_backfilled": False,
            "automatic_trading": False,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.poll_seconds)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="quant-lab-auto-refresh", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=min(5.0, self.poll_seconds + 1.0))
