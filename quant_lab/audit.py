"""Append-only JSONL audit trail, constrained to a caller-selected local directory."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: str | Path):
        target = Path(path).expanduser()
        raw_path = str(path)
        if "://" in raw_path or raw_path.startswith(("\\\\", "//")) or target.anchor.startswith(("\\\\", "//")):
            raise ValueError("audit path must be local")
        self.path = target.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: str, **payload: Any) -> None:
        record = {"event": event, "recorded_at": datetime.now(timezone.utc).isoformat(), **payload}
        def encode(value: Any) -> str:
            if hasattr(value, "isoformat"):
                return value.isoformat()
            return str(value)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=encode) + "\n")
