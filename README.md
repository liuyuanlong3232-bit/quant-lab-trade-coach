# Quant-Lab Personal Trade Coach

这是个人使用的脱敏源码基线：本地优先、只读事实、人工确认、禁止自动下单。默认 API 和前端只监听 `127.0.0.1`；未来若迁移到 VPS，应通过 Tailscale 私网访问，并单独审核部署配置。

## 本地运行

1. 使用 Python 3.10+ 创建虚拟环境并安装 `pip install -e .`。
2. 在 `frontend/` 执行 `pnpm install --frozen-lockfile`。
3. 执行 `pwsh -File scripts/start-local.ps1`。
4. 浏览器打开 `http://127.0.0.1:5173`。

启动器不会安装软件、连接 VPS、连接券商或执行交易。行情刷新和 AI 解释只能由明确的人工操作触发；缺失、过期或冲突事实保持 fail-closed。

## 数据与凭据边界

运行时数据、SQLite、缓存、日志、账户资料、OpenID、QQ Bot 密钥和任何 `.env` 均不属于源码基线，也不得提交。`.env.example` 仅保留空占位符。QQ Bot 密钥应使用操作系统凭据存储；VPS 同步脚本不在本基线中。

## 未来 VPS 边界

迁移前需单独验证网络探针、数据新鲜度、备份和 Tailscale 访问控制。VPS 只提供经审计的只读事实导出；本工作台不写 VPS、不接券商、不自动下单。

