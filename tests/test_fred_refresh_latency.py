import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quant_lab.trade_coach import (
    INSTRUMENT_SPECS,
    MarketObservation,
    RealMarketCollector,
    TradeCoachStore,
)


class FredRefreshLatencyTests(unittest.TestCase):
    def test_fred_series_are_fetched_in_parallel_with_bounded_retry_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TradeCoachStore(Path(directory) / "coach.sqlite3")
            collector = RealMarketCollector(store, project_root=directory, fred_timeout=0.2, fred_retries=2)
            fred_specs = [spec for spec in INSTRUMENT_SPECS if spec.source == "FRED"]
            calls = []

            def fake_history(spec, observed_at):
                calls.append(spec.provider_symbol)
                time.sleep(0.06)
                observation = MarketObservation(
                    spec.symbol, "FRED", observed_at, observed_at, None, None, None,
                    1.0, None, 1.0, "MISSING", reason_codes=("FRED_FETCH_ERROR:TimeoutError",),
                )
                return [observation], {"source": "FRED", "attempts": 3, "error": "TimeoutError"}

            with patch.object(collector, "ingest_local_evidence", return_value={}), \
                 patch.object(collector, "_fred_history", side_effect=fake_history), \
                 patch("quant_lab.trade_coach.INSTRUMENT_SPECS", tuple(fred_specs)):
                started = time.monotonic()
                result = collector.refresh(include_live=True)
                elapsed = time.monotonic() - started

            self.assertEqual(sorted(calls), sorted(spec.provider_symbol for spec in fred_specs))
            self.assertEqual(len(calls), len(fred_specs))
            # Four 60ms workers should not take four times the fixture delay.
            self.assertLess(elapsed, 0.18)
            self.assertTrue(all(item["status"] == "MISSING" for item in result["details"] if item["symbol"] in {s.symbol for s in fred_specs}))


if __name__ == "__main__":
    unittest.main()
