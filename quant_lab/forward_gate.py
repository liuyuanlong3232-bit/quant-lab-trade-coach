"""Strict five-trading-day Sprint 1A readiness gate, read-only over probe runs."""

from __future__ import annotations

import sqlite3
import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Mapping

from .forward_probe import CN_TZ, DEFAULT_MAPPING_VERSION, VERIFIED_FUTURES_MAPPING_ALLOWLIST
from .forward_store import _local_path


REQUIRED_SYMBOLS = ("000426.XSHE", "000960.XSHE", "AG0", "AU0", "SN0", "SC0")
CHECKPOINTS = ("09:31", "10:00", "13:30", "14:50", "15:05")


@dataclass(frozen=True)
class GateConfig:
    checkpoints: tuple[str, ...] = CHECKPOINTS
    tolerance_minutes: int = 5
    required_symbols: tuple[str, ...] = REQUIRED_SYMBOLS
    target_days: int = 5
    # Empty by default: no continuous futures alias is considered verified in
    # Sprint 1A until an independently reviewed, versioned mapping is added.
    verified_mapping_allowlist: frozenset[tuple[str, str]] = VERIFIED_FUTURES_MAPPING_ALLOWLIST


@dataclass(frozen=True)
class DayGate:
    day: str
    check_count: int
    passed: bool
    checkpoint_results: Mapping[str, bool]
    reasons: tuple[str, ...]


def _checkpoint_time(label: str) -> time:
    hour, minute = (int(part) for part in label.split(":", 1))
    return time(hour, minute)


def _near_checkpoint(stamp: datetime, label: str, tolerance: int) -> bool:
    target = datetime.combine(stamp.date(), _checkpoint_time(label), tzinfo=CN_TZ)
    return abs((stamp - target).total_seconds()) <= tolerance * 60


def _reason_codes(evidence: Mapping[str, object]) -> tuple[str, ...]:
    raw = evidence.get("reason_codes", ())
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return (raw,)
        return tuple(str(item) for item in decoded) if isinstance(decoded, list) else (raw,)
    return tuple(str(item) for item in raw) if isinstance(raw, (tuple, list)) else ()


def _futures_mapping_verified(evidence: Mapping[str, object], symbol: str, allowlist: frozenset[tuple[str, str]]) -> bool:
    reasons = _reason_codes(evidence)
    if "SOURCE_CONTINUOUS_ALIAS_UNVERIFIED" in reasons:
        return False
    if not bool(evidence.get("mapping_verified")):
        return False
    version = str(evidence.get("mapping_version") or DEFAULT_MAPPING_VERSION)
    return (symbol, version) in allowlist


def evaluate_day(snapshots: Iterable[Mapping[str, object]], day: str, *, config: GateConfig | None = None) -> DayGate:
    cfg = config or GateConfig()
    same_day = [snapshot for snapshot in snapshots if str(snapshot.get("observed_at", "")).startswith(day)]
    results: dict[str, bool] = {}
    reasons: list[str] = []
    for checkpoint in cfg.checkpoints:
        candidates = []
        for snapshot in same_day:
            observed = datetime.fromisoformat(str(snapshot["observed_at"]))
            label = snapshot.get("check_point")
            if label not in (None, "") and str(label) != checkpoint:
                continue
            if _near_checkpoint(observed, checkpoint, cfg.tolerance_minutes):
                candidates.append(snapshot)
        passed = False
        for snapshot in candidates:
            evidence = {str(item["symbol"]): item for item in snapshot.get("evidence", [])}  # type: ignore[union-attr]
            if set(evidence) != set(cfg.required_symbols):
                continue
            if any(str(evidence[symbol].get("selected_status")) != "READY" for symbol in cfg.required_symbols):
                continue
            if any(str(evidence[symbol].get("primary_status")) != "READY" or str(evidence[symbol].get("backup_status")) != "READY" for symbol in ("000426.XSHE", "000960.XSHE")):
                continue
            if any(str(evidence[symbol].get("primary_status")) != "READY" for symbol in ("AG0", "AU0", "SN0", "SC0")):
                if "FUTURES_PRICE_NOT_READY" not in reasons:
                    reasons.append("FUTURES_PRICE_NOT_READY")
                continue
            if any(not _futures_mapping_verified(evidence[symbol], symbol, cfg.verified_mapping_allowlist) for symbol in ("AG0", "AU0", "SN0", "SC0")):
                if "FUTURES_MAPPING_UNVERIFIED" not in reasons:
                    reasons.append("FUTURES_MAPPING_UNVERIFIED")
                continue
            passed = True
            break
        results[checkpoint] = passed
        if not passed:
            reasons.append(f"CHECKPOINT_{checkpoint}_NOT_QUALIFIED")
    if not same_day:
        reasons.append("NO_RUNS_FOR_DAY")
    return DayGate(day, len(same_day), all(results.values()) and bool(same_day), results, tuple(reasons))


def load_snapshots(db_path: str | Path) -> tuple[dict[str, object], ...]:
    path = _local_path(db_path)
    if not path.is_file():
        return ()
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        runs = connection.execute("SELECT run_id,observed_at,check_point,overall_status FROM probe_runs ORDER BY observed_at").fetchall()
    except sqlite3.OperationalError:
        connection.close()
        return ()
    output = []
    for run in runs:
        evidence = [dict(row) for row in connection.execute("SELECT * FROM probe_evidence WHERE run_id=? ORDER BY symbol", (run["run_id"],)).fetchall()]
        output.append({"run_id": run["run_id"], "observed_at": run["observed_at"], "check_point": run["check_point"], "overall_status": run["overall_status"], "evidence": evidence})
    connection.close()
    return tuple(output)


def evaluate_all_days(db_path: str | Path, *, config: GateConfig | None = None) -> tuple[DayGate, ...]:
    snapshots = load_snapshots(db_path)
    days = sorted({str(snapshot["observed_at"])[:10] for snapshot in snapshots})
    cfg = config or GateConfig()
    return tuple(evaluate_day(snapshots, day, config=cfg) for day in days)


def gate_summary(db_path: str | Path, *, config: GateConfig | None = None) -> dict[str, object]:
    cfg = config or GateConfig()
    days = evaluate_all_days(db_path, config=cfg)
    qualified = sum(day.passed for day in days)
    return {"qualified_days": qualified, "target_days": cfg.target_days, "progress": f"{min(qualified, cfg.target_days)}/{cfg.target_days}", "status": "PASS" if qualified >= cfg.target_days else "PENDING", "days": days}
