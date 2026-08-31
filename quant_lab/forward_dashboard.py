"""Read-only localhost Sprint 1A dashboard."""

from __future__ import annotations

import html
import json
import sqlite3
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .forward_gate import REQUIRED_SYMBOLS, gate_summary
from .forward_store import _local_path
from .trade_coach import TradeCoachService


def _ro_connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _status_class(status: object) -> str:
    return {"READY": "ready", "STALE": "stale", "MISSING": "bad", "CONFLICT": "bad"}.get(str(status), "bad")


def _cell(value: object) -> str:
    return html.escape("" if value is None else str(value))


def _row_value(row: sqlite3.Row, key: str, default: object = "[]") -> object:
    """Read optional provenance columns from pre-migration evidence safely."""
    return row[key] if key in row.keys() else default


def _display(value: object, *, empty: str = "N/A") -> str:
    return _cell(empty if value is None or value == "" else value)


def _latency(value: object) -> str:
    if value is None or value == "":
        return "N/A"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _cell(value)
    rendered = str(int(number)) if number.is_integer() else f"{number:.1f}"
    return f"{rendered} ms"


def _timestamp_html(value: object) -> str:
    """Render probe timestamps compactly while retaining the exact raw value."""
    raw = "" if value is None else str(value)
    if not raw:
        return "未运行"
    display = raw
    try:
        parsed = datetime.fromisoformat(raw)
        display = parsed.strftime("%Y-%m-%d %H:%M:%S")
        offset = parsed.strftime("%z")
        if len(offset) == 5:
            display += f" {offset[:3]}:{offset[3:]}"
    except (TypeError, ValueError):
        display = raw.replace("T", " ", 1)
    return f"<time class='timestamp-value' title='{_cell(raw)}'>{_cell(display)}</time>"


SYMBOL_LABELS = {
    "000426.XSHE": "兴业银锡（000426）",
    "000960.XSHE": "锡业股份（000960）",
    "AG0": "沪银连续（AG0）",
    "AU0": "沪金连续（AU0）",
    "SN0": "沪锡连续（SN0）",
    "SC0": "原油连续（SC0）",
}
SOURCE_LABELS = {
    "mootdx": "通达信",
    "tencent": "腾讯行情",
    "sina_futures": "新浪期货",
    "eastmoney_futures": "东方财富期货",
    "primary+backup": "主备源",
}
STATUS_LABELS = {
    "READY": "正常",
    "STALE": "行情过期",
    "MISSING": "数据缺失",
    "CONFLICT": "主备冲突",
    "PENDING": "待完成",
    "PASS": "通过",
}
REASON_LABELS = {
    "SOURCE_CONTINUOUS_ALIAS_UNVERIFIED": "连续合约映射待核验",
    "NO_PROBE_EVIDENCE": "尚无探针证据",
}


def _status_html(value: object) -> str:
    code = str(value)
    return f"{_cell(STATUS_LABELS.get(code, '未翻译状态'))} <small class='tech'>({_cell(code)})</small>"


def _source_html(value: object) -> str:
    code = str(value)
    return f"{_cell(SOURCE_LABELS.get(code, '未翻译来源'))} <small class='tech'>({_cell(code)})</small>"


def _symbol_html(symbol: str) -> str:
    label = SYMBOL_LABELS.get(symbol, f"未翻译标的（{symbol}）")
    # The Chinese label already contains the requested human-facing code;
    # avoid rendering the same code a second time as a technical suffix.
    return _cell(label)


def _codes(value: object) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        try:
            import json

            parsed = json.loads(value)
        except (ValueError, TypeError):
            return (value,)
        if isinstance(parsed, list):
            return tuple(str(code) for code in parsed)
        return (value,)
    if isinstance(value, (tuple, list)):
        return tuple(str(code) for code in value)
    return (str(value),)


def _reason_label(code: str) -> str:
    if code in REASON_LABELS:
        return REASON_LABELS[code]
    if "RemoteDisconnected" in code:
        return "远端主动断开"
    if "SOURCE_CONTINUOUS_ALIAS_UNVERIFIED" in code:
        return REASON_LABELS["SOURCE_CONTINUOUS_ALIAS_UNVERIFIED"]
    return f"未翻译技术原因（{code}）"


def _reason_html(value: object) -> str:
    codes = _codes(value)
    if not codes:
        return "无"
    labels = "；".join(_cell(_reason_label(code)) for code in codes)
    raw = ", ".join(codes)
    return f"{labels} <details><summary>技术详情</summary><code>{_cell(raw)}</code></details>"


def _value_status_html(value: object) -> str:
    code = str(value)
    if code in STATUS_LABELS:
        return _status_html(code)
    return _cell(value)


def _conclusion(latest: dict[str, sqlite3.Row]) -> str:
    stocks = [latest.get(symbol) for symbol in ("000426.XSHE", "000960.XSHE")]
    futures = [latest.get(symbol) for symbol in ("AG0", "AU0", "SN0", "SC0")]
    stock_primary = bool(stocks) and all(row is not None and row["primary_status"] == "READY" for row in stocks)
    stock_backup = bool(stocks) and all(row is not None and row["backup_status"] == "READY" for row in stocks)
    if stock_primary and stock_backup:
        stock_text = "股票双源正常"
    elif stock_backup:
        stock_text = "股票备源正常，主源仍缺失"
    elif stock_primary:
        stock_text = "股票主源正常，备源仍缺失"
    else:
        stock_text = "股票双源未完整"
    futures_backup = bool(futures) and all(row is not None and row["backup_status"] == "READY" for row in futures)
    futures_primary = bool(futures) and all(row is not None and row["primary_status"] == "READY" for row in futures)
    if futures_primary and futures_backup:
        future_text = "期货主备源正常"
    elif futures_primary:
        future_text = "期货主源正常，备源缺失，暂不能计入验收"
    elif futures_backup:
        future_text = "期货备源正常，主源缺失，暂不能计入验收"
    else:
        future_text = "期货双源未完整，暂不能计入验收"
    return f"{stock_text}；{future_text}。"


def probe_summary(db_path: str | Path) -> dict[str, object]:
    """Return a read-only JSON view of the existing probe evidence.

    This endpoint intentionally exposes observation evidence only. It does not
    calculate signals, account values, orders, or any strategy state.
    """
    path = _local_path(db_path)
    gate = gate_summary(path)
    latest: dict[str, sqlite3.Row] = {}
    last_probe: str | None = None
    latest_overall = "MISSING"
    db_integrity = "MISSING"
    wal = "UNKNOWN"
    if path.is_file():
        connection = _ro_connect(path)
        try:
            db_integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            wal = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).upper()
            rows = connection.execute("SELECT e.*,r.observed_at,r.overall_status FROM probe_evidence e JOIN probe_runs r ON r.run_id=e.run_id ORDER BY r.observed_at DESC,e.id DESC").fetchall()
            if rows:
                last_probe = str(rows[0]["observed_at"])
                latest_overall = str(rows[0]["overall_status"])
            for row in rows:
                latest.setdefault(str(row["symbol"]), row)
        except sqlite3.OperationalError:
            db_integrity = "MISSING_SCHEMA"
        finally:
            connection.close()

    def source_payload(row: sqlite3.Row | None, prefix: str) -> dict[str, object]:
        if row is None:
            return {"source": None, "status": "MISSING", "close": None, "exchange_time": None, "latency_ms": None, "reason_codes": ["NO_PROBE_EVIDENCE"]}
        reasons = _codes(_row_value(row, f"{prefix}_reason_codes"))
        return {
            "source": row[f"{prefix}_source"],
            "status": row[f"{prefix}_status"],
            "close": row[f"{prefix}_close"],
            "exchange_time": row[f"{prefix}_exchange_time"],
            "latency_ms": row[f"{prefix}_latency_ms"],
            "reason_codes": list(reasons),
        }

    instruments: list[dict[str, object]] = []
    for symbol in REQUIRED_SYMBOLS:
        row = latest.get(symbol)
        if row is None:
            instruments.append({
                "symbol": symbol,
                "label": SYMBOL_LABELS.get(symbol, symbol),
                "asset_class": None,
                "primary": source_payload(None, "primary"),
                "backup": source_payload(None, "backup"),
                "selected": {"source": None, "status": "MISSING", "close": None, "exchange_time": None, "latency_ms": None, "price_deviation_bps": None, "reason_codes": ["NO_PROBE_EVIDENCE"]},
            })
            continue
        instruments.append({
            "symbol": symbol,
            "label": SYMBOL_LABELS.get(symbol, symbol),
            "asset_class": row["asset_class"],
            "primary": source_payload(row, "primary"),
            "backup": source_payload(row, "backup"),
            "selected": {
                "source": row["selected_source"],
                "status": row["selected_status"],
                "close": row["selected_close"],
                "exchange_time": row["selected_exchange_time"],
                "latency_ms": row["selected_latency_ms"],
                "price_deviation_bps": row["price_deviation_bps"],
                "reason_codes": list(_codes(row["reason_codes"])),
            },
        })

    return {
        "schema_version": "quant_lab_probe_summary_v1",
        "read_only": True,
        "stage": "Sprint 1A",
        "gate": {
            "status": gate["status"],
            "progress": gate["progress"],
            "days": [{"day": day.day, "check_count": day.check_count, "passed": day.passed, "checkpoints": day.checkpoint_results} for day in gate["days"]],
        },
        "last_probe": last_probe,
        "overall_status": latest_overall,
        "database": {"exists": path.is_file(), "integrity": db_integrity, "wal": wal},
        "instruments": instruments,
        "capabilities": {"strategy": False, "paper_account": False, "orders": False},
    }


def render_dashboard(db_path: str | Path) -> str:
    path = _local_path(db_path)
    gate = gate_summary(path)
    latest: dict[str, sqlite3.Row] = {}
    last_probe = "未运行"
    latest_overall = "MISSING"
    db_integrity = "MISSING"
    wal = "UNKNOWN"
    if path.is_file():
        connection = _ro_connect(path)
        try:
            db_integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            wal = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).upper()
            rows = connection.execute("SELECT e.*,r.observed_at,r.overall_status FROM probe_evidence e JOIN probe_runs r ON r.run_id=e.run_id ORDER BY r.observed_at DESC,e.id DESC").fetchall()
            if rows:
                last_probe = str(rows[0]["observed_at"])
                latest_overall = str(rows[0]["overall_status"])
            for row in rows:
                latest.setdefault(str(row["symbol"]), row)
        except sqlite3.OperationalError:
            db_integrity = "MISSING_SCHEMA"
        finally:
            connection.close()

    cards = []
    for symbol in REQUIRED_SYMBOLS:
        row = latest.get(symbol)
        if row is None:
            cards.append(
                f"<article class='quote-card bad'><div class='quote-card__head'><div>"
                f"<span class='asset-kind'>未采集证据</span><h3>{_symbol_html(symbol)}</h3></div>"
                f"<span class='status-badge'>{_status_html('MISSING')}</span></div>"
                f"<div class='source-block'><div class='source-title'><span class='source-dot primary'></span>主源 <span class='source-name'>未记录</span> / {_status_html('MISSING')}</div>"
                f"<div class='quote-grid'><span>最新价 N/A</span><span>行情时间 N/A</span><span>响应耗时 N/A</span></div></div>"
                f"<div class='source-block'><div class='source-title'><span class='source-dot backup'></span>备源 <span class='source-name'>未记录</span> / {_status_html('MISSING')}</div>"
                f"<div class='quote-grid'><span>最新价 N/A</span><span>行情时间 N/A</span><span>响应耗时 N/A</span></div></div>"
                f"<div class='selected-quote'><div class='source-title'><span class='source-dot selected'></span><span class='field-label'>系统采用</span> 未记录 / {_status_html('MISSING')}</div>"
                f"<div class='quote-grid'><span>最新价 N/A</span><span>主备偏差 N/A bps</span><span>响应耗时 N/A</span><span>行情时间 N/A</span></div></div>"
                f"<div class='reason-line'><span class='field-label'>故障原因</span> {_reason_html(('NO_PROBE_EVIDENCE',))}</div></article>"
            )
            continue
        status = row["selected_status"]
        primary_reasons = _row_value(row, "primary_reason_codes")
        backup_reasons = _row_value(row, "backup_reason_codes")
        cards.append(
            f"<article class='quote-card {_status_class(status)}'><div class='quote-card__head'><div>"
            f"<span class='asset-kind'>{_cell(row['asset_class'])}</span><h3>{_symbol_html(symbol)}</h3></div>"
            f"<span class='status-badge'>{_status_html(status)}</span></div>"
            f"<div class='source-block'><div class='source-title'><span class='source-dot primary'></span>主源 <span class='source-name'>{_source_html(row['primary_source'])}</span> / {_status_html(row['primary_status'])}</div>"
            f"<div class='quote-grid'><span>最新价 {_display(row['primary_close'])}</span><span>行情时间 {_display(row['primary_exchange_time'])}</span><span>响应耗时 {_latency(row['primary_latency_ms'])}</span></div>"
            f"<div class='reason-line'><span class='field-label'>主源故障原因</span> {_reason_html(primary_reasons)}</div></div>"
            f"<div class='source-block'><div class='source-title'><span class='source-dot backup'></span>备源 <span class='source-name'>{_source_html(row['backup_source'])}</span> / {_status_html(row['backup_status'])}</div>"
            f"<div class='quote-grid'><span>最新价 {_display(row['backup_close'])}</span><span>行情时间 {_display(row['backup_exchange_time'])}</span><span>响应耗时 {_latency(row['backup_latency_ms'])}</span></div>"
            f"<div class='reason-line'><span class='field-label'>备源故障原因</span> {_reason_html(backup_reasons)}</div></div>"
            f"<div class='selected-quote'><div class='source-title'><span class='source-dot selected'></span><span class='field-label'>系统采用</span> {_source_html(row['selected_source'])} / {_status_html(status)}</div>"
            f"<div class='quote-grid'><span>最新价 {_display(row['selected_close'])}</span><span>主备偏差 {_display(row['price_deviation_bps'])} bps</span><span>响应耗时 {_latency(row['selected_latency_ms'])}</span><span>行情时间 {_display(row['selected_exchange_time'])}</span></div></div>"
            f"<div class='reason-line reason-line--overall'><span class='field-label'>系统故障原因</span> {_reason_html(row['reason_codes'])}</div></article>"
        )

    day_rows = []
    for day in gate["days"]:
        checkpoints = ", ".join(f"{_cell(name)}：{_status_html('PASS' if passed else 'PENDING')}" for name, passed in day.checkpoint_results.items())
        day_rows.append(f"<tr><td>{_cell(day.day)}</td><td>{day.check_count}</td><td>{_status_html('PASS' if day.passed else 'PENDING')}</td><td>{checkpoints}</td></tr>")
    if not day_rows:
        day_rows.append("<tr><td colspan='4'>尚无完整探针日</td></tr>")
    overall = _status_html(latest_overall)
    gate_status = _status_html(gate["status"])
    integrity = "正常 <small class='tech'>(ok)</small>" if db_integrity.lower() == "ok" else f"{_cell(db_integrity)}"
    wal_status = f"写入保护 <small class='tech'>({_cell(wal)})</small>"

    template = """<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta http-equiv='refresh' content='10'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Quant-Lab · Sprint 1A 只读探针</title><style>
:root{color-scheme:dark;--bg:#080d18;--panel:#101827;--panel-2:#141f31;--line:#26354b;--text:#edf3ff;--muted:#8c9bb2;--cyan:#57d6e8;--green:#45d39c;--amber:#f2bc61;--red:#ff6e7d;--shadow:0 16px 45px rgba(0,0,0,.22)}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 12% -10%,#182a47 0,#080d18 42rem),var(--bg);color:var(--text);font:14px/1.6 Inter,"Microsoft YaHei",system-ui,sans-serif;letter-spacing:.01em}main{max-width:1500px;margin:0 auto;padding:34px clamp(16px,3vw,48px) 56px}.hero{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;margin-bottom:24px}.eyebrow{color:var(--cyan);font-size:11px;font-weight:800;letter-spacing:.18em;text-transform:uppercase}.hero h1{font-size:clamp(27px,3vw,42px);line-height:1.15;margin:8px 0 8px;letter-spacing:-.04em}.hero p{color:var(--muted);margin:0}.hero-meta{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:8px}.tag{border:1px solid #31516e;border-radius:999px;padding:5px 11px;color:#b7eaf1;background:#102a3a;white-space:nowrap;font-size:12px}.notice,.conclusion,.metric,.quote-card,.table-shell,.module{background:linear-gradient(145deg,rgba(20,31,49,.96),rgba(12,19,32,.96));border:1px solid var(--line);box-shadow:var(--shadow);border-radius:14px}.notice{border-left:3px solid var(--amber);padding:13px 16px;color:#f5d69e;margin-bottom:14px}.notice strong{color:#fff}.conclusion{border-left:3px solid var(--cyan);padding:14px 16px;margin-bottom:22px}.conclusion-label{display:block;color:var(--muted);font-size:11px;letter-spacing:.13em;text-transform:uppercase;margin-bottom:3px}.dashboard-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin-bottom:30px}.metric{padding:16px;min-height:110px}.metric-label{color:var(--muted);font-size:12px;display:flex;justify-content:space-between;gap:8px}.metric-value{display:block;font-size:25px;font-weight:750;line-height:1.25;margin-top:11px;color:#fff;overflow-wrap:anywhere}.metric-value .tech{font-size:11px}.timestamp-value{font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.gate-value{color:var(--cyan)}.section-heading{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin:28px 0 12px}.section-heading h2{font-size:20px;margin:0;letter-spacing:-.02em}.section-heading p{color:var(--muted);font-size:12px;margin:0}.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.quote-card{padding:18px;border-left:3px solid var(--line);min-width:0}.quote-card.ready{border-left-color:var(--green)}.quote-card.stale{border-left-color:var(--amber)}.quote-card.bad{border-left-color:var(--red)}.quote-card__head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:15px}.asset-kind{color:var(--muted);font-size:10px;letter-spacing:.12em;text-transform:uppercase}.quote-card h3{font-size:19px;line-height:1.25;margin:3px 0 0}.status-badge{font-size:12px;white-space:nowrap}.ready .status-badge,.ready .pill{color:var(--green)}.stale .status-badge,.stale .pill{color:var(--amber)}.bad .status-badge,.bad .pill{color:var(--red)}.source-block{border-top:1px solid var(--line);padding:12px 0 10px}.source-title{font-size:13px;font-weight:650;display:flex;align-items:center;gap:6px;flex-wrap:wrap}.source-name{font-weight:500;color:#c9d7eb}.source-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--muted);margin-right:2px}.source-dot.primary{background:var(--cyan)}.source-dot.backup{background:#9b8cff}.source-dot.selected{background:var(--green)}.quote-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px 12px;color:#dbe5f5;margin-top:8px;font-size:12px}.selected-quote{border:1px solid #2d5261;background:rgba(27,76,86,.18);border-radius:10px;padding:12px;margin-top:7px}.selected-quote .quote-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.field-label{color:var(--muted);font-size:11px}.reason-line{color:#becadd;font-size:12px;margin-top:8px;overflow-wrap:anywhere}.reason-line .field-label{margin-right:4px}.reason-line--overall{border-top:1px solid var(--line);padding-top:11px}.tech{font-size:.78em;color:#71839d;font-weight:400}details{display:inline-block;margin-left:4px}summary{cursor:pointer;color:#7791b3;font-size:11px}code{white-space:pre-wrap;overflow-wrap:anywhere;color:#ffabb5}.table-shell{overflow-x:auto}.table-shell table{border-collapse:collapse;width:100%;min-width:620px}.table-shell th,.table-shell td{border-bottom:1px solid var(--line);padding:13px 15px;text-align:left;vertical-align:top}.table-shell th{color:var(--muted);font-size:11px;font-weight:650;letter-spacing:.08em;text-transform:uppercase}.table-shell tr:last-child td{border-bottom:0}.table-shell .pill{font-weight:650}.roadmap{margin-top:30px}.modules{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.module{padding:16px;opacity:.72;border-style:dashed}.module-head{display:flex;justify-content:space-between;gap:12px}.module h3{margin:0;font-size:15px}.module-state{color:var(--muted);font-size:11px}.module p{margin:8px 0 0;color:var(--muted);font-size:12px}.footer-note{color:var(--muted);font-size:11px;margin-top:28px}@media(max-width:900px){.dashboard-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.cards{grid-template-columns:1fr}}@media(max-width:580px){main{padding:22px 12px 40px}.hero{display:block}.hero-meta{justify-content:flex-start;margin-top:16px}.dashboard-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.metric{padding:12px;min-height:96px}.metric--timestamp{grid-column:1/-1}.metric-value{font-size:20px;margin-top:8px}.quote-card{padding:14px}.quote-grid{grid-template-columns:1fr 1fr}.selected-quote .quote-grid{grid-template-columns:1fr}.modules{grid-template-columns:1fr}.section-heading{display:block}.section-heading p{margin-top:3px}}</style></head><body><main>
<header class='hero'><div><div class='eyebrow'>QUANT-LAB / SPRINT 1A</div><h1>本地实时行情探针</h1><p>双源行情证据 · 数据验收 · 只读观测台</p></div><div class='hero-meta'><span class='tag'>阶段 1A · 进行中</span><span class='tag'>LOCALHOST ONLY</span><span class='tag'>10 秒刷新</span></div></header>
<div class='notice'><strong>只读行情探针</strong>：不生成信号、不创建订单、不执行交易。期货仅作联动因子，连续合约映射未自动推断。</div><div class='conclusion'><span class='conclusion-label'>当前读数 / 结论</span>__CONCLUSION__</div>
<section class='dashboard-grid'><div class='metric'><div class='metric-label'>五日数据验收 Gate <span>实际 Gate · 5 个检查点</span></div><strong class='metric-value gate-value'>__PROGRESS__</strong><span class='metric-label'>状态：__GATE_STATUS__</span></div><div class='metric metric--timestamp'><div class='metric-label'>最后探针 <span>北京时间</span></div><strong class='metric-value'>__LAST_PROBE__</strong></div><div class='metric'><div class='metric-label'>整体状态 <span>最新运行</span></div><strong class='metric-value'>__OVERALL__</strong></div><div class='metric'><div class='metric-label'>数据库完整性 <span>只读检查</span></div><strong class='metric-value'>__INTEGRITY__</strong></div><div class='metric'><div class='metric-label'>WAL 写入保护 <span>SQLite</span></div><strong class='metric-value'>__WAL__</strong></div></section>
<div class='section-heading'><h2>六标的 · 最新证据</h2><p>主源 / 备源 / 系统采用源 · 价格、时间、延迟与偏差</p></div><section class='cards'>__CARDS__</section>
<div class='section-heading'><h2>每日检查点</h2><p>必须完成 09:31 · 10:00 · 13:30 · 14:50 · 15:05</p></div><div class='table-shell'><table><thead><tr><th>交易日</th><th>检查次数</th><th>是否合格</th><th>检查点</th></tr></thead><tbody>__DAYS__</tbody></table></div>
<section class='roadmap'><div class='section-heading'><h2>策略与模拟账户</h2><p>能力边界 · 当前版本不启用</p></div><div class='modules'><article class='module'><div class='module-head'><h3>策略执行 / 信号</h3><span class='module-state'>未启用 · 预留</span></div><p>仅保留展示位置；当前探针不会计算策略信号，也不会触发任何动作。</p></article><article class='module'><div class='module-head'><h3>模拟账户 / 订单</h3><span class='module-state'>未启用 · 预留</span></div><p>当前版本不连接券商、不创建订单、不记录成交；此区域不代表已有账户能力。</p></article></div></section><p class='footer-note'>本页面由本地 Python 服务生成，绑定 127.0.0.1:8765；所有状态均来自当前 SQLite 证据，不对缺失数据作推断。</p></main></body></html>"""
    return (template.replace("__PROGRESS__", _cell(gate["progress"])).replace("__GATE_STATUS__", gate_status).replace("__LAST_PROBE__", _timestamp_html(last_probe)).replace("__OVERALL__", overall).replace("__INTEGRITY__", integrity).replace("__WAL__", wal_status).replace("__CONCLUSION__", _conclusion(latest)).replace("__CARDS__", "".join(cards)).replace("__DAYS__", "".join(day_rows)))


def make_server(
    db_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    trade_coach_db: str | Path | None = None,
    trade_coach_project_root: str | Path | None = None,
) -> ThreadingHTTPServer:
    if host.lower() not in {"127.0.0.1", "localhost"}:
        raise ValueError("dashboard must bind only to 127.0.0.1 or localhost")
    database = _local_path(db_path)
    # The Personal Trade Coach is an additive product layer.  It receives the
    # existing local forward database as evidence, but writes only to its own
    # ``data/trade_coach`` SQLite file and never mutates the Sprint 1 tables.
    if trade_coach_db is None:
        trade_service = TradeCoachService.for_forward_database(database)
    else:
        trade_service = TradeCoachService(
            trade_coach_db,
            project_root=trade_coach_project_root or database.parent.parent.parent,
            bootstrap=True,
        )

    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, value: object) -> None:
            payload = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/api/trade-coach"):
                status, value = trade_service.handle("GET", self.path)
                self._json(status, value)
                return
            if self.path in {"/api/probe-summary", "/api/probe-summary/"}:
                self._json(200, probe_summary(database))
                return
            if self.path not in {"/", "/index.html", "/diagnostic"}:
                self.send_error(404)
                return
            payload = render_dashboard(database).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:  # noqa: N802
            if not self.path.startswith("/api/trade-coach"):
                self.send_error(404)
                return
            try:
                if self.path.startswith("/api/trade-coach/qqbot/settings"):
                    host = (self.headers.get("Host") or "").split(":", 1)[0].lower()
                    origin = self.headers.get("Origin")
                    peer = str(self.client_address[0]).strip("[]")
                    valid_origin = not origin and peer in {"127.0.0.1", "::1", "localhost"}
                    if origin:
                        parsed_origin = urllib.parse.urlparse(origin)
                        valid_origin = parsed_origin.scheme == "http" and parsed_origin.hostname in {"127.0.0.1", "localhost"}
                    if host not in {"127.0.0.1", "localhost"} or not valid_origin:
                        self._json(403, {"error": "LOCAL_ORIGIN_REQUIRED"})
                        return
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > (64 * 1024 if self.path.startswith("/api/trade-coach/qqbot/settings") else 2 * 1024 * 1024):
                    raise ValueError("request body too large")
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                if not isinstance(body, dict):
                    raise ValueError("request body must be a JSON object")
                status, value = trade_service.handle("POST", self.path, body)
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError, OSError, PermissionError) as exc:
                status, value = 400, {"error": type(exc).__name__, "message": str(exc), "fail_closed": True}
            self._json(status, value)

        def do_DELETE(self) -> None:  # noqa: N802
            if self.path != "/api/trade-coach/qqbot/binding":
                self.send_error(404); return
            host_name = (self.headers.get("Host") or "").split(":", 1)[0].lower()
            origin = self.headers.get("Origin")
            peer = str(self.client_address[0]).strip("[]")
            valid = (not origin and peer in {"127.0.0.1", "::1", "localhost"})
            if origin:
                parsed = urllib.parse.urlparse(origin)
                valid = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
            if host_name not in {"127.0.0.1", "localhost"} or not valid:
                self._json(403, {"error": "LOCAL_ORIGIN_REQUIRED"}); return
            try:
                status, value = trade_service.handle("DELETE", self.path)
            except (ValueError, OSError, PermissionError) as exc:
                status, value = 400, {"error": type(exc).__name__, "message": str(exc), "fail_closed": True}
            self._json(status, value)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    # Gateway lifetime follows the dashboard lifetime; credentials are checked
    # by the service and no network task is created when they are absent.
    trade_service.start_qqbot_gateway()
    trade_service.start_auto_refresh()
    original_close = server.server_close
    def close_with_gateway() -> None:
        trade_service.stop_auto_refresh()
        trade_service.stop_qqbot_gateway()
        original_close()
    server.server_close = close_with_gateway  # type: ignore[method-assign]
    return server


def serve(
    db_path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    trade_coach_db: str | Path | None = None,
    trade_coach_project_root: str | Path | None = None,
) -> None:
    server = make_server(
        db_path,
        host,
        port,
        trade_coach_db=trade_coach_db,
        trade_coach_project_root=trade_coach_project_root,
    )
    print(f"Quant-Lab read-only dashboard: http://{host}:{port}/")
    try:
        server.serve_forever()
    finally:
        server.server_close()
