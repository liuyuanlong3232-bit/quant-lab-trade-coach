"""Append-only Sprint 1 storage: raw/normalized snapshots, manifests, and foundation DB."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .forward_probe import Observation, CN_TZ


def _local_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    raw = str(path)
    if "://" in raw or raw.startswith(("\\\\", "//")) or candidate.anchor.startswith(("\\\\", "//")):
        raise ValueError("forward storage must be local")
    return candidate.resolve()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class SnapshotStore:
    """Never replaces a source response or observation; every write appends."""

    def __init__(self, root: str | Path):
        self.root = _local_path(root)
        self.raw_root = self.root / "raw"
        self.observation_root = self.root / "observations"
        self.manifest_root = self.root / "manifests"
        for directory in (self.raw_root, self.observation_root, self.manifest_root):
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _day(observation: Observation) -> str:
        return observation.observed_at.astimezone(CN_TZ).date().isoformat()

    def append(
        self,
        observation: Observation,
        *,
        primary_raw: str,
        backup_raw: str,
        primary_source: str = "unknown_primary",
        backup_source: str = "unknown_backup",
    ) -> dict[str, str]:
        day = self._day(observation)
        raw_path = self.raw_root / f"{day}.jsonl"
        observation_path = self.observation_root / f"{day}.jsonl"
        raw_record = {
            "symbol": observation.symbol,
            "observed_at": observation.observed_at.astimezone(CN_TZ).isoformat(),
            "primary_source": primary_source,
            "backup_source": backup_source,
            "primary_response": primary_raw,
            "backup_response": backup_raw,
        }
        # Explicit append mode is intentional.  A blank raw response is still
        # retained as evidence of a failed source call.
        with raw_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(raw_record, ensure_ascii=False, sort_keys=True) + "\n")
        with observation_path.open("a", encoding="utf-8", newline="\n") as handle:
            with_ref = replace(observation, raw_ref=str(raw_path))
            handle.write(json.dumps(with_ref.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
        return {"raw": str(raw_path), "observation": str(observation_path)}

    def generate_manifest(self, day: str) -> Path:
        files = []
        for directory in (self.raw_root, self.observation_root):
            candidate = directory / f"{day}.jsonl"
            if candidate.is_file():
                files.append({"path": str(candidate.relative_to(self.root)), "sha256": sha256_file(candidate), "bytes": candidate.stat().st_size})
        actual_sources: set[str] = set()
        raw_file = self.raw_root / f"{day}.jsonl"
        if raw_file.is_file():
            for line in raw_file.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for key in ("primary_source", "backup_source"):
                    value = record.get(key)
                    if isinstance(value, str) and value != "unknown_primary" and value != "unknown_backup":
                        actual_sources.add(value)
        manifest = {
            "manifest_version": "quant_lab_probe_manifest_v1",
            "generated_at": datetime.now(CN_TZ).isoformat(),
            "observed_day": day,
            "files": files,
            "sources": sorted(actual_sources),
            "append_only": True,
        }
        target = self.manifest_root / f"{day}.json"
        if target.exists():
            # Do not silently overwrite a daily evidence manifest.
            suffix = datetime.now(CN_TZ).strftime("%H%M%S%f")
            target = self.manifest_root / f"{day}.{suffix}.json"
        target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return target


class FoundationLedger:
    """SQLite WAL foundation only.  Deliberately no orders or fills tables."""

    def __init__(self, path: str | Path):
        self.path = _local_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.initialize()

    def initialize(self) -> None:
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS market_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                source TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                exchange_time TEXT,
                open REAL, high REAL, low REAL, close REAL, volume REAL,
                status TEXT NOT NULL,
                reason_codes TEXT NOT NULL,
                raw_ref TEXT,
                UNIQUE(symbol, source, observed_at, id)
            );
            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                initial_cash REAL NOT NULL,
                created_at TEXT NOT NULL,
                version TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS versions (
                version TEXT PRIMARY KEY,
                manifest_hash TEXT,
                created_at TEXT NOT NULL,
                scope TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS probe_runs (
                run_id TEXT PRIMARY KEY,
                observed_at TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                check_point TEXT,
                overall_status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS probe_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                asset_class TEXT NOT NULL,
                primary_source TEXT NOT NULL,
                primary_status TEXT NOT NULL,
                primary_close REAL,
                primary_exchange_time TEXT,
                primary_latency_ms REAL,
                backup_source TEXT NOT NULL,
                backup_status TEXT NOT NULL,
                backup_close REAL,
                backup_exchange_time TEXT,
                backup_latency_ms REAL,
                selected_source TEXT NOT NULL,
                selected_status TEXT NOT NULL,
                selected_close REAL,
                selected_exchange_time TEXT,
                selected_latency_ms REAL,
                price_deviation_bps REAL,
                mapping_verified INTEGER NOT NULL DEFAULT 0,
                mapping_version TEXT NOT NULL DEFAULT 'unverified',
                contract_mapping TEXT NOT NULL DEFAULT '{}',
                primary_reason_codes TEXT NOT NULL DEFAULT '[]',
                backup_reason_codes TEXT NOT NULL DEFAULT '[]',
                reason_codes TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES probe_runs(run_id)
            );
            """
        )
        for column, definition in (("latency_ms", "REAL"), ("price_deviation_bps", "REAL")):
            try:
                self.connection.execute(f"ALTER TABLE market_observations ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        # Keep a previously-created Sprint 1A database readable while adding
        # only provenance columns; no historical evidence is rewritten.
        for column, definition in (
            ("mapping_version", "TEXT NOT NULL DEFAULT 'unverified'"),
            ("contract_mapping", "TEXT NOT NULL DEFAULT '{}'"),
            ("primary_reason_codes", "TEXT NOT NULL DEFAULT '[]'"),
            ("backup_reason_codes", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            try:
                self.connection.execute(f"ALTER TABLE probe_evidence ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower() and "no such table" not in str(exc).lower():
                    raise
        self.connection.commit()

    def seed_accounts(self, *, version: str = "HERMES-RESOURCE-v0.1") -> None:
        accounts = (
            ("buy_hold", "Buy and hold foundation", 200000.0),
            ("trend20", "20-day trend foundation", 200000.0),
            ("trend20_sector_hermes", "Trend + Sector + Hermes foundation", 200000.0),
            ("vps_full_factor", "VPS full-factor foundation", 200000.0),
        )
        now = datetime.now(CN_TZ).isoformat()
        self.connection.executemany(
            "INSERT OR IGNORE INTO accounts(account_id,name,initial_cash,created_at,version) VALUES(?,?,?,?,?)",
            [(account_id, name, cash, now, version) for account_id, name, cash in accounts],
        )
        self.connection.commit()

    def append_observation(self, observation: Observation) -> int:
        cursor = self.connection.execute(
            "INSERT INTO market_observations(symbol,source,observed_at,exchange_time,open,high,low,close,volume,status,reason_codes,raw_ref,latency_ms,price_deviation_bps) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (observation.symbol, observation.source, observation.observed_at.isoformat(), observation.exchange_time.isoformat() if observation.exchange_time else None, observation.open, observation.high, observation.low, observation.close, observation.volume, observation.status, json.dumps(observation.reason_codes), observation.raw_ref, observation.latency_ms, observation.price_deviation_bps),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def append_probe_run(self, run_id: str, observed_at: datetime, results: Iterable[tuple[Observation, Observation, Observation, str, str]], *, check_point: str | None = None) -> None:
        rows = tuple(results)
        statuses = [selected.status for _, _, selected, _, _ in rows]
        overall = "READY" if rows and all(status == "READY" for status in statuses) else ("CONFLICT" if "CONFLICT" in statuses else ("STALE" if "STALE" in statuses else "MISSING"))
        recorded_at = datetime.now(CN_TZ).isoformat()
        self.connection.execute("INSERT INTO probe_runs(run_id,observed_at,recorded_at,check_point,overall_status) VALUES(?,?,?,?,?)", (run_id, observed_at.isoformat(), recorded_at, check_point, overall))
        evidence_rows = []
        for primary, backup, selected, _, _ in rows:
            asset_class = "stock" if primary.symbol.endswith(".XSHE") or primary.symbol.endswith(".XSHG") else "future_factor"
            evidence_rows.append((
                run_id, primary.symbol, asset_class,
                primary.source, primary.status, primary.close, primary.exchange_time.isoformat() if primary.exchange_time else None, primary.latency_ms,
                json.dumps(primary.reason_codes, ensure_ascii=False),
                backup.source, backup.status, backup.close, backup.exchange_time.isoformat() if backup.exchange_time else None, backup.latency_ms,
                json.dumps(backup.reason_codes, ensure_ascii=False),
                selected.source, selected.status, selected.close, selected.exchange_time.isoformat() if selected.exchange_time else None, selected.latency_ms, selected.price_deviation_bps,
                int(selected.mapping_verified),
                selected.mapping_version,
                json.dumps(selected.contract_mapping or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(selected.reason_codes, ensure_ascii=False),
            ))
        self.connection.executemany("INSERT INTO probe_evidence(run_id,symbol,asset_class,primary_source,primary_status,primary_close,primary_exchange_time,primary_latency_ms,primary_reason_codes,backup_source,backup_status,backup_close,backup_exchange_time,backup_latency_ms,backup_reason_codes,selected_source,selected_status,selected_close,selected_exchange_time,selected_latency_ms,price_deviation_bps,mapping_verified,mapping_version,contract_mapping,reason_codes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", evidence_rows)
        self.connection.commit()

    def append_audit(self, event_type: str, payload: dict[str, object]) -> None:
        self.connection.execute("INSERT INTO audit_events(event_type,recorded_at,payload) VALUES(?,?,?)", (event_type, datetime.now(CN_TZ).isoformat(), json.dumps(payload, ensure_ascii=False, sort_keys=True)))
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
