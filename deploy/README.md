# 私人 Tailscale 部署准备（未执行）

本目录只描述部署，不代表已连接 VPS、启动容器或开放端口。目标是个人使用、人工确认、纸面/手工执行；不接券商、不自动下单。

## 结构与边界

- `quant-lab` 容器内 Python API 仅绑定 `127.0.0.1:8765`，nginx 仅提供静态前端和同容器 API 反代。
- Compose 将唯一端口发布为宿主机 `127.0.0.1:${QUANT_LAB_PORT}`；默认没有公网监听。若使用 Tailscale，应在宿主机单独、人工审核 `tailscale serve` 将该本机端口映射到私网，禁止直接发布公网端口。
- SQLite、运行状态和日志位于命名 volume；源码只读，容器根文件系统只读。凭据只从未提交的 `.env` 注入，示例值为空。
- 不在容器中运行 probe、同步、交易或券商客户端；数据源探针必须通过人工确认后单独运行，并在上线前满足 freshness/provenance Gate。

## 首次部署前 Gate（在目标主机执行，本文未执行）

1. 审核镜像摘要、`docker compose config` 和 `scripts/audit_deployment.py` 输出；确认 `.env` 不在 Git 工作树。
2. 先创建数据/日志备份并验证可恢复，再构建镜像。容器健康检查只能证明 API 可响应，不能证明行情新鲜或策略可交易。
3. 运行只读数据源探针，要求每个允许标的具备时间戳、来源、原始快照引用和新鲜度；缺失/过期/冲突必须保持 `FAIL_CLOSED`，不得因 Gate 失败上线交易功能。
4. 确认 VPS 防火墙没有放行 8080；仅通过 Tailscale ACL/Serve 访问，并从另一台已授权设备验证。

## 运维（示例，需人工审核）

- 日志轮转：在宿主机对 Docker/container 日志及 `quant_lab_logs` volume 配置 `logrotate`，保留 14 天、压缩并限制权限；不要把日志提交到仓库。
- 备份：停止写入后备份 `quant_lab_data` volume（含 SQLite 的 `-wal`/`-shm`），记录时间和 SHA-256；恢复前保留旧 volume，恢复后运行健康检查和只读审计。
- 升级：固定镜像 digest，先备份，再 `docker compose up -d --no-deps quant-lab`；升级失败立即切回上一个 digest 并回读健康状态。不要使用 `latest`。
- 回滚：保留上一镜像和上一份 `.env`；回滚不删除数据 volume。任何数据迁移必须先复制到新 volume 并人工验收。

## QQ secrets 与风险事实（可选）

将 `deploy/.env.example` 复制为未提交的 `.env`，并在宿主机创建三个仅 owner 可读（Linux `chmod 600`）的文件：`qqbot_app_id`、`qqbot_app_secret`、`qqbot_openid`。Compose 以 Docker secrets 只读挂载到 `/run/secrets`；Linux 文件缺失或权限过宽会 fail-closed，API 不回显值，设置页显示部署托管且不可网页覆盖。Windows 仍使用 Credential Manager。

如需 VPS 风险事实，使用 `docker-compose.risk-facts.example.yml`，在未提交 `.env` 设置 `QUANT_LAB_VPS_FACT_HOST_PATH`，仅以只读方式挂载到固定路径 `/var/lib/quant-lab/vps-facts`。不得挂载 `/root` 或整库；路径缺失、过期、冲突均保持 fail-closed。
