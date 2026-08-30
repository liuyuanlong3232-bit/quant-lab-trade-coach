"""Day-0 local audit records for the two JoinQuant paper strategies.

This module records what a local operator expected to see and what was
actually observed in a JoinQuant simulation log.  It does not call JoinQuant,
read a broker, place an order, or infer fills.  A record is append-only JSONL
and a PASS requires explicit evidence and an exact expected/observed match.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from .audit import AuditLog


SHANGHAI_TZ = timezone(timedelta(hours=8))
BASELINE_STRATEGY_ID = "joinquant_20d_paper"
HERMES_STRATEGY_ID = "joinquant_hermes_20d_paper"
STRATEGY_IDS = (BASELINE_STRATEGY_ID, HERMES_STRATEGY_ID)
RISK_STATUSES = frozenset({"GREEN", "ORANGE", "RED", "MISSING", "STALE", "INVALID"})
NON_GREEN_RISK = frozenset({"ORANGE", "RED", "MISSING", "STALE", "INVALID"})


def _aware(value: datetime | str) -> datetime:
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        value = datetime.fromisoformat(text)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("checked_at must be an ISO-8601 timestamp with timezone")
    return value


def _day(value: date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value)
    if not isinstance(value, date):
        raise ValueError("decision_for must be YYYY-MM-DD")
    return value


def _status(value: object, field: str) -> str:
    if isinstance(value, Mapping):
        value = value.get("observed")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} observed status must be a non-empty string")
    return value.strip().upper()


@dataclass(frozen=True)
class StatusPair:
    expected: str
    observed: str

    def to_dict(self) -> dict[str, str]:
        return {"expected": self.expected, "observed": self.observed}


@dataclass(frozen=True)
class Day0AuditRecord:
    strategy_id: str
    decision_for: date
    checked_at: datetime
    log_status: StatusPair
    signal_status: StatusPair
    risk_status: StatusPair
    order_status: StatusPair
    fill_status: StatusPair
    rejection_codes: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    overall_status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "day0_joinquant_simulation_audit_v1",
            "strategy_id": self.strategy_id,
            "decision_for": self.decision_for.isoformat(),
            "checked_at": self.checked_at.isoformat(),
            "log_status": self.log_status.to_dict(),
            "signal_status": self.signal_status.to_dict(),
            "risk_status": self.risk_status.to_dict(),
            "order_status": self.order_status.to_dict(),
            "fill_status": self.fill_status.to_dict(),
            "rejection_codes": list(self.rejection_codes),
            "evidence_refs": list(self.evidence_refs),
            "overall_status": self.overall_status,
        }


def _expected(strategy_id: str, hermes_status: str) -> dict[str, str]:
    if strategy_id == BASELINE_STRATEGY_ID:
        return {
            "log_status": "PRESENT",
            "signal_status": "EVALUATED",
            "risk_status": "NOT_APPLICABLE",
            "order_status": "RECORDED",
            "fill_status": "RECORDED",
        }
    if strategy_id != HERMES_STRATEGY_ID:
        raise ValueError(f"unknown strategy_id: {strategy_id}")
    if hermes_status in NON_GREEN_RISK:
        return {
            "log_status": "PRESENT",
            "signal_status": "SKIP_NEW",
            "risk_status": hermes_status,
            "order_status": "NO_NEW_ORDER",
            "fill_status": "NO_FILL",
        }
    return {
        "log_status": "PRESENT",
        "signal_status": "EVALUATED",
        "risk_status": "GREEN",
        "order_status": "RECORDED",
        "fill_status": "RECORDED",
    }


def make_day0_records(
    *,
    decision_for: date | str,
    checked_at: datetime | str,
    hermes_status: str,
    observed: Mapping[str, Mapping[str, object]] | None = None,
    evidence_refs: tuple[str, ...] | list[str] = (),
) -> tuple[Day0AuditRecord, Day0AuditRecord]:
    """Build baseline and Hermes records without making any external calls.

    ``observed`` is intentionally explicit.  Missing observations become
    ``NOT_OBSERVED`` and therefore cannot produce PASS.  A checked date after
    ``checked_at`` is rejected, which prevents future dates (including an
    unoccurred 2026-08-21) from being marked passed.
    """

    day = _day(decision_for)
    checked = _aware(checked_at)
    if day > checked.astimezone(SHANGHAI_TZ).date():
        raise ValueError("decision_for cannot be in the future of checked_at")
    risk = str(hermes_status).strip().upper()
    if risk not in RISK_STATUSES:
        raise ValueError("hermes_status must be GREEN, ORANGE, RED, MISSING, STALE, or INVALID")
    observed = observed or {}
    refs = tuple(str(ref).strip() for ref in evidence_refs if str(ref).strip())
    records: list[Day0AuditRecord] = []
    for strategy_id in STRATEGY_IDS:
        expected = _expected(strategy_id, risk)
        supplied = observed.get(strategy_id, {})
        if not isinstance(supplied, Mapping):
            raise ValueError(f"observed.{strategy_id} must be an object")
        pairs: dict[str, StatusPair] = {}
        for field, expected_value in expected.items():
            value = _status(supplied.get(field, "NOT_OBSERVED"), field)
            pairs[field] = StatusPair(expected_value, value)
        rejection_codes: list[str] = []
        if strategy_id == HERMES_STRATEGY_ID and risk in NON_GREEN_RISK:
            if pairs["signal_status"].observed != "SKIP_NEW" or pairs["order_status"].observed != "NO_NEW_ORDER":
                rejection_codes.append("HERMES_FAIL_CLOSED_VIOLATION")
            if pairs["fill_status"].observed not in {"NO_FILL", "NOT_OBSERVED"}:
                rejection_codes.append("HERMES_FILL_UNEXPECTED")
        for field, pair in pairs.items():
            if pair.expected != pair.observed:
                rejection_codes.append(f"{field.upper()}_MISMATCH")
        if not refs:
            rejection_codes.append("EVIDENCE_MISSING")
        overall = "PASS" if not rejection_codes else "FAIL"
        records.append(
            Day0AuditRecord(
                strategy_id=strategy_id,
                decision_for=day,
                checked_at=checked,
                log_status=pairs["log_status"],
                signal_status=pairs["signal_status"],
                risk_status=pairs["risk_status"],
                order_status=pairs["order_status"],
                fill_status=pairs["fill_status"],
                rejection_codes=tuple(dict.fromkeys(rejection_codes)),
                evidence_refs=refs,
                overall_status=overall,
            )
        )
    return records[0], records[1]


class Day0AuditJournal:
    """Append-only local JSONL journal for Day-0 records."""

    def __init__(self, path: str | Path):
        self._audit = AuditLog(path)

    @property
    def path(self) -> Path:
        return self._audit.path

    def append(self, record: Day0AuditRecord) -> None:
        self._audit.append("day0_joinquant_simulation", **record.to_dict())
