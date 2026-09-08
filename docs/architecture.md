# 架构说明

## 总览

控制面负责编排普通数据面，并通过内网 SSH 直连纳管远端节点；当前 AI 备用运行在控制面本机。AI 路由维护主、备两个候选，支持自动探测和控制台人工固定选择。用户代理流量的正常路径不依赖控制面在线：

![生产架构图](diagrams/system-architecture.svg)

[查看 PlantUML 源文件](diagrams/system-architecture.puml)

- **普通数据面**：承载 `VLESS + REALITY` 流量，加载控制面生成的动态 AI 路由
- **AI 上游池**：主 `nat.qq.pw:27166`，备用 `redacted-ip-004:27166`；备用节点是控制面本机 Docker `xray-ai-node`

- 控制面：`xray-routing-panel`
- 普通数据面：本地容器、本地二进制，或远端 SSH 目标上的 Xray
- AI 节点：控制面本机 Docker `xray-ai-node`；如有独立远端 AI 节点也支持 SSH 纳管
- AI 路由子系统：控制面容器中的 `xray-ai-domain-manager`，通过内网 SSH 或共享工作目录管理普通数据面
- 备份子系统：`xray-routing-panel-db-backup`
- 备份归档上传组件：`R2 灾备上传`

首页展示三节点状态（普通数据面、AI 节点、控制面备用）和当前流量导向路径，以及 AI 路由状态和 DNS 故障切换状态。
如果启用了 DNS 故障切换，首页还会额外展示当前 DNS 指向、最近探测结果和最近一次切换状态。

三节点单点故障的正式边界见 [fault-tolerance.md](fault-tolerance.md)。控制面故障时，已有客户端的普通代理流量应继续走普通数据面；控制面管理页面、订阅和自动运维不属于数据面可用性保证范围。

## 已部署扩展：Prometheus-only 每日节点运维分析

生产环境已部署 Prometheus、Grafana 和 `xray-ops-daily-reporter`。普通数据面由 Prometheus 直接抓取 exporter；AI 数据面位于 NAT 后，由控制面的专用 SSH 回环隧道转发 exporter HTTP。Reporter 只查询 Prometheus HTTP API，以确定性规则生成 JSON/Markdown 影子报告，并仅在 SQLite 保存 `report_runs` 审计。

该子系统不进入用户流量路径。旧 SSH/raw-log Collector 已从生产删除，Reporter 不挂载 SSH 凭据、不执行远程命令，也不读取 Xray、Docker 或 systemd 原始日志。当前仍为 `rules_only` 影子模式，正式验收边界见[每日节点运维分析](ops-reporting/index.md)，日常问题见[故障排查手册](ops-reporting/troubleshooting.md)。

## 组件职责

### 控制面

- 入口代码：`app/panel.py`（进程入口）→ `app/web/`（`create_app` 工厂 + 按域视图模块）、`app/state/`（`PanelState` facade 组合域 service）
- `PanelState` 持有两个受管节点控制器：`data_plane`（普通数据面）和 `ai_node`（AI 节点），均复用 `DataPlaneController` / `ManagedNodeController`（`app/xray/node_control.py`）
- Flask 同时托管：管理后台 SPA（`/`）、订阅者门户 SPA（`/portal`）、公共/认证页（`/plans`、`/customer/*`）、租户订阅直达（`/tenant/<token>`）和探针/AI 仪表盘；前端发布资源位于 `app/static/{admin,portal,landing}`，Admin 源码与 Vite 配置位于 `frontend/`
- 保存端口、租户、流量、AI 聚合，以及商业化数据（客户、套餐、订单、服务订阅、支付凭证）到 `data/panel.db`
- 保存 DNS 故障切换状态和事件历史到 `data/panel.db`
- 根据数据库内容生成 `app/xray/runtime/panel-ports.json`
- 调用 `python -m app.xray.render_config` 生成 `app/xray/runtime/config.json`（普通数据面）、`config-ai-node.json`（AI 节点）和 `config-backup.json`（控制面备用）
- 对普通数据面做配置校验、同步、重启、统计采集、探针采样和 Cloudflare DNS 切换
- 对 AI 节点做本机 Docker 状态检查和重启；显式配置远端目标时改用 SSH，配置上传能力由 `AI_NODE_CONFIG_PATH` 单独控制，生产当前保持关闭
- 读取本机 AI Xray 的回环 `/debug/vars`，只聚合入站/直出累计字节；以 Prometheus 文本格式暴露 `/metrics`（token 鉴权）。管理后台「监控」标签把这些指标经 Grafana（`monitoring/` 栈）以 `d-solo` iframe 内嵌出图，观测数据走 Prometheus，配置/事务数据仍走 `data/panel.db`

### 普通数据面

- 实际承载 `VLESS + REALITY` 流量
- 通过 Xray API 暴露 `statsquery`
- 通过 `access.log` 提供连接和域名观测输入
- 接收 `xray-ai-domain-manager` 生成并同步的 `dynamic-routing.json`，将 AI 域名流量转发到选中的 AI 上游
- `auto` 模式下单个 AI 上游不可达时切换到另一候选；全部候选不可达，或人工固定目标不可达时，管理器删除 `dynamic-routing.json`，AI 流量回退到数据面 freedom 直出

### AI 节点

- 当前为控制面本机 Docker `xray-ai-node` 上的独立 VLESS + REALITY Xray；也支持部署到远端独立机器
- 监听 `AI_UPSTREAM_PORT`，通过 VLESS + REALITY 接收普通数据面 `ai_proxy` outbound 转发的 AI 域名流量
- freedom 直出，不做域名分类、不运行 `ai_domain_manager`、无 panel-ports；记录独立 `ai-access.log`/`ai-error.log`，并通过回环 metrics 暴露入站与直出字节计数
- 使用独立于普通数据面的 REALITY 凭据；两端隧道字段必须按 [AI 节点独立凭据](ai-node-credentials.md) 保持匹配
- 本机模式通过 Docker 做状态检查和重启；远端模式通过 SSH 管理。生产当前以空 `AI_NODE_CONFIG_PATH` 禁止自动上传配置
- 部署见 [AI 节点部署与 SSH 纳管](ai-node-deployment.md)，故障处理见 [ChatGPT 路由排障](chatgpt-routing-troubleshooting.md)

### 控制面备用 Xray

- 可选在控制面本机启动备用 `xray-reality-backup`，在 DNS 切换后接管入口流量
- 双模式运行：
  - **relay 模式**（AI 节点正常时）：将所有流量转发到 AI 节点
  - **直出模式**（AI 节点也故障时）：freedom 直出
- 控制面自动探测 AI 节点可达性并切换模式
- 详见 [dns-failover.md](dns-failover.md)

### AI 路由子系统

- 入口代码：`app/xray/ai_routing/runner.py`；编排集中在 `app/xray/ai_routing/manager.py`
- 按职责拆分为观测、分类、候选探测、选择、数据库仓储和产物生成模块；旧的 `app/xray/ai_domain_manager.py` 仅保留兼容导出
- 每小时通过普通数据面 SSH 增量读取 `access.log`，统计最近一小时域名窗口；本地模式读取本机日志
- 结合内建规则、Codex 或 OpenAI 兼容接口做域名分类
- 探测 `nat.qq.pw:27166` 和 `redacted-ip-004:27166` 两个 AI 候选
- 支持 `auto`、`primary`、`backup`、`forced_fallback` 四种选择模式；人工模式写入 `panel.db` 的 `app_state`
- 将 `ai_candidates`、`manual_mode`、当前 `ai_target` 和不可达原因提供给控制台
- 仅将已观测且分类为 `ai` 的域名写入 `panel.db` 的 `ai_domains` / `ai_domain_observations`，并保留已有 AI 历史累计；动态路由只包含 AI 域名，其他域名沿用普通数据面 `freedom` 直出

### 灾备归档、完整性与节点恢复组件

- 入口代码：`scripts/run_db_backup_cycle.py`、`scripts/collect_remote_backup.py`、`scripts/build_backup_bundle.py`、`scripts/node_recovery.py`、`scripts/upload_backup_r2.py`
- 先由 `scripts/backup_db.py` 生成新的 `panel.db` 备份，再按 `DB_BACKUP_EXTRA_PATHS` 收集控制面文件，并通过严格只读 SSH 收集普通数据面的实际配置；本机 AI 配置随控制面运行时目录归档
- `collect_remote_backup.py` 只负责 SSH、校验和 staging；`build_backup_bundle.py` 负责归档及两个 manifest；`node_recovery.py` 负责归档验证和替换节点目录准备；`upload_backup_r2.py` 只负责加密、R2 上传和记录写入
- 按配置调用 Cloudflare R2 做加密归档的异地保存，不进入快速恢复或故障切换路径

## 节点模式判定

`app/xray/node_control.py` 中的 `DataPlaneController`（别名 `ManagedNodeController`）按以下优先级决定模式，对普通数据面和 AI 节点均适用：

1. `ssh`
   - 条件：设置了 `DATAPLANE_SSH_TARGET`（普通数据面）或 `AI_NODE_SSH_TARGET`（AI 节点）
   - 能力：同步配置、读取远端日志和报表、重启远端节点
2. `local`
   - 条件：设置了可执行的 `DATAPLANE_LOCAL_BIN`
   - 能力：本地校验配置和访问本地 API；进程守护由你自己负责
3. `docker`
   - 条件：存在可管理的 `DATAPLANE_CONTAINER_NAME`（普通数据面）或 `AI_NODE_CONTAINER_NAME`（AI 节点）
   - 能力：重启本地容器并读取本地 API
4. `unmanaged`
   - 条件：以上都不满足
   - 能力：面板仍可维护元数据和渲染配置，但不能自动重启或同步节点

AI 节点当前使用 `docker` 模式；显式设置远端目标后才使用 `ssh` 模式。AI 域名同步模式在 UI 中会显示为：

- `远端镜像`：`ssh`
- `本地运行`：`local` 或 `docker`
- `本地缓存`：`unmanaged`

## 主要数据流

1. 管理员在 Web UI 或 `POST /api/ports` 修改端口状态。
2. `panel.db` 持久化端口、租户、流量和 AI 聚合数据。
3. `panel-ports.json` 记录当前有效监听端口。
4. `render_config.py` 合并 `app/xray/.env`、`panel-ports.json` 和可选 `dynamic-routing.json`，生成 `config.json`（普通数据面）、`config-ai-node.json`（AI 节点）和 `client-test.json`；`xray-ai-domain-manager` 重渲染时必须继续使用同目录的 `panel-ports.json`，避免周期任务覆盖有效监听端口。
5. 控制面通过 SSH 将 `config.json` 推送到普通数据面；AI 节点配置同步由 `AI_NODE_CONFIG_PATH` 独立控制，生产当前禁用自动上传。
6. 普通数据面加载 `config.json` 并通过 Xray API 提供 `statsquery`。
7. `xray-ai-domain-manager` 每小时从普通数据面 `access.log` 读取域名，探测双 AI 候选，将 AI 观测写入 `panel.db`，并输出只含 AI 域名的路由产物；人工切换会立即触发一次 `--once` 重算。
8. AI 域名流量通过 `dynamic-routing.json` 转发到选中的 AI 上游；截至 2026 年 8 月 23 日，主节点不可达，备用 `redacted-ip-004:27166` 已被选中。
9. 非 AI 域名不进入 `dynamic-routing.json`，由普通数据面的默认 `freedom` 在 DMIT 直出；自动模式下所有候选不可达，或人工固定目标不可达时，管理器删除 `dynamic-routing.json`，AI 流量也回退数据面 freedom 直出。
10. 独立 DNS 故障切换 worker 对数据面公网入口做 TCP 探测，并在达到阈值时调用 Cloudflare API 更新单条记录；它与数据面日志、流量和配置同步任务隔离。
11. 数据面故障时 DNS 切到控制面备用。控制面探测 AI 节点可达性：AI 节点正常 → relay 模式转发到 AI 节点；AI 节点也故障 → 自动切换为直出模式。
12. `xray-routing-panel-db-backup` 按 cron 生成 `backups/*.db`，先通过 `collect_remote_backup.py` 只读采集普通数据面，再生成带 `backup-manifest.json` 和 `node-recovery-manifest.json` 的 `backups/*-disaster-*.tar.gz`；本机 AI 配置来自 `config/`，校验结果写入 `node-recovery-status.json`，启用时调用 Cloudflare R2 上传加密灾备归档。
13. 首页读取三节点状态、双 AI 候选、流量导向路径、`ai_routing_status`、`dns_failover_status` 和 AI 域名聚合结果。

AI 观测链路：`xray-ai-node` 将 Xray metrics 绑定到 `redacted-ip-007:31097`，面板读取后在
`/metrics` 输出 `xray_panel_ai_node_*`；控制面 cAdvisor 绑定 `redacted-ip-007:18081`，
Prometheus 同时采集 `xray-ai-node` 的容器级 CPU、内存和网络总量。AI access log 只
保留在控制面日志目录，集中日志链路仅采集 `ai-error.log`。面板增量读取
`ai-access.log`，在最近窗口内按目标域名、端口和网络协议聚合请求量，输出
`xray_panel_ai_destination_*` Top 指标；Xray access log 不提供按目标拆分的字节量。

## 关键运行产物

- `data/panel.db`：端口、租户、流量和 AI 域名聚合
- `data/panel.db` 内 `dns_failover_state` / `dns_failover_history`：DNS 切换当前态和最近事件
- `backups/*.db`：最近几天的本地数据库备份
- `backups/*-disaster-*.tar.gz`：包含数据库与配置文件的离线灾备归档
- `backups/node-recovery-status.json`：最近一次节点恢复完整性状态
- 归档 `nodes/remote-node-collection.json`：普通数据面的 SSH 目标、文件状态和 SHA-256；显式启用远端 AI 采集时才会增加 AI 节点项
- 归档 `node-recovery-manifest.json`：节点必需/可选文件、恢复目标路径和 `recoveryReady`
- `backups/r2-upload-record.json`：最新一次 R2 上传记录
- `app/xray/runtime/panel-ports.json`：当前有效监听端口列表
- `app/xray/runtime/config.json`：普通数据面 Xray 服务端配置
- `app/xray/runtime/config-ai-node.json`：AI 节点 Xray 服务端配置
- `app/xray/runtime/config-backup.json`：控制面备用 Xray 配置（relay 或直出模式）
- `app/xray/runtime/client-test.json`：本地客户端测试配置
- `app/xray/runtime/dynamic-routing.json`：AI 动态路由片段（AI 节点不可达时被删除）
- `app/xray/reports/hourly-domains/latest.json`：最近一小时域名报告
- `app/xray/logs/access.log`：普通数据面连接和域名观测输入
- `app/xray/logs/ai-access.log` / `ai-error.log`：AI 节点本地连接与错误日志
