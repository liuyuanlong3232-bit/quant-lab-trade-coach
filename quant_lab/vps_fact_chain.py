"""Read-only audit of the approved VPS macro fact chain.

The Personal Trade Coach is allowed to consume, but not silently repair, the
VPS event-calendar -> Prediction Gate -> ``vps_macro_risk_v1`` chain.  This
module audits an operator-supplied capture of those files.  It never opens an
SSH connection and never writes to a remote host; a capture is explicitly
reported as captured evidence rather than live remote state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
CHAIN_SCHEMA_VERSION = "quant_lab_vps_fact_chain_audit_v1"
CALENDAR_MAX_AGE_HOURS = 20.0


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(CN_TZ)


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _file_evidence(path: Path, *, root: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "relative_path": str(path.relative_to(root)) if path.is_relative_to(root) else str(path),
        "exists": path.is_file(),
        "sha256": _sha256(path) if path.is_file() else None,
    }


def audit_vps_fact_chain(evidence_dir: str | Path, *, now: datetime | None = None, calendar_max_age_hours: float = CALENDAR_MAX_AGE_HOURS) -> dict[str, Any]:
    """Audit a captured remote evidence directory without changing it.

    Expected captures are the files produced by the approved VPS work:
    ``economic_calendar_current.json``, ``pit-macro-dry-run-r3.json`` and
    ``remote_cron_live``.  Missing captures stay ``MISSING``.  A calendar with
    old data or a fetch error is always ``EVENT_CALENDAR_UNAVAILABLE`` even if
    it still contains old event rows.
    """

    root = Path(evidence_dir).expanduser().resolve()
    decision = (now or datetime.now(CN_TZ)).astimezone(CN_TZ)
    calendar_path = root / "economic_calendar_current.json"
    pit_path = root / "pit-macro-dry-run-r3.json"
    cron_path = root / "remote_cron_live"
    changelog_path = root / "remote_CHANGELOG_final.md"
    publisher_path = root / "remote_current_publish_vps_macro_risk_v1.py"

    calendar_reasons: list[str] = []
    calendar_meta: Mapping[str, Any] = {}
    calendar_events = 0
    if not calendar_path.is_file():
        calendar_reasons.append("EVENT_CALENDAR_CAPTURE_MISSING")
    else:
        try:
            decoded = _read_json(calendar_path)
            calendar_meta = decoded.get("meta") if isinstance(decoded, Mapping) and isinstance(decoded.get("meta"), Mapping) else {}
            calendar_events = len(decoded.get("events", [])) if isinstance(decoded, Mapping) and isinstance(decoded.get("events"), list) else 0
            success = _parse_time(calendar_meta.get("last_success_at") or calendar_meta.get("retrieved_at"))
            age_hours = (decision - success).total_seconds() / 3600 if success else None
            if not success:
                calendar_reasons.append("EVENT_CALENDAR_TIMESTAMP_MISSING")
            elif age_hours is not None and age_hours > calendar_max_age_hours:
                calendar_reasons.append("EVENT_CALENDAR_STALE")
            if calendar_meta.get("last_error_code"):
                calendar_reasons.append(f"EVENT_CALENDAR_LAST_ERROR:{calendar_meta.get('last_error_code')}")
            source = str(calendar_meta.get("source") or "").strip().lower()
            if source not in {"cache", "jin10"}:
                calendar_reasons.append("EVENT_CALENDAR_SOURCE_UNVERIFIED")
            if calendar_reasons:
                calendar_reasons.insert(0, "EVENT_CALENDAR_UNAVAILABLE")
            calendar_status = "READY" if not calendar_reasons else "MISSING"
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            calendar_status = "INVALID"
            calendar_reasons = ["EVENT_CALENDAR_UNAVAILABLE", "EVENT_CALENDAR_CAPTURE_INVALID"]
            age_hours = None
    if not calendar_path.is_file():
        calendar_status = "MISSING"
        age_hours = None
    calendar = {
        **_file_evidence(calendar_path, root=root),
        "status": calendar_status,
        "source": calendar_meta.get("source"),
        "last_success_at": calendar_meta.get("last_success_at") or calendar_meta.get("retrieved_at"),
        "last_error_code": calendar_meta.get("last_error_code"),
        "event_count": calendar_events,
        "age_hours": age_hours,
        "reason_codes": list(dict.fromkeys(calendar_reasons)),
    }

    pit_reasons: list[str] = []
    pit_status = "MISSING"
    pit_payload: Mapping[str, Any] = {}
    if not pit_path.is_file():
        pit_reasons.append("PREDICTION_GATE_CAPTURE_MISSING")
    else:
        try:
            decoded = _read_json(pit_path)
            outer = decoded if isinstance(decoded, Mapping) else {}
            snapshot = outer.get("snapshot") if isinstance(outer.get("snapshot"), Mapping) else outer
            pit_payload = dict(snapshot)
            missing = [str(value) for value in (snapshot.get("missing_field_list") or [])]
            errors = [str(value) for value in (snapshot.get("pit_errors") or [])]
            outer_status = str(outer.get("status") or snapshot.get("data_quality_flag") or "").upper()
            if outer_status == "MISSING" or missing or errors:
                pit_reasons.extend(errors)
                pit_reasons.extend(f"PREDICTION_GATE_FIELD_MISSING:{value}" for value in missing)
                pit_reasons.append("PREDICTION_GATE_MISSING")
                pit_status = "MISSING"
            else:
                pit_status = "READY"
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pit_status = "INVALID"
            pit_reasons.append("PREDICTION_GATE_CAPTURE_INVALID")
    pit = {
        **_file_evidence(pit_path, root=root),
        "status": pit_status,
        "generated_at": pit_payload.get("generated_at"),
        "snapshot_version": pit_payload.get("snapshot_version"),
        "missing_field_list": list(pit_payload.get("missing_field_list") or []),
        "reason_codes": list(dict.fromkeys(pit_reasons)),
    }

    cron_text = ""
    if cron_path.is_file():
        try:
            cron_text = cron_path.read_text(encoding="utf-8")
        except OSError:
            cron_text = ""
    cron_matches = bool(re.search(r"^35\s+16\s+\*\s+\*\s+1-5.*generate_pit_macro_snapshot\.py.*publish_vps_macro_risk_v1\.py", cron_text, re.MULTILINE))
    cron_reasons = [] if cron_matches else ["VPS_RISK_CRON_CAPTURE_MISSING_OR_UNEXPECTED"]
    cron = {
        **_file_evidence(cron_path, root=root),
        "status": "READY" if cron_matches else "MISSING",
        "expected_order": "16:25 USD -> 16:30 copper -> 16:35 PIT -> vps_macro_risk_v1",
        "matched": cron_matches,
        "reason_codes": cron_reasons,
    }

    publisher_present = publisher_path.is_file()
    publisher_reasons = [] if publisher_present else ["VPS_RISK_PUBLISHER_CAPTURE_MISSING"]
    # The captured directory intentionally does not count a dry-run script as
    # a published risk point.  A real output must be a separate JSONL artifact.
    output_candidates = [root / "vps_macro_risk_points.jsonl", root / "remote_vps_macro_risk_points.jsonl"]
    output_path = next((path for path in output_candidates if path.is_file()), None)
    if output_path is None:
        publisher_reasons.append("VPS_RISK_FACT_NOT_PUBLISHED")
    publisher = {
        **_file_evidence(publisher_path, root=root),
        "status": "READY" if publisher_present and output_path is not None else "MISSING",
        "output": _file_evidence(output_path, root=root) if output_path else None,
        "reason_codes": list(dict.fromkeys(publisher_reasons)),
    }

    reasons = list(dict.fromkeys([*calendar["reason_codes"], *pit["reason_codes"], *cron["reason_codes"], *publisher["reason_codes"]]))
    status = "READY" if calendar["status"] == pit["status"] == cron["status"] == publisher["status"] == "READY" else "MISSING"
    return {
        "schema_version": CHAIN_SCHEMA_VERSION,
        "status": status,
        "evidence_scope": "CAPTURED_REMOTE_ARTIFACTS_READ_ONLY",
        "live_ssh_verified": False,
        "audited_at": decision.isoformat(),
        "evidence_dir": str(root),
        "calendar": calendar,
        "prediction_gate": pit,
        "risk_publisher": publisher,
        "cron": cron,
        "reason_codes": reasons,
        "fail_closed": status != "READY",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", default=".codex-vps-pit-review", help="captured VPS evidence directory")
    parser.add_argument("--now", help="decision time with timezone, for deterministic audits")
    args = parser.parse_args(argv)
    decision = _parse_time(args.now) if args.now else None
    result = audit_vps_fact_chain(args.evidence_dir, now=decision)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["CALENDAR_MAX_AGE_HOURS", "CHAIN_SCHEMA_VERSION", "audit_vps_fact_chain"]
