# 运维与排障

## 健康检查

- 接口：`GET /healthz`
- 返回体：`{"ok": <bool>, "data_plane_running": <bool>, "ai_node_running": <bool>}`
- 默认行为：要求数据面可用时才返回健康

如果你只启动面板而不启动数据面：

- 设置 `PANEL_HEALTH_REQUIRES_XRAY=0`

`ai_node_running` 反映 AI 节点远端 Socket 可达性，不代表 REALITY 凭据匹配或实际 ChatGPT 请求成功。

## Prometheus 监控（`/metrics`）

面板把已采集的状态以 Prometheus 文本格式暴露在 `GET /metrics`，可直接接入 Prometheus + Grafana。

- 鉴权：必须设置 `METRICS_TOKEN`。
  - 未设置时端点返回 `404`（默认不对外开放）。
  - 设置后需带请求头 `Authorization: X <METRICS_TOKEN>`，否则返回 `401`。
- 可选 `METRICS_DP_TTL`（默认 `30` 秒）：缓存数据面存活检测和 AI Xray 指标读取，避免高频/并发抓取叠加 I/O。
- 抓取路径严格只读、不触发流量同步与探针，可放心按 15–30s 抓取。

暴露的指标（前缀 `xray_panel_`）：

- 业务：`port_traffic_bytes_total`(counter, `port`/`note`/`direction`)、`port_connections_total`(counter)、`ports_total`/`ports_enabled`/`ports_active`/`ports_expired`/`ports_quota`/`ports_disabled`(gauge)
- 存活/可用性：`up`、`port_reachable`(gauge, 来自 TCP 探针)、`port_probe_timestamp_seconds`
- 数据面：`data_plane_configured`/`data_plane_running`(gauge, 带 `mode` 标签)
- AI 节点：`ai_node_configured`/`ai_node_running`(gauge, 带 `mode` 标签)、`ai_node_metrics_available`(gauge)、`ai_node_traffic_bytes_total` 和 `ai_node_egress_bytes_total`(counter, `direction` 标签)
- AI 域名/端口：`ai_destination_log_available`、`ai_destination_window_seconds`、`ai_destination_requests`、`ai_destination_requests_per_second`、`ai_destination_last_seen_timestamp_seconds`（`domain`/`port`/`network` 标签，仅展开最近窗口 Top 50）和 `ai_destination_other_requests`
- DNS 故障切换：`dns_failover_enabled`、`dns_failover_target_info`、`dns_failover_last_probe_healthy`、`dns_failover_consecutive_failures`/`_successes`、`dns_failover_peak_window_active`
- AI 路由：`ai_domains_total`、`ai_domain_hits_total`、`ai_domains_last_update_timestamp_seconds`

> `traffic`/`connections` 为 counter，但“重置流量并启用”/配额恢复会清零累计值——这是合法的 counter reset，`rate()`/`increase()` 能正确处理。

- 本机备用 AI 与控制面共享 Node Exporter/cAdvisor；业务端口仍为 `27166`，不承担监控流量。AI Xray metrics 只监听 `redacted-ip-007:31097`，由面板聚合后进入 Prometheus。远端 AI 模式才需要单独配置 AI 主机的监控 target。
- 控制面 cAdvisor 监听 `redacted-ip-007:18081`，用于采集 `xray-ai-node` 容器级 CPU、内存和网络总量；该总量不能替代 Xray 入站/出站业务计数。
- AI 域名/端口指标来自本机 `ai-access.log` 的 `accepted` 记录，默认聚合最近 10 分钟请求量；access log 没有按目标拆分的字节数，不应把请求量指标解释为 per-domain 字节量。
- 高流量查询：`topk(20, xray_panel_ai_destination_requests)`；高请求速率查询：`topk(20, xray_panel_ai_destination_requests_per_second)`。
- 控制面 Prometheus 的普通数据面和控制面 targets 应显示 `up`；若远端 AI target 出现 `timeout` 或 `connection refused`，再检查远端 AI 节点的 exporter 容器和端口监听。

AI 路由状态至少应同时查看：

- `ai_candidates`：主、备候选及各自探测结果；
- `manual_mode`：`auto`、`primary`、`backup` 或 `forced_fallback`；
- `route_status`：例如 `applied`、`fallback_to_primary`、`manual_target_unreachable`；
- `ai_target.selected_index`：实际写入 `ai_proxy` 的候选。


Prometheus `scrape_config` 示例：

```yaml
scrape_configs:
  - job_name: xray-panel
    metrics_path: /metrics
    scheme: https
    authorization: { type: Bearer, credentials: "${METRICS_TOKEN}" }
    static_configs: [{ targets: ["panel.example.com"], labels: { role: control_plane } }]
  - job_name: node
    static_configs:
      - { targets: ["panel-host:9100"],     labels: { host: panel } }
      - { targets: ["dataplane-host:9100"],  labels: { host: dataplane } }
```

## 管理后台「监控」标签（内嵌 Grafana）

管理后台新增「监控」标签，把 Grafana 的单图（`d-solo`）以 iframe 内嵌进来，让管理员无需单独登录 Grafana 即可看到主机系统资源（CPU/内存/磁盘/网络/负载/Swap）与每端口流量/连接速率。其余总览/端口/商务/DNS 等数据仍由面板自身（SQLite）提供，不受影响。

启用步骤：

1. 启动 `monitoring/` 监控栈（Prometheus + Grafana + node_exporter）。其中 Grafana 已开启匿名只读（`GF_AUTH_ANONYMOUS_ENABLED=true` + `Viewer`）与内嵌（`GF_SECURITY_ALLOW_EMBEDDING=true`），并通过 provisioning 自动加载内嵌专用 dashboard `monitoring/grafana/dashboards/xray-observability.json`（UID `xray-observability`，带显式 panel id）。
2. 给面板设置 `GRAFANA_PUBLIC_URL=https://xray.zrhe2016.cc/grafana/`；该路径由 Cloudflare Access Email OTP 保护。
3. Grafana 使用 `GF_SERVER_ROOT_URL=https://xray.zrhe2016.cc/grafana/` 和 `GF_SERVER_SERVE_FROM_SUB_PATH=true`，重新启动监控栈后访问 `/grafana/`。
4. 重新部署包含 `app/static/` 发布资源的面板镜像，登录后台点「监控」即可。顶部可切换 1h/6h/24h 时间范围。

> ✅ **当前入口**：Grafana 通过 `https://xray.zrhe2016.cc/grafana/` 访问，Cloudflare Access 是公网认证边界；Grafana 的 `3001` 仅供本机 Nginx 反代使用。Cloudflare Access 邮箱会由 Nginx 转为 Grafana Auth Proxy 用户标识，认证后不再显示 Grafana 登录页。

> 前置项：要看**数据面（DMIT `redacted-ip-011`）**的系统资源，需在该机部署一份 node_exporter，并在 `monitoring/prometheus/prometheus.yml` 取消 `job_name: node` 下 DMIT target 的注释后 reload；否则「监控」里的主机指标只反映面板主机。

### 监控栈启停

监控栈由 `monitoring/docker-compose.monitoring.yml` 定义，包括 node_exporter、cAdvisor、Prometheus
和 Grafana；Prometheus 配置及 Grafana provisioning/dashboard 也全部位于 `monitoring/`。

```bash
cd monitoring
docker compose -f docker-compose.monitoring.yml up -d
docker compose -f docker-compose.monitoring.yml ps
docker compose -f docker-compose.monitoring.yml down  # 保留数据卷
```

修改 `prometheus.yml` 后可热加载：

```bash
curl -X POST http://redacted-ip-007:9090/-/reload
```

访问入口：Grafana 使用 `https://xray.zrhe2016.cc/grafana/`，Prometheus 是 `redacted-ip-007:9090`。Grafana 管理员密码来自
`monitoring/.env`。公网访问 Grafana 前必须通过 Cloudflare Access；不要直接暴露 `3001`、`9090` 或 node_exporter 的 `:9100`。

### Fluent Bit 日志采集

日志采集由三类独立组件组成：日志中心主机上的 Loki、三台 Docker 主机上的 Fluent Bit Agent，以及控制面上的 Grafana。Agent 不修改业务容器的 logging driver，而是读取 Docker JSON 日志、Xray `error.log` 和 allowlist 内的 systemd 错误日志，通过 Tailscale 发送到远端 Loki。

```bash
# 日志中心
cd monitoring/loki
docker compose up -d

# 每台控制面/普通数据面/AI 数据面主机
cd ../fluent-bit
docker compose -f docker-compose.agent.yml up -d

# 控制面的 Grafana/Prometheus
cd ..
docker compose -f docker-compose.monitoring.yml up -d prometheus grafana
```

Grafana Explore 中使用 `{job="platform-logs"}` 查询。完整边界、Tailscale ACL、LogQL 示例和排障命令见 [Fluent Bit 日志采集](logging-fluent-bit.md)。

当前三节点部署状态和生产路径见 [Fluent Bit 日志采集·当前生产部署](logging-fluent-bit.md#当前生产部署)。控制面业务日志可直接查询：

```logql
{job="platform-logs",node_role="control_plane",category="business"} | json
```

## 流量与连接统计

当前统计链路拆成两部分：

- 连接数来自 `app/xray/logs/access.log`
  - 只统计 `panel-<listen_port>` inbound tag
- 字节流量来自 Xray API `statsquery`
  - 查询模式为 `inbound>>>panel-`
  - 每次拉取后会执行 `-reset`

因此：

- `total_connections` 依赖访问日志增量同步
- `total_bytes_sent` / `total_bytes_received` 依赖 Xray API 周期采样
- 流量上限判断使用上行加下行累计值

## 自动维护规则

- 非商业端口到期后自动删除；关联客户或商业订阅的端口到期后自动停用，保留记录和订阅凭据供续费
- 达到流量上限的端口会自动停用
- “重置流量并启用”会清零累计流量与当日流量，但不会清零连接数

端口变更只有在配置校验、所需节点重载和数据库提交成功后才完成。重载失败（包括返回失败状态）
或数据库提交失败时，会回滚数据库并恢复原配置文件；已尝试重载的数据面会再次加载原配置。
如果数据面回滚同步或重载也失败，记录 `node.data_plane.rollback_failed` 事件，需要检查节点实际运行状态。

## 灾备归档与上传

- `xray-routing-panel-db-backup` 默认每天 `03:00 UTC` 备份一次 `panel.db`，并生成一个包含配置文件的灾备归档
- 本地 `.db` 和 `tar.gz` 备份文件均落在 `./backups`
- 额外文件由 `DB_BACKUP_EXTRA_PATHS` 指定；Compose 默认包含 `app/xray/.env`、运行时/报告、`data/uploads` 和部署脚本
- Compose 还会在打包前通过内网直连 SSH 以只读方式采集普通数据面（`root@100.116.187.106:22`）；本机 AI 备用的 `.env` 与 `config-ai-node.json` 已在控制面运行时目录中，随 `config/` 归档
- 每个归档都包含 `node-recovery-manifest.json`，任务还会生成 `node-recovery-status.json`，明确标记 `recoveryReady`
- 远端采集默认是非必需的：普通数据面失联不会丢弃控制面归档；要把普通数据面配置作为任务门禁，设置 `DB_BACKUP_SSH_COLLECTION_REQUIRED=1`
- 当 `DB_BACKUP_R2_ENABLED=1` 时，归档成功后会继续调用 `R2 灾备上传`
- R2 对象 key 默认包含日期、归档名称和 SHA-256 前缀；对象保留由 Cloudflare R2 生命周期策略控制，不承担快速恢复

上传链路依赖：

- `DB_BACKUP_R2_ENDPOINT`、`DB_BACKUP_R2_BUCKET`
- `DB_BACKUP_R2_ACCESS_KEY_ID`、`DB_BACKUP_R2_SECRET_ACCESS_KEY`
- 独立保存的 `DB_BACKUP_ENCRYPTION_PASSWORD`

排查建议：

- 查看日志：`docker compose logs -f xray-routing-panel-db-backup`
- 确认最新本地备份已生成到 `./backups`
- 确认对应的 `*-disaster-*.tar.gz` 已生成，并检查其中 `backup-manifest.json` 的 `skippedExtraPaths`
- 检查 `node-recovery-status.json`：普通数据面和 AI 数据面都应为 `ready=true`；必需 `.env` 缺失会列入 `missingRequiredArtifacts`
- 确认 `./backups/r2-upload-record.json` 是否已更新

节点替换的演练和应急命令见[节点备份完整性与快速恢复](node-recovery.md)。应急时先执行 `python3 scripts/node_recovery.py validate --bundle <bundle> --require-ready`，再执行 `prepare`，不要直接解压覆盖运行目录。

SSH 采集的认证、known_hosts、实测路径和只读排障命令见[远端节点配置采集](remote-node-backup.md)。采集器不会在远端写入、重启或执行配置同步。

## TCP 探针

当 `PROBE_ENABLED=1` 时，面板会周期性对 `DATAPLANE_PROBE_HOST:<listen_port>` 做 TCP 连通性探测。

相关页面与配置：

- 页面：`/probe-dashboard`
- 常用变量：`PROBE_INTERVAL`、`PROBE_TIMEOUT`、`PROBE_TEST_LISTEN_PORT`
- compose 默认把 `PROBE_INTERVAL` 设为 `180` 秒

远端模式注意：

- 探针目标不能继续是 `redacted-ip-007`
- 把 `DATAPLANE_PROBE_HOST` 设置成远端入口 IP 或域名

## DNS 故障切换

当 `DNS_FAILOVER_ENABLED=1` 且配置完整时，面板会后台周期性执行以下规则：

- 只探测 `DNS_FAILOVER_PROBE_HOST:DNS_FAILOVER_PROBE_PORT`
- DNS 故障切换探测运行在独立 worker 中，不会被数据面 SSH、日志同步或流量统计阻塞
- 连续失败达到 `DNS_FAILOVER_FAILURE_THRESHOLD` 时，把单条 Cloudflare DNS 记录切到备用目标
- 连续成功达到 `DNS_FAILOVER_RECOVERY_THRESHOLD` 时，自动回切到主数据面
- 如果启用了高峰窗口，窗口内会把备用/专用节点视为首选目标，窗口外恢复主节点优先
- AI 候选故障不触发 DNS 切换：`auto` 模式优先切换到另一候选，全部候选不可达时由 `ai_domain_manager` 回退；数据面故障时 DNS 切到控制面备用，AI 节点健康度决定备用是 relay 还是直出模式

故障场景矩阵（详见 [dns-failover.md](dns-failover.md)）：

| 场景 | DNS 切换 | 流量路径 |
| --- | --- | --- |
| 正常 | — | 客户端→数据面→直出；AI→数据面→AI节点→直出 |
| 单个 AI 候选故障 | 不切换 | 客户端→数据面→另一 AI 候选（auto） |
| 主、备 AI 候选同时故障 | 不切换 | 客户端→数据面→直出（AI 流量回退） |
| 数据面故障 | → backup | 客户端→控制面备用→relay→AI节点→直出 |
| 双节点故障 | → backup | 客户端→控制面备用→直出 |

手动入口：

- 首页 "DNS 故障切换" 卡片
- `POST /api/dns-failover/check`

### 数据面故障时的应急切换

自动切换异常或需要立即恢复业务时，按以下顺序操作：

1. 从外部网络确认控制面备用入口 `DNS_FAILOVER_BACKUP_CONTENT:DNS_FAILOVER_PROBE_PORT` 可达。
2. 在首页「DNS 故障切换」卡片中将目标手动切到 `backup`，或在 Cloudflare DNS 中把 `CF_DNS_RECORD_NAME` 指向 `DNS_FAILOVER_BACKUP_CONTENT`。
3. 确认 Cloudflare 记录已更新，并等待记录 TTL 生效。
4. 检查控制面备用 Xray 的 relay / direct 模式和客户端连接。
5. 数据面恢复后，不要立即手动回切；先确认 `DNS_FAILOVER_PROBE_HOST:DNS_FAILOVER_PROBE_PORT` 连续成功达到 `DNS_FAILOVER_RECOVERY_THRESHOLD`，再让系统自动回切。

如果数据库中的 `last_probe_checked_at` 长时间不更新，而控制面 HTTP 服务仍然可用，优先检查 DNS failover worker、控制面日志和远程 SSH 超时配置。此时不要只重启控制面或单纯调低失败阈值。
- `POST /api/dns-failover/switch`

生效速度建议：

- 非代理记录把 `CF_DNS_RECORD_TTL` 设为 `60`
- `DNS_FAILOVER_INTERVAL` 设小一些可以更快触发切换，但会增加探测频率和 Cloudflare API 调用概率
- 当前不支持 Cloudflare Load Balancer / Pool，也不做多记录原子切换

控制面备用 Xray（双模式）：

- 已新增 `docker compose` 服务 `xray-reality-backup`
- 双模式运行：AI 节点正常时 relay 到 AI 节点，AI 节点也故障时 freedom 直出
- 先把 `CONTROL_PLANE_BACKUP_XRAY_ENABLED=1` 写入根 `.env`
- 控制面作为备用时，启动方式为：`docker compose --profile backup-xray up -d xray-reality-backup`
- 如果控制面本机要接管流量，可把 `DNS_FAILOVER_BACKUP_CONTENT` 留空，让面板自动获取控制面本机公网 IP
- relay 模式只可使用与 AI 节点独立 inbound 完整匹配、受保护的 `CONTROL_PLANE_BACKUP_UPSTREAM_URL`
- 不得从普通数据面 `XRAY_*` 自动派生 relay URL；没有独立 AI 凭据时保持 relay 能力关闭
- 如果不想启用这套本机备用模式，保持 `CONTROL_PLANE_BACKUP_XRAY_ENABLED=0`，并手动填写 `DNS_FAILOVER_BACKUP_CONTENT`
- 完整机制详见 [dns-failover.md](dns-failover.md)

高峰专用节点示例：

- `DNS_FAILOVER_PEAK_ENABLED=1`
- `DNS_FAILOVER_PEAK_START=19:00`
- `DNS_FAILOVER_PEAK_END=23:00`
- `DNS_FAILOVER_PEAK_TIMEZONE=America/Los_Angeles`
- 启用后，面板会在该时区的 19:00-23:00 把备用目标当作首选线路

排查建议：

- 首页先确认“最近探测”与“当前 DNS 指向”是否一致
- `CF_API_TOKEN` 至少需要目标 Zone 的 DNS 编辑权限
- 如果自动切换没有发生，检查探测目标是否确实是数据面公网入口，而不是控制面地址

## 数据面重启与同步能力

### `docker`

- 可通过 `DATAPLANE_CONTAINER_NAME` 管理本地容器
- 默认重启目标是 `xray-reality-local`

### `local`

- 可做配置校验和本地 API 采样
- 进程重启和守护由你自己负责

### `ssh`

- 控制面先在本地渲染，再通过 SSH 上传配置
- 可读取远端 `access.log`、`dynamic-routing.json`、AI 报表和数据库快照

### `unmanaged`

- 面板仍可维护端口和租户数据
- 但不能自动重启、同步或读取数据面状态

## AI 节点重启与同步能力

AI 节点的模式判定与普通数据面相同（`ssh` / `local` / `docker` / `unmanaged`）。当前生产使用本机 `docker` 模式；设置 `AI_NODE_SSH_TARGET` 后才使用远端 `ssh` 模式。

### `ssh`（远端 SSH 纳管）

- 使用远端 SSH 时直接走内网连接和密码/键盘交互认证；应用不保存密码或私钥
- `AI_NODE_SSH_OPTIONS` 必须启用严格主机校验并使用专用 `known_hosts`
- `AI_NODE_API_SERVER` 用于远端 Socket 状态检查；当前生产检查 `redacted-ip-007:27166`
- `AI_NODE_CONFIG_PATH` 非空时才支持上传；生产当前显式留空，因此配置上传关闭
- 即使上传关闭，`GET /api/ai-node/status` 和 `POST /api/ai-node/restart` 仍可用
- AI 节点使用独立 REALITY 凭据，主数据面 outbound 必须与其 inbound 完整匹配
- 部署见 [AI 节点部署与 SSH 纳管](ai-node-deployment.md)，凭据见 [AI 节点独立凭据](ai-node-credentials.md)

### `docker`（本地测试）

- 通过 `AI_NODE_CONTAINER_NAME` 管理本地容器
- 适用于 `docker compose --profile ai-node` 本地测试场景

### `unmanaged`

- 面板仍可渲染 `config-ai-node.json`，但不能自动推送或重启

## 常见问题

### 端口显示不可达

优先检查：

- 数据面监听端口是否真的暴露在目标入口
- `DATAPLANE_PROBE_HOST` 是否仍错误地指向本地回环
- 防火墙或上游转发是否允许面板探测目标端口

### `/healthz` 一直失败

检查：

- 数据面是否在运行
- `DATAPLANE_API_SERVER` 是否可访问
- 如果当前只需要管理 UI，是否已经把 `PANEL_HEALTH_REQUIRES_XRAY=0`

当 DNS 已切到启用的控制面备用 Xray 时，`/healthz` 会把控制面接管状态视为健康；健康检查不会执行流量日志同步，避免主数据面失联时阻塞健康接口。

### 数据面无法重启

常见原因：

- 当前模式为 `unmanaged`
- 未设置 `DATAPLANE_RESTART_COMMAND`
- Docker 模式下 `DATAPLANE_CONTAINER_NAME` 错误

### AI 节点不可达

检查：

- `AI_NODE_SSH_TARGET`、内网 SSH 端口 `22` 和专用 `known_hosts` 是否正确
- 目标 SSH 服务是否允许密码/键盘交互认证，且人工连接可以完成登录
- `AI_NODE_API_SERVER` 指向的本机或远端 Socket 是否监听
- `AI_NODE_PROBE_HOST` 是否指向当前 AI 节点入口
- `AI_UPSTREAM_HOST:AI_UPSTREAM_PORT` 是否是当前 AI 业务端点
- `GET /api/ai-node/status` 返回的 `last_error` 字段
- 部署问题见 [AI 节点部署与 SSH 纳管](ai-node-deployment.md)

如果端口可达但 ChatGPT/OpenAI 仍不能连接，不要重复上传配置；按 [ChatGPT 路由排障](chatgpt-routing-troubleshooting.md) 比较主数据面 outbound 与 AI inbound 的凭据摘要，并核对 Docker 真实 bind source。

### AI 路由状态一直没有报告

检查：

- `docker compose --profile xray logs -f xray-ai-domain-manager`
- `app/xray/reports/hourly-domains/latest.json` 是否生成
- `AI_ROUTING_ENABLED` 是否为 `1`
- AI 候选是否可达，以及 `manual_mode` 是否意外固定在故障节点
- 自动模式下全部候选不可达时，`route_status` 应为 `fallback_to_primary`
- 人工固定目标不可达时，`route_status` 应为 `manual_target_unreachable`
