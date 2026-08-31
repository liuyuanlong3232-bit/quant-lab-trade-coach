import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from quant_lab.trade_calendar import CN_TZ, TushareTradeCalendarUpdater


class TushareTradeCalendarUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old = {name: os.environ.get(name) for name in ("TUSHARE_TOKEN", "TUSHARE_TOKEN_FILE")}
        os.environ.pop("TUSHARE_TOKEN_FILE", None)
        os.environ["TUSHARE_TOKEN"] = "secret-must-never-appear"
        self.now = datetime(2026, 9, 1, 15, 20, tzinfo=CN_TZ)

    def tearDown(self):
        for name, value in self.old.items():
            if value is None: os.environ.pop(name, None)
            else: os.environ[name] = value
        self.temp.cleanup()

    def response(self):
        start = self.now.date() - timedelta(days=31)
        end = self.now.date() + timedelta(days=370)
        items = []
        day = start
        while day <= end:
            items.append([day.strftime("%Y%m%d"), 0 if day.weekday() >= 5 else 1, ""])
            day += timedelta(days=1)
        return json.dumps({"code": 0, "data": {"fields": ["cal_date", "is_open", "pretrade_date"], "items": items}}).encode()

    def test_atomic_versioned_publish_covers_more_than_twelve_months(self):
        updater = TushareTradeCalendarUpdater(self.root, http=lambda _body, _timeout: self.response(), now=lambda: self.now)
        result = updater.update()
        self.assertEqual(result["status"], "UPDATED")
        self.assertNotIn("secret-must-never-appear", json.dumps(result))
        manifest_path = self.root / "data" / "trade_coach" / "source_cache" / "tushare_trade_calendar_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertGreaterEqual((datetime.fromisoformat(manifest["coverage_end"]).date() - self.now.date()).days, 365)
        versioned = manifest_path.parent / manifest["path"]
        self.assertTrue(versioned.is_file())
        import hashlib
        self.assertEqual(hashlib.sha256(versioned.read_bytes()).hexdigest(), manifest["sha256"])

    def test_failed_update_preserves_old_manifest_and_never_echoes_token(self):
        good = TushareTradeCalendarUpdater(self.root, http=lambda _body, _timeout: self.response(), now=lambda: self.now)
        self.assertEqual(good.update()["status"], "UPDATED")
        manifest_path = self.root / "data" / "trade_coach" / "source_cache" / "tushare_trade_calendar_manifest.json"
        before = manifest_path.read_bytes()
        failed = TushareTradeCalendarUpdater(self.root, http=lambda _body, _timeout: (_ for _ in ()).throw(RuntimeError("secret-must-never-appear")), now=lambda: self.now + timedelta(days=1)).update()
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(manifest_path.read_bytes(), before)
        self.assertNotIn("secret-must-never-appear", json.dumps(failed))

    def test_missing_token_fails_closed_without_network(self):
        os.environ.pop("TUSHARE_TOKEN", None)
        calls = []
        result = TushareTradeCalendarUpdater(self.root, http=lambda *_: calls.append(1) or b"{}", now=lambda: self.now).update()
        self.assertEqual(result["status"], "SKIPPED")
        self.assertIn("TUSHARE_TOKEN_NOT_CONFIGURED", result["reason_codes"])
        self.assertEqual(calls, [])

    def test_incomplete_future_coverage_does_not_publish(self):
        incomplete = json.dumps({"code": 0, "data": {"fields": ["cal_date", "is_open"], "items": [["20260901", 1]]}}).encode()
        result = TushareTradeCalendarUpdater(self.root, http=lambda *_: incomplete, now=lambda: self.now).update()
        self.assertEqual(result["status"], "FAILED")
        self.assertFalse((self.root / "data" / "trade_coach" / "source_cache" / "tushare_trade_calendar_manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
