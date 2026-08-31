import csv
import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from quant_lab.auto_refresh import AShareTradingCalendar, AutoRefreshScheduler, CN_TZ
from quant_lab.trade_coach import TradeCoachStore


class AutoRefreshSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = TradeCoachStore(self.root / "coach.sqlite3", seed_candidate=False)

    def tearDown(self):
        self.temp.cleanup()

    def write_calendar(self, rows):
        target = self.root / "data" / "trade_coach" / "source_cache"
        target.mkdir(parents=True)
        csv_path = target / "tushare_trade_calendar.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["cal_date", "is_open"])
            writer.writeheader(); writer.writerows(rows)
        manifest = {"source": "TUSHARE", "calendar": "SSE", "retrieved_at": "2026-08-30T12:00:00+08:00", "sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest()}
        (target / "tushare_trade_calendar_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_evidenced_slot_runs_once_across_restart(self):
        self.write_calendar([{"cal_date": "2026-09-01", "is_open": "1"}])
        calls = []; now = datetime(2026, 9, 1, 9, 36, tzinfo=CN_TZ)
        scheduler = AutoRefreshScheduler(self.store, self.root, lambda: calls.append("refresh") or {"status": "COMPLETE"}, points=("09:35",), now=lambda: now)
        self.assertEqual(scheduler.tick(now), ["2026-09-01@09:35"])
        restarted = AutoRefreshScheduler(self.store, self.root, lambda: calls.append("duplicate") or {}, points=("09:35",), now=lambda: now)
        self.assertEqual(restarted.tick(now), [])
        self.assertEqual(calls, ["refresh"])
        self.assertEqual(self.store.auto_refresh_audit(1)[0]["status"], "COMPLETE")

    def test_missed_slot_is_not_backfilled(self):
        self.write_calendar([{"cal_date": "2026-09-01", "is_open": "1"}])
        calls = []; now = datetime(2026, 9, 1, 9, 40, tzinfo=CN_TZ)
        scheduler = AutoRefreshScheduler(self.store, self.root, lambda: calls.append("refresh") or {}, points=("09:35",), grace_minutes=4, now=lambda: now)
        self.assertEqual(scheduler.tick(now), [])
        self.assertEqual(calls, [])
        self.assertEqual(self.store.auto_refresh_audit(), [])

    def test_weekend_and_unknown_calendar_skip_without_network(self):
        calls = []; weekend = datetime(2026, 9, 5, 9, 35, tzinfo=CN_TZ)
        scheduler = AutoRefreshScheduler(self.store, self.root, lambda: calls.append("network") or {}, points=("09:35",), now=lambda: weekend)
        scheduler.tick(weekend)
        self.assertEqual(calls, [])
        self.assertIn("A_SHARE_WEEKEND", self.store.auto_refresh_audit(1)[0]["reason_codes"])
        weekday = datetime(2026, 9, 7, 9, 35, tzinfo=CN_TZ)
        scheduler.tick(weekday)
        self.assertEqual(calls, [])
        self.assertIn("A_SHARE_TRADING_CALENDAR_UNAVAILABLE", self.store.auto_refresh_audit(1)[0]["reason_codes"])

    def test_holiday_and_bad_hash_fail_closed(self):
        self.write_calendar([{"cal_date": "2026-10-01", "is_open": "0"}])
        self.assertEqual(AShareTradingCalendar(self.root).status(datetime(2026, 10, 1).date()).status, "CLOSED")
        csv_path = self.root / "data" / "trade_coach" / "source_cache" / "tushare_trade_calendar.csv"
        csv_path.write_text(csv_path.read_text(encoding="utf-8") + "2026-10-02,0\n", encoding="utf-8")
        self.assertEqual(AShareTradingCalendar(self.root).status(datetime(2026, 10, 2).date()).status, "UNKNOWN")

    def test_provider_failure_is_explicit_and_not_retried(self):
        self.write_calendar([{"cal_date": "2026-09-01", "is_open": "1"}])
        now = datetime(2026, 9, 1, 15, 10, tzinfo=CN_TZ)
        def fail(): raise TimeoutError("provider timeout")
        scheduler = AutoRefreshScheduler(self.store, self.root, fail, points=("15:10",), now=lambda: now)
        scheduler.tick(now); scheduler.tick(now)
        rows = self.store.auto_refresh_audit()
        self.assertEqual([row["status"] for row in rows], ["FAILED", "CLAIMED"])
        self.assertIn("AUTO_REFRESH_ERROR:TimeoutError", rows[0]["reason_codes"])

    def test_calendar_initialization_and_daily_maintenance_are_idempotent(self):
        updates = []; market = []
        morning = datetime(2026, 9, 1, 8, 0, tzinfo=CN_TZ)
        missing = AutoRefreshScheduler(self.store, self.root, lambda: market.append(1) or {}, calendar_update=lambda: updates.append("init") or {"status": "UPDATED", "reason_codes": []}, points=("09:35",), now=lambda: morning)
        missing.tick(morning); missing.tick(morning)
        self.assertEqual(updates, ["init"])
        self.assertEqual(market, [])

        self.write_calendar([{"cal_date": "2026-09-01", "is_open": "1"}])
        close = datetime(2026, 9, 1, 15, 20, tzinfo=CN_TZ)
        daily = AutoRefreshScheduler(self.store, self.root, lambda: market.append(1) or {}, calendar_update=lambda: updates.append("daily") or {"status": "UPDATED", "reason_codes": []}, points=("09:35",), now=lambda: close)
        daily.tick(close); daily.tick(close)
        self.assertEqual(updates, ["init", "daily"])
        self.assertEqual(market, [])


if __name__ == "__main__":
    unittest.main()
