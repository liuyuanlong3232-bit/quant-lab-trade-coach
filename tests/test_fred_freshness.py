import json
import unittest
from datetime import datetime

from quant_lab.trade_coach import CN_TZ, _fresh_status, _us_business_day


def fred_row(day: str, *, metadata=None):
    return {
        "source": "FRED", "status": "READY", "close": 2.34,
        "exchange_time": day, "contract_mapping": json.dumps(metadata or {}),
    }


class FredFreshnessTests(unittest.TestCase):
    def test_friday_weekend_and_monday_before_window_ready(self):
        row = fred_row("2026-08-28")
        for now in ("2026-08-29T12:00:00+08:00", "2026-08-30T12:00:00+08:00", "2026-08-31T16:00:00+08:00"):
            status, _ = _fresh_status(row, now=datetime.fromisoformat(now), max_hours=1)
            self.assertEqual(status, "READY")

    def test_fallback_window_overdue_is_stale(self):
        row = fred_row("2026-08-28")
        status, reasons = _fresh_status(row, now=datetime.fromisoformat("2026-09-01T02:00:00+00:00"), max_hours=1)
        self.assertEqual(status, "STALE")
        self.assertIn("FRED_RELEASE_WINDOW_OVERDUE_FALLBACK", reasons)

    def test_weekend_provider_poll_extends_only_to_next_release_window(self):
        row = fred_row("2026-08-27", metadata={"fetched_at": "2026-08-29T19:46:52+08:00"})
        status, reasons = _fresh_status(row, now=datetime.fromisoformat("2026-08-31T16:00:00+08:00"), max_hours=1)
        self.assertEqual(status, "READY")
        self.assertIn("FRED_PROVIDER_POLL_FALLBACK", reasons)
        status, reasons = _fresh_status(row, now=datetime.fromisoformat("2026-09-01T08:00:00+08:00"), max_hours=1)
        self.assertEqual(status, "STALE")
        self.assertIn("FRED_RELEASE_WINDOW_OVERDUE_FALLBACK", reasons)

    def test_holiday_does_not_count_as_release_day(self):
        # Friday before Labor Day: Tuesday is the next US business day.
        row = fred_row("2026-09-04")
        status, _ = _fresh_status(row, now=datetime.fromisoformat("2026-09-07T22:00:00+00:00"), max_hours=1)
        self.assertEqual(status, "READY")

    def test_official_release_metadata_wins(self):
        row = fred_row("2026-08-27", metadata={"fred_updated_at": "2026-08-28T15:16:00-05:00", "fred_next_release": "2026-08-31"})
        status, reasons = _fresh_status(row, now=datetime.fromisoformat("2026-08-31T20:00:00+00:00"), max_hours=1)
        self.assertEqual(status, "READY")
        self.assertIn("FRED_OFFICIAL_RELEASE_WINDOW_OPEN", reasons)

        status, reasons = _fresh_status(row, now=datetime.fromisoformat("2026-09-01T06:00:00+00:00"), max_hours=1)
        self.assertEqual(status, "STALE")
        self.assertIn("FRED_OFFICIAL_RELEASE_WINDOW_MISSED", reasons)

    def test_memorial_day_and_cross_year_new_year_are_holidays(self):
        self.assertFalse(_us_business_day(datetime.fromisoformat("2026-05-25").date()))
        self.assertTrue(_us_business_day(datetime.fromisoformat("2026-06-29").date()))
        self.assertFalse(_us_business_day(datetime.fromisoformat("2021-12-31").date()))

    def test_bad_timezone_and_missing_observation_fail_closed(self):
        row = fred_row("2026-08-27", metadata={"fred_next_release": "2026-08-31T15:00:00"})
        self.assertEqual(_fresh_status(row, now=datetime.now(CN_TZ), max_hours=1)[0], "MISSING")
        self.assertEqual(_fresh_status(fred_row(""), now=datetime.now(CN_TZ), max_hours=1)[0], "MISSING")


if __name__ == "__main__":
    unittest.main()
