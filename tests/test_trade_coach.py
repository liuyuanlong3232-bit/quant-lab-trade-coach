import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from quant_lab.trade_coach import (
    CN_TZ,
    DeepSeekMentorProvider,
    MarketObservation,
    MiMoMentorProvider,
    MultiMentorProvider,
    NotificationService,
    OpenAICompatibleMentorProvider,
    PublicEvidenceVerifier,
    TradeCoachService,
    TradeCoachStore,
    WebhookNotificationAdapter,
    QQBotNotificationAdapter,
    build_instrument_states,
    build_advice,
    evaluate_market_regime,
    evaluate_stock_state,
    load_vps_facts,
    risk_assessment,
    _series_values,
)
from quant_lab.vps_fact_chain import audit_vps_fact_chain


class FakeHTTPResponse:
    def __init__(self, body=b"{}", status=200):
        self.body = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return self.body


class TradeCoachTests(unittest.TestCase):
    def test_scheduled_refresh_skips_when_manual_refresh_lock_is_held(self):
        service = TradeCoachService(self.make_store().path, project_root=Path.cwd(), bootstrap=False)
        service._refresh_lock.acquire()
        try:
            self.assertEqual(service._scheduled_refresh()["scheduler_status"], "SKIPPED_BUSY")
        finally:
            service._refresh_lock.release()

    def test_adjusted_series_never_falls_back_to_raw_close(self):
        rows = [
            {"close": 41.58, "adjusted_close": None},
            {"close": 42.00, "adjusted_close": 804.092},
        ]
        self.assertEqual(_series_values(rows, adjusted=True), [804.092])
        self.assertEqual(_series_values(rows, adjusted=False), [41.58, 42.0])

    def make_store(self):
        self.temp = tempfile.TemporaryDirectory()
        return TradeCoachStore(Path(self.temp.name) / "coach.sqlite3")

    def tearDown(self):
        if hasattr(self, "temp"):
            self.temp.cleanup()

    def add_series(self, store, symbol, start, slope=0.01):
        base = 10.0
        for index in range(25):
            value = base * (1 + slope) ** index
            stamp = start + timedelta(days=index)
            store.append_observation(MarketObservation(symbol, "test-real-source", stamp, stamp, value, value * 1.01, value * 0.99, value, 1000, value, "READY", source_ref="test-fixture", raw_hash=f"{symbol}-{index}"))

    def advice_for_position(self, shares, regime_code, *, total_assets=50000.0, planned_cash_out=5000.0):
        with tempfile.TemporaryDirectory() as directory:
            store = TradeCoachStore(Path(directory) / "coach.sqlite3")
            self.add_series(store, "000426.XSHE", datetime(2026, 7, 1, tzinfo=CN_TZ), 0.01)
            store.append_account_snapshot(
                status="CONFIRMED", shares=shares, avg_cost=34.751, available_cash=10000.0,
                total_assets=total_assets, planned_cash_out=planned_cash_out,
                source="USER_EXPLICIT_CONFIRMATION", note="position-band-test",
            )
            regime = {"code": regime_code, "label": regime_code, "evidence_status": "COMPLETE", "available_symbols": [], "missing_symbols": []}
            stock = {"code": "SYNC", "label": "与商品和板块同步", "evidence_status": "COMPLETE"}
            return build_advice(store, regime, stock, risk_assessment(load_vps_facts(None)))

    def test_candidate_is_not_current_until_explicit_confirmation(self):
        store = self.make_store()
        account = store.account()
        self.assertEqual(account["candidate"]["status"], "PENDING_USER_CONFIRMATION")
        self.assertIsNone(account["confirmed"])
        store.append_account_snapshot(status="CONFIRMED", shares=600, avg_cost=34.751, available_cash=9720.25, total_assets=34668.25, source="USER_EXPLICIT_CONFIRMATION", note="test")
        self.assertEqual(store.account()["confirmed"]["shares"], 600)

    def test_tushare_tin_main_history_retains_concrete_contract(self):
        store = self.make_store()
        root = Path(self.temp.name) / "project"
        cache = root / "data" / "trade_coach" / "source_cache"
        cache.mkdir(parents=True)
        row = {
            "schema_version": "tushare_tin_main_history_v1",
            "trade_date": "2026-08-28",
            "available_at": "2026-08-30T00:40:00+08:00",
            "source": "TUSHARE",
            "product": "SN.SHF",
            "mapping_ts_code": "SN2610.SHF",
            "open": 420000.0,
            "high": 425000.0,
            "low": 418000.0,
            "close": 423870.0,
            "settle": 422100.0,
            "volume": 1000.0,
            "open_interest": 2000.0,
            "mapping_version": "tushare_fut_mapping_v1",
            "series_semantics": "SHFE_TIN_DAILY_MAIN_CONTRACT_UNADJUSTED",
        }
        (cache / "tushare_tin_main_history_v1.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        service = TradeCoachService(store.path, project_root=root, bootstrap=False)
        self.assertEqual(service.collector.ingest_local_evidence()["tin"], 1)
        saved = service.store.history("TIN", 5)[0]
        self.assertEqual(json.loads(saved["contract_mapping"])["contract"], "SN2610.SHF")
        self.assertEqual(saved["source"], "TUSHARE_TIN_MAIN local")

    def test_missing_vps_is_not_green_or_exit(self):
        facts = load_vps_facts(None)
        self.assertEqual(facts.status, "MISSING")
        risk = risk_assessment(facts, company_risk_confirmed=True)
        self.assertNotEqual(risk["status"], "CONFIRMED_MAJOR_RISK")
        self.assertFalse(risk["exit_allowed"])

    def test_vps_loader_never_uses_future_point_in_time_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vps.jsonl"
            path.write_text(json.dumps({
                "generated_at": "2026-08-30T09:00:00+08:00",
                "valid_until": "2026-08-31T15:00:00+08:00",
                "risk_level": "GREEN",
                "source": "HERMES",
                "model_version": "vps_macro_risk_v1",
                "prediction_gate_status": "ACTIVE",
                "macro_event_gate": "NORMAL",
            }) + "\n", encoding="utf-8")
            facts = load_vps_facts(path, decision_at=datetime(2026, 8, 29, 18, tzinfo=CN_TZ))
            self.assertEqual(facts.status, "MISSING")
            self.assertIn("VPS_FACT_NO_PIT_RECORD", facts.reason_codes)

    def test_vps_calendar_and_prediction_gate_missing_stay_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vps.jsonl"
            base = {
                "generated_at": "2026-08-29T09:00:00+08:00",
                "valid_until": "2026-08-30T15:00:00+08:00",
                "risk_level": "RED",
                "source": "HERMES",
                "model_version": "vps_macro_risk_v1",
                "prediction_gate_status": "ACTIVE",
                "macro_event_gate": "NORMAL",
            }
            path.write_text(json.dumps({**base, "macro_event_gate": "EVENT_CALENDAR_UNAVAILABLE"}) + "\n", encoding="utf-8")
            facts = load_vps_facts(path, decision_at=datetime(2026, 8, 29, 18, tzinfo=CN_TZ))
            self.assertEqual(facts.status, "MISSING")
            self.assertIn("EVENT_CALENDAR_UNAVAILABLE", facts.reason_codes)
            path.write_text(json.dumps({**base, "prediction_gate_status": "MISSING"}) + "\n", encoding="utf-8")
            facts = load_vps_facts(path, decision_at=datetime(2026, 8, 29, 18, tzinfo=CN_TZ))
            self.assertEqual(facts.status, "MISSING")
            self.assertIn("PREDICTION_GATE_UNAVAILABLE", facts.reason_codes)

    def test_complete_up_regime_generates_legal_non_automatic_advice(self):
        store = self.make_store()
        start = datetime(2026, 7, 1, tzinfo=CN_TZ)
        for symbol in ("000426.XSHE", "SILVER", "GOLD", "TIN", "COPPER", "OIL", "801050.SI"):
            self.add_series(store, symbol, start, 0.01)
        for symbol in ("DXY", "REAL10Y"):
            self.add_series(store, symbol, start, -0.01)
        store.append_account_snapshot(status="CONFIRMED", shares=600, avg_cost=34.751, available_cash=9720.25, total_assets=34668.25, source="USER_EXPLICIT_CONFIRMATION", note="test")
        regime = evaluate_market_regime(store)
        stock = evaluate_stock_state(store)
        advice = build_advice(store, regime, stock, risk_assessment(load_vps_facts(None)))
        self.assertEqual(regime["code"], "TREND_UP")
        self.assertIn(advice["action"], {"HOLD", "ADD_IN_STEPS", "REDUCE_IN_STEPS", "WAIT"})
        self.assertTrue(advice["manual_confirmation_required"])
        self.assertFalse(advice["automatic_trading"])
        self.assertNotEqual(advice["action"], "EXIT_MAJOR_RISK")

    def test_trend_up_position_band_emits_add_hold_and_reduce(self):
        add = self.advice_for_position(500, "TREND_UP")
        self.assertEqual(add["action"], "ADD_IN_STEPS")
        self.assertEqual(add["recommended_share_range"][1] % 100, 0)

        capacity = add["recommended_share_range"][1]
        hold = self.advice_for_position(capacity, "TREND_UP")
        self.assertEqual(hold["action"], "HOLD")

        reduce = self.advice_for_position(3000, "TREND_UP")
        self.assertEqual(reduce["action"], "REDUCE_IN_STEPS")
        for advice in (add, hold, reduce):
            self.assertTrue(advice["manual_confirmation_required"])
            self.assertFalse(advice["automatic_trading"])

    def test_range_position_band_emits_add_hold_and_reduce(self):
        add = self.advice_for_position(300, "RANGE")
        self.assertEqual(add["action"], "ADD_IN_STEPS")
        capacity = add["recommended_share_range"][1]
        hold = self.advice_for_position(capacity, "RANGE")
        self.assertEqual(hold["action"], "HOLD")
        reduce = self.advice_for_position(3000, "RANGE")
        self.assertEqual(reduce["action"], "REDUCE_IN_STEPS")

    def test_planned_cash_out_reduces_position_capacity(self):
        without_withdrawal = self.advice_for_position(500, "TREND_UP", planned_cash_out=0.0)
        with_withdrawal = self.advice_for_position(500, "TREND_UP", planned_cash_out=20000.0)
        self.assertLess(with_withdrawal["recommended_share_range"][1], without_withdrawal["recommended_share_range"][1])

    def test_down_mode_keeps_one_lot_as_ordinary_floor(self):
        advice = self.advice_for_position(100, "DOWN")
        self.assertEqual(advice["recommended_share_range"], [100, 100])
        self.assertEqual(advice["action"], "HOLD")

    def test_down_mode_empty_account_stays_empty(self):
        advice = self.advice_for_position(0, "DOWN")
        self.assertEqual(advice["recommended_share_range"], [0, 0])
        self.assertEqual(advice["action"], "HOLD")
        self.assertNotEqual(advice["action"], "ADD_IN_STEPS")
        self.assertNotEqual(advice["action"], "EXIT_MAJOR_RISK")

    def test_down_regime_does_not_implicitly_clear(self):
        store = self.make_store()
        start = datetime(2026, 7, 1, tzinfo=CN_TZ)
        for symbol in ("000426.XSHE", "SILVER", "GOLD", "TIN", "COPPER", "OIL", "801050.SI"):
            self.add_series(store, symbol, start, -0.01)
        for symbol in ("DXY", "REAL10Y"):
            self.add_series(store, symbol, start, 0.01)
        store.append_account_snapshot(status="CONFIRMED", shares=600, avg_cost=34.751, available_cash=9720.25, total_assets=34668.25, source="USER_EXPLICIT_CONFIRMATION", note="test")
        regime = evaluate_market_regime(store)
        stock = evaluate_stock_state(store)
        advice = build_advice(store, regime, stock, risk_assessment(load_vps_facts(None)))
        self.assertEqual(regime["code"], "DOWN")
        self.assertEqual(advice["action"], "REDUCE_IN_STEPS")  # ordinary decline may reduce, never clear automatically
        self.assertNotEqual(advice["action"], "EXIT_MAJOR_RISK")

    def test_event_and_diary_are_append_only_and_deduplicated(self):
        store = self.make_store()
        first = store.upsert_event(event_key="source", event_type="数据源状态", payload={"status": "MISSING"})
        second = store.upsert_event(event_key="source", event_type="数据源状态", payload={"status": "MISSING"})
        self.assertTrue(first["is_new_notification"])
        self.assertFalse(second["is_new_notification"])
        transition_one = store.upsert_event(event_key="regime", event_type="模式变化", payload={"state_fingerprint": "same", "change_summary": "首次记录"})
        transition_two = store.upsert_event(event_key="regime", event_type="模式变化", payload={"state_fingerprint": "same", "change_summary": "状态保持"})
        self.assertEqual(transition_one["id"], transition_two["id"])
        self.assertFalse(transition_two["is_new_notification"])
        one = store.append_diary(layer="市场事实", content={"value": "MISSING"})
        two = store.append_diary(layer="导师判断", content={"value": "WAIT"})
        self.assertEqual(two["prev_hash"], one["record_hash"])
        self.assertEqual(len(store.diary(10)), 2)

    def test_narrative_polling_does_not_create_revisions_without_new_facts(self):
        service = TradeCoachService(self.make_store().path, project_root=Path.cwd(), bootstrap=False)
        first = service.summary()
        count_after_first = len(service.store.diary(100))
        narrative_rows_after_first = service.store.latest_narrative()
        second = service.summary()
        self.assertEqual(first["narrative"]["summary"], second["narrative"]["summary"])
        self.assertEqual(narrative_rows_after_first["id"], service.store.latest_narrative()["id"])
        self.assertEqual(count_after_first, len(service.store.diary(100)))

    def test_long_term_diary_observation_is_persisted_as_memory(self):
        service = TradeCoachService(self.make_store().path, project_root=Path.cwd(), bootstrap=False)
        service.record_diary({"layer": "长期记忆", "content": {"kind": "HYPOTHESIS", "text": "测试假设"}})
        self.assertEqual(service.store.memories(1)[0]["content"], "测试假设")

    def test_http_contract_exposes_summary_and_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            service = TradeCoachService(Path(directory) / "coach.sqlite3", project_root=Path.cwd(), bootstrap=False)
            status, summary = service.handle("GET", "/api/trade-coach/summary")
            self.assertEqual(status, 200)
            self.assertEqual(summary["account"]["status"], "PENDING_USER_CONFIRMATION")
            status, payload = service.handle("POST", "/api/trade-coach/account/confirm", {"shares": 600, "avg_cost": 34.751, "available_cash": 9720.25, "total_assets": 34668.25})
            self.assertEqual(status, 200)
            self.assertEqual(payload["confirmed"]["status"], "CONFIRMED")

    def test_trade_coach_launcher_forwards_named_parameters(self):
        launcher = (Path(__file__).parents[1] / "scripts" / "start_trade_coach.ps1").read_text(encoding="utf-8")
        self.assertIn("$invokeParameters = @{", launcher)
        self.assertIn("& $Launcher @invokeParameters", launcher)
        self.assertIn("$PSVersionTable.PSVersion.Major -lt 7", launcher)
        self.assertIn("PowerShell\\7\\pwsh.exe", launcher)
        self.assertIn("continuing with Windows PowerShell 5.1 compatibility mode", launcher)
        self.assertNotIn('$arguments = @("-ApiPort"', launcher)
        self.assertNotRegex(launcher, r"(?m)^\s*exit\b")

    def test_browser_runtime_check_asserts_rendered_dom_proxy_and_console(self):
        check = (Path(__file__).parents[1] / "scripts" / "check_trade_coach_browser.mjs").read_text(encoding="utf-8")
        self.assertIn("Runtime.evaluate", check)
        self.assertIn("#root", check)
        self.assertIn("consoleAPICalled", check)
        self.assertIn("api_proxy_status", check)
        self.assertIn("Rendered #root is empty", check)

    @unittest.skipUnless(sys.platform == "win32", "Windows PowerShell 5.1 integration test")
    def test_windows_powershell_51_wrapper_stays_alive_after_http_ready(self):
        """Exercise the user-facing wrapper, not only the delegated child script."""

        root = Path(__file__).parents[1]
        powershell = shutil.which("powershell.exe")
        if not powershell:
            candidate = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            powershell = str(candidate) if candidate.is_file() else None
        if not powershell:
            self.skipTest("Windows PowerShell 5.1 is unavailable")

        version = subprocess.check_output(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "$PSVersionTable.PSVersion.Major"],
            cwd=root,
            text=True,
            encoding="ascii",
            errors="replace",
            timeout=10,
        ).strip()
        self.assertEqual(version, "5", "the regression must run under Windows PowerShell 5.1")

        def free_port():
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
                return int(sock.getsockname()[1])

        api_port = free_port()
        frontend_port = free_port()
        while frontend_port == api_port:
            frontend_port = free_port()

        wrapper = root / "scripts" / "start_trade_coach.ps1"
        # Windows can retain redirected console log handles for a few moments
        # after taskkill has ended the full launcher tree. The assertions below
        # prove runtime behavior; delayed temp-log reclamation is not a product
        # failure and must not make the suite flaky.
        with tempfile.TemporaryDirectory(prefix="launcher-ps51-", dir=str(root), ignore_cleanup_errors=True) as directory:
            temporary_root = Path(directory)
            relative_database = temporary_root.relative_to(root) / "trade_coach.sqlite3"
            command = [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(wrapper),
                "-ApiPort",
                str(api_port),
                "-FrontendPort",
                str(frontend_port),
                "-PollSeconds",
                "1",
                "-Database",
                str(relative_database),
            ]
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            stdout_path = temporary_root / "launcher.stdout.log"
            stderr_path = temporary_root / "launcher.stderr.log"
            stdout_handle = stdout_path.open("w", encoding="utf-8")
            stderr_handle = stderr_path.open("w", encoding="utf-8")
            try:
                process = subprocess.Popen(
                    command,
                    cwd=root,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creationflags,
                )
            finally:
                # The launcher owns separate runtime logs for its children;
                # close the parent-side handles so test cleanup cannot block
                # on inherited pipes after the process tree is stopped.
                stdout_handle.close()
                stderr_handle.close()

            def http_status(url):
                with urllib.request.urlopen(url, timeout=2) as response:
                    return int(response.status), response.read(8192)

            ready_at = None
            try:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        output = stdout_path.read_text(encoding="utf-8", errors="replace")
                        errors = stderr_path.read_text(encoding="utf-8", errors="replace")
                        self.fail("Windows PowerShell wrapper exited before HTTP readiness: " + (errors or output)[-2000:])
                    try:
                        status, _ = http_status(f"http://127.0.0.1:{api_port}/api/trade-coach/summary")
                        if status == 200:
                            ready_at = time.monotonic()
                            break
                    except (OSError, TimeoutError):
                        pass
                    time.sleep(0.25)
                self.assertIsNotNone(ready_at, "API did not return HTTP 200 within 45 seconds")

                time.sleep(10.5)
                self.assertIsNone(process.poll(), "Windows PowerShell wrapper did not remain alive for 10 seconds")
                self.assertGreaterEqual(time.monotonic() - ready_at, 10.0)
                status, _ = http_status(f"http://127.0.0.1:{api_port}/api/trade-coach/summary")
                self.assertEqual(status, 200)
                status, frontend_body = http_status(f"http://127.0.0.1:{frontend_port}/")
                self.assertEqual(status, 200)
                self.assertIn(b"id=\"root\"", frontend_body)
            finally:
                # Stop only this test's process tree and any child PIDs it
                # announced; never touch the user's existing default services.
                if process.poll() is None:
                    subprocess.run(
                        ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=10,
                    )
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                output = stdout_path.read_text(encoding="utf-8", errors="replace")
                announced_pids = {
                    int(pid)
                    for pid in re.findall(r"PID\s+(\d+)", output)
                    if int(pid) not in {os.getpid(), process.pid}
                }
                for pid in announced_pids:
                    subprocess.run(
                        [powershell, "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=10,
                    )

    def test_ai_provider_without_key_is_explicitly_fail_closed(self):
        provider = OpenAICompatibleMentorProvider(api_key="")
        status = provider.status()
        self.assertEqual(status["status"], "NOT_CONFIGURED")
        self.assertFalse(status["is_ai"])
        self.assertIsNone(status["model"])
        result = provider.explain(context={"action": "WAIT"}, memories=[], verification={"status": "NOT_REQUESTED"})
        self.assertEqual(result["status"], "NOT_CONFIGURED")
        self.assertFalse(result["is_ai"])
        self.assertTrue(result["fail_closed"])
        self.assertIn("OPENAI_API_KEY_UNSET", result["reason_codes"])

    def test_ai_provider_returns_auditable_structured_output(self):
        requested = []
        payload = {
            "summary": "仅解释已给出的证据",
            "drivers": ["白银趋势证据"],
            "risks": ["数据可能过期"],
            "counter_evidence": ["锡历史不足"],
            "questions": ["是否需要补充来源"],
            "source_references": ["https://example.test/source"],
            "confidence": "MEDIUM",
            "uncertainty": "仍需人工复核",
            "rule_action_reference": "WAIT",
        }

        def opener(request, timeout):
            requested.append((request, timeout))
            return FakeHTTPResponse(json.dumps({"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}))

        provider = OpenAICompatibleMentorProvider(api_key="test-secret", base_url="https://provider.test/v1", model="test-model", timeout=3, opener=opener)
        result = provider.explain(
            context={"deterministic_advice": {"action": "WAIT"}},
            memories=[{"id": 7, "content": "长期观察：缺失不是中性"}],
            verification={"status": "READY", "checked": []},
        )
        self.assertEqual(provider.status()["status"], "READY")
        self.assertEqual(result["status"], "READY")
        self.assertTrue(result["is_ai"])
        self.assertFalse(result["fail_closed"])
        self.assertEqual(result["structured_output"]["confidence"], "MEDIUM")
        self.assertEqual(result["memory_ids"], [7])
        self.assertEqual(len(requested), 1)
        self.assertNotIn("test-secret", requested[0][0].data.decode("utf-8"))
        self.assertIn("long_term_memory", requested[0][0].data.decode("utf-8"))

    def test_ai_provider_rejects_unstructured_response(self):
        def opener(_request, timeout):
            self.assertEqual(timeout, 2)
            return FakeHTTPResponse(json.dumps({"choices": [{"message": {"content": "not-json"}}]}))

        provider = OpenAICompatibleMentorProvider(api_key="test-secret", timeout=2, opener=opener)
        result = provider.explain(context={}, memories=[], verification={})
        self.assertEqual(result["status"], "INVALID_RESPONSE")
        self.assertTrue(result["fail_closed"])
        self.assertIn("AI_STRUCTURED_OUTPUT_INVALID", result["reason_codes"])

    def test_deepseek_catalog_selects_preferred_live_model(self):
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return FakeHTTPResponse(json.dumps({"data": [{"id": "deepseek-v4-pro"}, {"id": "deepseek-v4-flash"}]}))

        provider = DeepSeekMentorProvider(api_key="test-secret", opener=opener, timeout=2)
        discovery = provider.discover_models()
        self.assertEqual(discovery["status"], "READY")
        self.assertEqual(discovery["model"], "deepseek-v4-flash")
        self.assertEqual(provider.status()["model_source"], "FROZEN_DEFAULT_AND_CATALOG")
        self.assertEqual(requests[0][0].get_method(), "GET")
        self.assertEqual(requests[0][1], 2)

    def test_deepseek_frozen_default_survives_catalog_network_failure(self):
        def opener(_request, timeout):
            self.assertEqual(timeout, 2)
            raise urllib.error.URLError("catalog temporarily unavailable")

        provider = DeepSeekMentorProvider(api_key="test-secret", opener=opener, timeout=2)
        self.assertEqual(provider.model, "deepseek-v4-flash")
        discovery = provider.discover_models()
        self.assertEqual(discovery["status"], "PROVIDER_ERROR")
        status = provider.status()
        self.assertEqual(status["status"], "CONFIGURED_PENDING_CALL")
        self.assertEqual(status["model"], "deepseek-v4-flash")
        self.assertFalse(status["is_ai"])
        self.assertTrue(status["fail_closed"])

    def test_deepseek_direct_call_is_not_blocked_by_catalog_failure(self):
        payload = {
            "summary": "仅解释确定性规则",
            "drivers": ["fixture"],
            "risks": ["fixture only"],
            "counter_evidence": [],
            "questions": [],
            "source_references": [],
            "confidence": "LOW",
            "uncertainty": "fixture",
            "rule_action_reference": "WAIT",
        }

        def opener(request, timeout):
            self.assertEqual(timeout, 2)
            if request.get_method() == "GET":
                raise urllib.error.URLError("catalog temporarily unavailable")
            return FakeHTTPResponse(json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]}))

        provider = DeepSeekMentorProvider(api_key="test-secret", opener=opener, timeout=2)
        self.assertEqual(provider.discover_models()["status"], "PROVIDER_ERROR")
        result = provider.explain(context={"deterministic_advice": {"action": "WAIT"}}, memories=[], verification={"status": "NOT_REQUESTED"})
        self.assertEqual(result["status"], "READY")
        self.assertTrue(result["is_ai"])
        self.assertEqual(result["model"], "deepseek-v4-flash")
        self.assertEqual(result["structured_output"]["rule_action_reference"], "WAIT")

    def test_mimo_anthropic_response_is_structured_and_auditable(self):
        payload = {
            "summary": "仅解释 fixture 证据",
            "drivers": ["规则动作为等待"],
            "risks": ["证据可能过期"],
            "counter_evidence": ["没有实时账户"],
            "questions": ["是否需要人工复核"],
            "source_references": [],
            "confidence": "LOW",
            "uncertainty": "fixture only",
            "rule_action_reference": "WAIT",
        }
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return FakeHTTPResponse(json.dumps({"content": [{"type": "text", "text": json.dumps(payload)}]}))

        provider = MiMoMentorProvider(api_key="test-secret", base_url="https://mimo.test/anthropic", model="mimo-v2.5", timeout=2, opener=opener)
        result = provider.explain(context={"fixture": True}, memories=[{"id": 3, "content": "fixture memory"}], verification={"status": "NOT_REQUESTED"})
        self.assertEqual(result["status"], "READY")
        self.assertTrue(result["is_ai"])
        self.assertEqual(result["protocol"], "anthropic_messages_v1")
        self.assertEqual(result["structured_output"]["confidence"], "LOW")
        self.assertEqual(requests[0][0].get_method(), "POST")
        self.assertEqual(requests[0][0].get_header("X-api-key"), "test-secret")
        self.assertNotIn("test-secret", requests[0][0].data.decode("utf-8"))

    def test_multi_provider_prefers_deepseek_before_mimo(self):
        deepseek = DeepSeekMentorProvider(api_key="deepseek-secret", model="deepseek-v4-flash", opener=lambda *_args, **_kwargs: FakeHTTPResponse())
        mimo = MiMoMentorProvider(api_key="mimo-secret", base_url="https://mimo.test/anthropic", model="mimo-v2.5", opener=lambda *_args, **_kwargs: FakeHTTPResponse())
        chain = MultiMentorProvider(use_environment=False)
        chain.providers = (deepseek, mimo)
        status = chain.status()
        self.assertEqual(status["status"], "CONFIGURED_PENDING_CALL")
        self.assertEqual(status["selected_provider"], "deepseek")
        self.assertEqual(status["model"], "deepseek-v4-flash")
        self.assertFalse(status["is_ai"])
        self.assertIsNone(status["last_call_succeeded"])
        self.assertEqual(status["fallback_order"], ["deepseek", "mimo"])

    def test_mimo_configuration_is_not_reported_as_live_ai_health(self):
        provider = MiMoMentorProvider(api_key="mimo-secret", base_url="https://mimo.test/anthropic", model="mimo-v2.5")
        status = provider.status()
        self.assertEqual(status["status"], "CONFIGURED_PENDING_CALL")
        self.assertTrue(status["configured"])
        self.assertFalse(status["is_ai"])
        self.assertIsNone(status["last_call_succeeded"])
        self.assertTrue(status["fail_closed"])

    def test_deepseek_retries_timeout_once_and_audits_attempt_reasons(self):
        payload = {"summary": "fixture", "drivers": [], "risks": [], "counter_evidence": [], "questions": [], "source_references": [], "confidence": "LOW"}
        calls = []
        def opener(request, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                raise TimeoutError("fixture timeout")
            return FakeHTTPResponse(json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]}))
        provider = DeepSeekMentorProvider(api_key="fixture-key", model="deepseek-v4-flash", opener=opener)
        result = provider.explain(context={"fixture": True}, memories=[], verification={})
        self.assertEqual(provider.timeout, 45.0)
        self.assertEqual(len(calls), 2)
        self.assertEqual([item["reason"] for item in result["attempts"]], ["TIMEOUT", "SUCCESS"])

    def test_deepseek_does_not_retry_http_4xx(self):
        calls = []
        def opener(request, timeout):
            calls.append(1)
            return FakeHTTPResponse("{}", status=401)
        result = DeepSeekMentorProvider(api_key="fixture-key", model="deepseek-v4-flash", opener=opener).explain(context={}, memories=[], verification={})
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["reason_codes"], ["AI_HTTP_STATUS:401"])
        self.assertEqual(result["attempts"][0]["reason"], "HTTP_401")

    def test_mimo_openai_compatible_uses_bearer_and_json_object(self):
        payload = {"summary": "fixture", "drivers": [], "risks": [], "counter_evidence": [], "questions": [], "source_references": [], "confidence": "LOW"}
        requests = []
        def opener(request, timeout):
            requests.append(request)
            return FakeHTTPResponse(json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]}))
        provider = MiMoMentorProvider(api_key="fixture-key", base_url="https://mimo.test", model="mimo-v2.5", opener=opener)
        result = provider.explain(context={"fixture": True}, memories=[], verification={})
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["protocol"], "openai_compatible")
        self.assertEqual(requests[0].full_url, "https://mimo.test/v1/chat/completions")
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer fixture-key")
        self.assertEqual(json.loads(requests[0].data)["response_format"], {"type": "json_object"})

    def test_invalid_response_hash_does_not_contain_key_or_prompt(self):
        def opener(request, timeout):
            return FakeHTTPResponse(json.dumps({"choices": [{"message": {"content": "not-json"}}]}))
        result = DeepSeekMentorProvider(api_key="fixture-secret", model="deepseek-v4-flash", opener=opener).explain(context={"private_prompt": "do-not-log"}, memories=[], verification={})
        self.assertEqual(result["status"], "INVALID_RESPONSE")
        self.assertNotIn("fixture-secret", json.dumps(result))
        self.assertNotIn("do-not-log", json.dumps(result))
        self.assertRegex(result["response_hash"], r"^[0-9a-f]{64}$")

    def test_source_verifier_is_read_only_and_reports_reachability(self):
        calls = []

        def opener(request, timeout):
            calls.append((request.get_method(), request.full_url, timeout))
            return FakeHTTPResponse(b"verified public sample", status=200)

        verifier = PublicEvidenceVerifier(timeout=2, opener=opener)
        result = verifier.verify(["https://example.test/a", "not-a-url"], requested=True)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["healthy_count"], 1)
        self.assertEqual(calls[0][0], "GET")
        self.assertEqual(verifier.verify([], requested=False)["status"], "NOT_REQUESTED")

    def test_service_records_ai_audit_but_never_labels_template_as_ai(self):
        service = TradeCoachService(self.make_store().path, project_root=Path.cwd(), bootstrap=False)
        result = service.generate_ai_mentor(verify_sources=False)
        self.assertEqual(result["status"], "NOT_CONFIGURED")
        self.assertIsNotNone(result["ai_run_id"])
        self.assertEqual(service.store.latest_ai_run()["status"], "NOT_CONFIGURED")
        summary = service.summary()
        self.assertEqual(summary["ai"]["status"], "NOT_CONFIGURED")
        self.assertTrue(all(step["reasoning_kind"] == "DETERMINISTIC_RULES" for step in summary["deterministic_mentor_chain"]))
        self.assertTrue(all(item.get("reasoning_kind") == "DETERMINISTIC_RULES" for item in summary["advice"]["mentor_chain"]))

    def test_webhook_notification_adapter_is_real_or_explicitly_unconfigured(self):
        self.assertEqual(WebhookNotificationAdapter(url="").status()["status"], "NOT_CONFIGURED")
        calls = []

        def opener(request, timeout):
            calls.append((request.get_method(), request.full_url, timeout, request.data))
            return FakeHTTPResponse(b"ok", status=204)

        store = self.make_store()
        service = NotificationService(store, adapter=WebhookNotificationAdapter(url="https://notify.test/hook", timeout=2, opener=opener))
        result = service.send({"event_type": "TEST", "message": "manual verification"})
        self.assertEqual(result["status"], "DELIVERED")
        self.assertEqual(result["response_code"], 204)
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(store.notification_deliveries(1)[0]["status"], "DELIVERED")

    def test_qqbot_official_auth_fail_closed_and_secret_safe(self):
        calls = []
        secret, token = "SECRET_SENTINEL", "TOKEN_SENTINEL"
        def opener(request, timeout):
            calls.append(request)
            if len(calls) == 1:
                return FakeHTTPResponse(json.dumps({"access_token": token}), status=200)
            return FakeHTTPResponse(b"{}", status=204)
        adapter = QQBotNotificationAdapter(app_id="APP", app_secret=secret, openid="openid", opener=opener)
        result = adapter.send({"event": {"message": "safe reminder"}})
        self.assertEqual(result["status"], "DELIVERED")
        self.assertEqual(json.loads(calls[0].data), {"appId": "APP", "clientSecret": secret})
        self.assertEqual(calls[1].headers["Authorization"], "QQBot " + token)
        for value in (result, repr(result)):
            self.assertNotIn(secret, str(value)); self.assertNotIn(token, str(value))

    def test_qqbot_waiting_binding_and_http_failures(self):
        self.assertEqual(QQBotNotificationAdapter(app_id="APP", app_secret="S").status()["status"], "WAITING_TARGET_BINDING")
        for code, expected in ((401, "QQBOT_AUTH_FAILED"), (403, "QQBOT_AUTH_FAILED"), (429, "QQBOT_RATE_LIMITED")):
            def opener(request, timeout, code=code):
                if "getAppAccessToken" in request.full_url:
                    return FakeHTTPResponse(json.dumps({"access_token": "T"}), status=200)
                return FakeHTTPResponse(b"{}", status=code)
            result = QQBotNotificationAdapter(app_id="APP", app_secret="S", openid="O", opener=opener).send({"message": "x"})
            self.assertEqual(result["status"], "FAILED"); self.assertEqual(result["reason_codes"], [expected])

    def test_stale_fallback_does_not_hide_latest_failed_probe(self):
        store = self.make_store()
        old = datetime(2026, 8, 20, 15, tzinfo=CN_TZ)
        latest = datetime(2026, 8, 21, 15, tzinfo=CN_TZ)
        store.append_observation(MarketObservation("801050.SI", "TUSHARE_INDEX_DAILY local", old, old, 100, 101, 99, 100, None, 100, "READY", source_ref="fixture", raw_hash="sector-old"))
        store.append_observation(MarketObservation("801050.SI", "TUSHARE_INDEX_DAILY local", latest, latest, None, None, None, None, None, None, "MISSING", ("UPSTREAM_TIMEOUT",), source_ref="fixture", raw_hash="sector-missing"))
        state = next(item for item in build_instrument_states(store, now=datetime(2026, 8, 29, 15, tzinfo=CN_TZ)) if item["symbol"] == "801050.SI")
        self.assertEqual(state["primary"]["status"], "MISSING")
        self.assertEqual(state["selected"]["status"], "STALE")
        self.assertEqual(state["selected"]["close"], 100)
        self.assertEqual(state["reconciliation_status"], "STALE")

    def test_vps_fact_chain_audit_keeps_calendar_gate_and_publisher_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "economic_calendar_current.json").write_text(json.dumps({"meta": {"source": "cache", "last_success_at": "2026-08-29T10:00:00+08:00", "last_error_code": "FETCH_FAILED"}, "events": [{"id": 1}]}), encoding="utf-8")
            (root / "pit-macro-dry-run-r3.json").write_text(json.dumps({"status": "MISSING", "snapshot": {"missing_field_list": ["Demand"], "pit_errors": ["EVENT_CALENDAR_UNAVAILABLE"], "generated_at": "2026-08-29T16:35:00+08:00"}}), encoding="utf-8")
            (root / "remote_cron_live").write_text("35 16 * * 1-5 /usr/bin/python generate_pit_macro_snapshot.py && /usr/bin/python publish_vps_macro_risk_v1.py\n", encoding="utf-8")
            (root / "remote_current_publish_vps_macro_risk_v1.py").write_text("# captured approved publisher\n", encoding="utf-8")
            result = audit_vps_fact_chain(root, now=datetime(2026, 8, 29, 20, tzinfo=CN_TZ))
            self.assertEqual(result["status"], "MISSING")
            self.assertTrue(result["fail_closed"])
            self.assertFalse(result["live_ssh_verified"])
            self.assertEqual(result["cron"]["status"], "READY")
            self.assertEqual(result["risk_publisher"]["status"], "MISSING")
            self.assertIn("EVENT_CALENDAR_UNAVAILABLE", result["reason_codes"])
            self.assertIn("VPS_RISK_FACT_NOT_PUBLISHED", result["reason_codes"])


if __name__ == "__main__":
    unittest.main()
