"""Command-line entrypoint for local backtests and manual order records."""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime
from dataclasses import replace
from pathlib import Path

from .audit import AuditLog
from .data import CSVMarketData
from .day0_audit import Day0AuditJournal, make_day0_records
from .engine import BacktestEngine
from .manual import confirm_manual_order, create_manual_order
from .portfolio import PortfolioConfig
from .strategy import SMACrossover
from .forward_dashboard import serve
from .forward_probe import CN_TZ, ProbeRunner
from .forward_store import FoundationLedger, SnapshotStore
from .trade_coach import TradeCoachService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quant-lab", description="Quant-Lab v0.1 local-only research tools")
    sub = parser.add_subparsers(dest="command", required=True)
    backtest = sub.add_parser("backtest", help="run a local CSV backtest")
    backtest.add_argument("--data", required=True, type=Path)
    backtest.add_argument("--cash", type=float, default=100000.0)
    backtest.add_argument("--fast", type=int, default=5)
    backtest.add_argument("--slow", type=int, default=20)
    backtest.add_argument("--quantity", type=float, default=1.0)
    backtest.add_argument("--audit", type=Path, default=Path("audit/backtest.jsonl"))
    manual = sub.add_parser("manual-order", help="create and optionally confirm a manual order record")
    manual.add_argument("--symbol", required=True)
    manual.add_argument("--side", required=True, choices=("buy", "sell"))
    manual.add_argument("--quantity", required=True, type=float)
    manual.add_argument("--limit-price", type=float)
    manual.add_argument("--confirm", action="store_true", help="record human confirmation; still does not execute")
    manual.add_argument("--audit", type=Path, default=Path("audit/manual-orders.jsonl"))
    day0 = sub.add_parser("day0-audit", help="append a local Day-0 JoinQuant simulation audit pair")
    day0.add_argument("--decision-for", required=True, help="audited trading date, YYYY-MM-DD")
    day0.add_argument("--checked-at", required=True, help="timezone-aware check time, ISO-8601")
    day0.add_argument(
        "--hermes-status",
        default="MISSING",
        choices=("GREEN", "ORANGE", "RED", "MISSING", "STALE", "INVALID"),
        help="PIT Hermes result used to derive the fail-closed expectation",
    )
    day0.add_argument("--observed-json", type=Path, help="local JSON object keyed by the two strategy IDs")
    day0.add_argument("--evidence-ref", action="append", default=[], help="log/report reference; repeatable")
    day0.add_argument("--audit", type=Path, default=Path("audit/day0-joinquant.jsonl"))
    probe = sub.add_parser("probe", help="run one explicit read-only market-data probe")
    probe.add_argument("--data-root", type=Path, default=Path("data/forward_probe"))
    probe.add_argument("--db", type=Path, default=Path("data/forward_probe/quant_lab_foundation.sqlite3"))
    probe.add_argument("--max-age-seconds", type=int, default=900)
    probe.add_argument("--max-deviation-bps", type=float, default=50.0)
    probe.add_argument("--check-point", help="configured checkpoint label, e.g. 09:31; omitted means manual/unscheduled")
    manifest = sub.add_parser("manifest", help="generate an append-only daily probe manifest")
    manifest.add_argument("--data-root", type=Path, default=Path("data/forward_probe"))
    manifest.add_argument("--day", required=True, help="observed day YYYY-MM-DD")
    dashboard = sub.add_parser("dashboard", help="serve the read-only localhost probe dashboard")
    dashboard.add_argument("--db", type=Path, default=Path("data/forward_probe/quant_lab_foundation.sqlite3"))
    dashboard.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"))
    dashboard.add_argument("--port", type=int, default=8765)
    coach = sub.add_parser("trade-coach", help="run the local Personal Trade Coach (manual confirmation only)")
    coach.add_argument("--db", type=Path, default=Path("data/trade_coach/trade_coach.sqlite3"))
    coach.add_argument("--project-root", type=Path, default=Path.cwd())
    coach.add_argument("--refresh", action="store_true", help="explicitly fetch public real market data")
    coach.add_argument("--local-only", action="store_true", help="import local evidence without network")
    coach.add_argument("--serve", action="store_true", help="serve the localhost dashboard and API")
    coach.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"))
    coach.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "backtest":
        audit = AuditLog(args.audit)
        result = BacktestEngine(PortfolioConfig(initial_cash=args.cash), audit).run(CSVMarketData(args.data), SMACrossover(args.fast, args.slow, args.quantity))
        print(json.dumps({"initial_cash": result.initial_cash, "final_equity": result.final_equity, "fill_count": len(result.fills)}, ensure_ascii=False))
        return 0
    if args.command == "day0-audit":
        observed = {}
        if args.observed_json is not None:
            observed = json.loads(args.observed_json.read_text(encoding="utf-8"))
            if not isinstance(observed, dict):
                raise ValueError("--observed-json must contain an object keyed by strategy_id")
        records = make_day0_records(
            decision_for=args.decision_for,
            checked_at=args.checked_at,
            hermes_status=args.hermes_status,
            observed=observed,
            evidence_refs=tuple(args.evidence_ref),
        )
        journal = Day0AuditJournal(args.audit)
        for record in records:
            journal.append(record)
        print(json.dumps([record.to_dict() for record in records], ensure_ascii=False))
        return 0
    if args.command == "probe":
        store = SnapshotStore(args.data_root)
        ledger = FoundationLedger(args.db)
        ledger.seed_accounts()
        results = ProbeRunner(max_age_seconds=args.max_age_seconds, max_deviation_bps=args.max_deviation_bps).run()
        run_id = uuid.uuid4().hex
        ledger.append_probe_run(run_id, results[0][2].observed_at if results else datetime.now(CN_TZ), results, check_point=args.check_point)
        output = []
        for primary, backup, selected, primary_raw, backup_raw in results:
            refs = store.append(
                selected,
                primary_raw=primary_raw,
                backup_raw=backup_raw,
                primary_source=primary.source,
                backup_source=backup.source,
            )
            selected = replace(selected, raw_ref=refs["raw"])
            ledger.append_observation(selected)
            ledger.append_audit("market_probe", {"symbol": selected.symbol, "status": selected.status, "primary": primary.to_dict(), "backup": backup.to_dict()})
            output.append({"symbol": selected.symbol, "status": selected.status, "source": selected.source, "reason_codes": selected.reason_codes})
        manifest = store.generate_manifest(results[0][2].observed_at.date().isoformat()) if results else None
        ledger.close()
        print(json.dumps({"observations": output, "manifest": str(manifest) if manifest else None}, ensure_ascii=False))
        return 0
    if args.command == "manifest":
        target = SnapshotStore(args.data_root).generate_manifest(args.day)
        print(json.dumps({"manifest": str(target)}, ensure_ascii=False))
        return 0
    if args.command == "dashboard":
        serve(args.db, host=args.host, port=args.port)
        return 0
    if args.command == "trade-coach":
        service = TradeCoachService(args.db, project_root=args.project_root, bootstrap=True)
        if args.refresh or args.local_only:
            result = service.refresh(include_live=bool(args.refresh and not args.local_only))
            print(json.dumps(result, ensure_ascii=False, default=str))
            if args.serve:
                serve(
                    Path(args.project_root) / "data" / "forward_probe" / "quant_lab_foundation.sqlite3",
                    host=args.host,
                    port=args.port,
                    trade_coach_db=args.db,
                    trade_coach_project_root=args.project_root,
                )
            return 0
        if args.serve:
            serve(
                Path(args.project_root) / "data" / "forward_probe" / "quant_lab_foundation.sqlite3",
                host=args.host,
                port=args.port,
                trade_coach_db=args.db,
                trade_coach_project_root=args.project_root,
            )
            return 0
        print(json.dumps(service.summary(), ensure_ascii=False, default=str))
        return 0
    audit = AuditLog(args.audit)
    order = create_manual_order(symbol=args.symbol, side=args.side, quantity=args.quantity, limit_price=args.limit_price, audit=audit)
    if args.confirm:
        order = confirm_manual_order(order, audit=audit)
    print(json.dumps(order, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
