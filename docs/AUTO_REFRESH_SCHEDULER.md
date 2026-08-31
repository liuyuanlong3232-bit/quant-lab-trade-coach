# 自动数据刷新调度

工作台常驻服务会以 `Asia/Shanghai` 运行只读数据刷新调度。固定检查点为
`09:35`、`11:25`、`13:35`、`14:50`、`15:10`，每个检查点只允许在随后
4 分钟内执行一次。容器重启后不会补跑已经错过的网络刷新。

## 交易日证据

调度不会把周一至周五直接当作交易日。只有固定 manifest 与其指向的内容寻址
CSV 同时存在并通过校验，当天 `is_open=1` 才允许联网：

- `data/trade_coach/source_cache/tushare_trade_calendar_manifest.json`
- `data/trade_coach/source_cache/tushare_trade_calendar.<hash>.csv`

CSV 必须含 `cal_date`（`YYYYMMDD` 或 `YYYY-MM-DD`）和 `is_open`（`0`/`1`）。
manifest 必须含：

```json
{
  "source": "TUSHARE",
  "calendar": "SSE",
  "path": "tushare_trade_calendar.<hash>.csv",
  "retrieved_at": "2026-08-30T12:00:00+08:00",
  "sha256": "CSV文件的SHA-256"
}
```

周末固定为 `CLOSED`。工作日若日历缺失、哈希不符、时间无时区或没有覆盖当天，
状态为 `UNKNOWN`，调度追加跳过审计但不会发起网络请求。交易所节假日由 Tushare
日历的 `is_open=0` 明确关闭。

## 审计和并发

`auto_refresh_slots` 保存不可重复的时点 claim；`auto_refresh_audit` 只追加
`CLAIMED`、`COMPLETE`、`SKIPPED` 或 `FAILED` 事件。进程在网络调用期间退出后，
同一时点也不会在重启后重新执行。手动刷新和自动刷新共享互斥锁；发生并发时自动
刷新记录 `AUTO_REFRESH_CONCURRENT_RUN` 并跳过。

调度只调用已有公开数据刷新和确定性分析重建。缺失或过期值仍保持
`MISSING`/`STALE`，不补值，不生成订单，不连接券商，也不触发自动交易。

## 安全更新

可显式运行：

```bash
python -m quant_lab.cli trade-calendar-update --project-root /var/lib/quant-lab
```

Token 优先从 `TUSHARE_TOKEN_FILE` 指向的受保护文件读取，也可从
`TUSHARE_TOKEN` 环境变量读取。Token、请求体和原始响应不会写入日志、manifest
或 SQLite 审计。更新至少覆盖未来370天；覆盖不完整、接口失败或哈希校验失败时，
固定 manifest 保持不变，旧版本继续可用。

常驻服务在日历缺失或无效时后台尝试一次初始化，并在每天15:20维护一次。维护动作
本身不是交易日判断，不会触发行情刷新；只有更新后的日历明确标记
`is_open=1`，行情调度才会放行。
