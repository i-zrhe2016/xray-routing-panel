# xray-routing-panel

`xray-routing-panel` 是一个面向开发者和运维的 Xray REALITY 控制面，用来统一管理普通数据面和 AI 节点上的监听端口、租户订阅、流量配额、AI 路由产物，以及基于 Cloudflare 的 DNS 故障切换。

## 核心能力

- 管理后台（Vue + Naive UI 单页应用）和 JSON API 统一管理监听端口、备注、到期时间、流量上限、租户凭据和订阅链接。
- 面向终端用户的**订阅者门户**：客户注册/登录、浏览套餐、查看订阅、续费和提交支付凭证。新购套餐不再通过面板创建预订单；每个端口租户即客户，原“租户面板”统一为门户中的订阅详情（Clash/V2Ray/VLESS 订阅链接、流量用量、凭据）。
- 根据数据库状态生成 `app/xray/runtime/panel-ports.json` 和 `app/xray/runtime/config.json`。
- 通过 Docker、本地二进制或 SSH 管理唯一 `data_plane`，并读取 Xray API / `access.log` 做统计。
- 按小时分析访问域名，生成动态 AI 路由规则、报表和数据库聚合结果。
- 基于公网 TCP 探测和 Cloudflare API 做单记录 DNS 故障切换，并支持自动回切。
- 管理后台「监控」标签内嵌 Grafana 图表（数据源自 Prometheus），展示主机系统资源与每端口流量/连接速率；配置/订单等数据仍由面板自身（SQLite）提供。详见 [operations.md](operations.md)。
- 可选启用控制面备用 Xray，配合 DNS 切换让控制面本机接管流量。
- 每天备份 `panel.db`、业务附件和节点实际配置，生成带恢复清单的归档，并可选地加密后保存到 Cloudflare R2。

## 当前架构

- `xray-routing-panel`（控制面）
  - Flask 作为 JSON API + SPA 壳服务端：托管管理后台 SPA（`/`）、订阅者门户 SPA（`/portal`）、服务端渲染的公共/认证页（`/customer/login`、`/customer/register`、`/plans`）以及探针/AI 仪表盘
  - 前端发布资源位于 `app/static/admin/*`、`app/static/portal/*` 与 `app/static/landing/*`；Admin 源码与 Vite 配置位于 `frontend/`，构建产物写回 `app/static/admin/*`
  - 维护 `data/panel.db`（客户、套餐、订单、服务订阅、支付凭证，以及端口/流量/AI/DNS 状态）
  - 通过内网 SSH 直连纳管普通数据面；AI 节点当前是控制面本机 Docker `xray-ai-node`，也支持显式切换为远端 SSH 模式
  - 维护 `dns_failover_state` / `dns_failover_history`
- 普通数据面（`xray-reality-local` 或远端数据面）
  - 实际承载 `VLESS + REALITY` 流量
  - 数据面模式由 `docker`、`local`、`ssh`、`unmanaged` 四类自动判定
  - 运行 `ai_domain_manager`，生成 `dynamic-routing.json` 将 AI 域名流量转发到选中的 AI 候选
- AI 路由控制器
  - 探测主 `nat.qq.pw:27166` 与备 `redacted-ip-004:27166`
  - 支持 `auto`、`primary`、`backup`、`forced_fallback` 四种模式
  - 将人工模式和当前候选状态持久化到 `app_state`
- AI 节点（当前备用为控制面本机 Docker；也支持远端独立机器）
  - 运行 VLESS + REALITY Xray，监听 `AI_UPSTREAM_PORT`，接收数据面转发的 AI 流量
  - freedom 直出，不做域名分类、不运行 `ai_domain_manager`
  - 使用独立于普通数据面的 REALITY 凭据；主数据面 outbound 必须与 AI inbound 完整匹配
  - 详见 [ai-node-deployment.md](ai-node-deployment.md)
- `xray-reality-backup`（控制面备用 Xray）
  - 可选的控制面备用 Xray，双模式运行：
    - **relay 模式**（AI 节点正常时）：将所有流量转发到 AI 节点
    - **直出模式**（AI 节点也故障时）：freedom 直出
  - 在 DNS 切换后接管入口流量
  - 详见 [dns-failover.md](dns-failover.md)
- `xray-ai-domain-manager`
  - 每小时读取普通数据面 `access.log`（远端模式通过 SSH 增量读取）
  - 仅将已观测的 AI 分类域名写入 `panel.db` 的 `ai_domains` / `ai_domain_observations`，保留历史累计
  - 输出只包含 AI 域名的 `dynamic-routing.json` 和小时域名报表；非 AI 域名由 DMIT 普通数据面 freedom 直出
  - AI 上游不可达时按模式切换候选；全部候选不可达时删除 `dynamic-routing.json`，流量回退数据面直出
- `xray-routing-panel-db-backup`
  - 负责 `panel.db` 定时备份、控制面配置归档，并在打包前调用只读 SSH 采集普通数据面实际配置
- `collect_remote_backup.py`
  - 只负责普通数据面配置的 SSH 读取、SHA-256 校验和 `nodes/` staging；显式配置远端 AI 时也可采集该节点
- `node_recovery.py`
  - 校验归档完整性，生成普通/AI 节点的便携恢复目录和单节点 Docker Compose
- `R2 灾备上传`
  - 负责将数据库备份加密并上传到 Cloudflare R2

首页当前聚合展示：

- 三节点状态（普通数据面、AI 节点、控制面备用）和当前流量导向
- `data_plane_status`
- `ai_routing_status`
- `dns_failover_status`

AI 节点作为独立受管节点纳管。AI 候选故障不涉及 DNS 切换，由 `ai_domain_manager` 按自动或人工模式选择；全部候选不可达时回退到数据面直出。数据面故障时 DNS 切到控制面备用，根据 AI 节点健康度自动选择 relay 或直出模式。

## 快速开始

1. 生成 REALITY 参数：

```bash
./app/xray/generate-secrets.sh
```

2. 准备 Xray 配置：

```bash
cp app/xray/.env.example app/xray/.env
```

至少填写：

- `XRAY_PUBLIC_HOST`
- `XRAY_CLIENT_UUID`
- `XRAY_REALITY_PRIVATE_KEY`
- `XRAY_REALITY_PUBLIC_KEY`
- `XRAY_REALITY_SHORT_ID`
- `XRAY_SERVER_NAME`
- `XRAY_DEST`

3. 准备面板覆盖项：

```bash
cp .env.example .env
```

常用项：

- `PANEL_PUBLIC_URL`
- `PANEL_USERNAME`
- `PANEL_PASSWORD`
- `PANEL_SECRET_KEY`
- `DATAPLANE_SSH_TARGET`
- `DATAPLANE_PROBE_HOST`
- `DNS_FAILOVER_ENABLED`
- `DB_BACKUP_R2_ENABLED`

4. 渲染配置：

```bash
python -m app.xray.render_config
```

5. 按部署模式启动：

- 只启动面板和数据库备份：

```bash
docker compose up -d --build
```

- 启动本地完整栈（面板 + 本地 Xray + AI 路由）：

```bash
docker compose --profile xray up -d --build
```

- 启动控制面备用 Xray：

```bash
docker compose --profile backup-xray up -d xray-reality-backup
```

> 前端发布资源已随仓库保存在 `app/static/{admin,portal,landing}`，`docker compose --build`
> 直接将其复制进镜像。运行时和镜像构建不安装 JavaScript 构建工具；Admin 修改需在宿主机
> `frontend/` 中完成测试和构建。

默认地址：

- 管理后台：`http://服务器IP:18080/`
- 订阅者门户：`http://服务器IP:18080/portal`
- 公共套餐页：`http://服务器IP:18080/plans`
- 租户订阅直达：`http://服务器IP:18080/tenant/<tenant_token>`
- 探针页：`http://服务器IP:18080/probe-dashboard`
- AI 域名页：`http://服务器IP:18080/ai-domain-dashboard`
- 健康检查：`http://服务器IP:18080/healthz`

## 常见部署变体

### 只跑控制面

- 使用 `docker compose up -d --build`
- 如不依赖本地 Xray，建议设置 `PANEL_HEALTH_REQUIRES_XRAY=0`

### 远端数据面

至少配置：

- `DATAPLANE_SSH_TARGET`
- `DATAPLANE_CONFIG_PATH`
- `DATAPLANE_PANEL_PORTS_PATH`
- `DATAPLANE_ACCESS_LOG_PATH`
- `DATAPLANE_PROBE_HOST`

注意：

- 控制面会先在本地渲染，再通过 SSH 上传产物
- 如果控制面和数据面分离，`DATAPLANE_PROBE_HOST` 应改成远端入口 IP 或域名，而不是 `redacted-ip-007`

### AI 节点

AI 节点当前是控制面本机 Docker 上的独立 VLESS + REALITY Xray，接收数据面转发的 AI 流量并 freedom 直出；也支持远端独立机器部署。部署见 [AI 节点部署与 SSH 纳管](ai-node-deployment.md)，凭据边界见 [AI 节点独立凭据](ai-node-credentials.md)。

至少配置：

- 本机模式：`AI_NODE_CONTAINER_NAME=xray-ai-node`、`AI_NODE_API_SERVER`、`AI_NODE_PROBE_HOST`
- 远端模式：`AI_NODE_SSH_TARGET`、`AI_NODE_SSH_BIN` / `AI_NODE_SSH_OPTIONS`、`AI_NODE_API_SERVER`、`AI_NODE_PROBE_HOST`
- `AI_UPSTREAM_HOST` / `AI_UPSTREAM_PORT`（在 `app/xray/.env` 中）

生产当前保持 `AI_NODE_CONFIG_PATH=`，禁用配置上传但保留状态检查与容器重启。

### 控制面备用 Xray

适合主数据面在远端、控制面本机作为备用接管节点的场景。双模式运行：AI 节点正常时 relay 到 AI 节点，AI 节点也故障时 freedom 直出。详见 [dns-failover.md](dns-failover.md)。

- 启用 `CONTROL_PLANE_BACKUP_XRAY_ENABLED=1`
- 启动 `xray-reality-backup`：`docker compose --profile backup-xray up -d xray-reality-backup`
- 如需自动推导备用 IP，可留空 `DNS_FAILOVER_BACKUP_CONTENT`
- relay 模式必须使用与 AI 节点独立 inbound 完整匹配、受保护的 `CONTROL_PLANE_BACKUP_UPSTREAM_URL`
- 不得从普通数据面 `XRAY_*` 盲目派生 relay URL；未提供独立 AI 凭据时应保持 relay 能力关闭

重要限制：

- `xray-reality-local` 和 `xray-reality-backup` 如果绑定同一端口，不能在同一台机器上同时接管同一个入口
- 常见用法是"主数据面在远端，控制面本机只作为备用"

## DNS 故障切换快速配置

最小可用配置示例：

```env
DNS_FAILOVER_ENABLED=1
DNS_FAILOVER_INTERVAL=10
DNS_FAILOVER_TIMEOUT=2
DNS_FAILOVER_FAILURE_THRESHOLD=2
DNS_FAILOVER_RECOVERY_THRESHOLD=2

DNS_FAILOVER_PROBE_HOST=edge.example.com
DNS_FAILOVER_PROBE_PORT=443

CF_API_TOKEN=replace_me
CF_ZONE_ID=replace_me
CF_DNS_RECORD_ID=replace_me
CF_DNS_RECORD_TYPE=A
CF_DNS_RECORD_NAME=edge.example.com
CF_DNS_RECORD_PROXIED=0
CF_DNS_RECORD_TTL=60

# 留空时自动获取主数据面公网 IP
DNS_FAILOVER_PRIMARY_CONTENT=

CONTROL_PLANE_BACKUP_XRAY_ENABLED=1

# 留空时自动获取控制面本机公网 IP
DNS_FAILOVER_BACKUP_CONTENT=
DNS_FAILOVER_BACKUP_LABEL=控制面备用Xray
```

行为说明：

- 自动切换只看 `DNS_FAILOVER_PROBE_HOST:DNS_FAILOVER_PROBE_PORT`
- DNS 故障切换运行在独立 worker 中，不会被数据面 SSH、日志同步或流量统计阻塞
- 连续失败达到阈值时切到备用，连续成功达到阈值时自动回切
- `DNS_FAILOVER_PRIMARY_CONTENT` 留空时，控制面会自动获取当前数据面的公网 IP
- `DNS_FAILOVER_BACKUP_CONTENT` 留空时，只有在 `CONTROL_PLANE_BACKUP_XRAY_ENABLED=1` 时才会自动获取控制面本机公网 IP
- AI 路由支持人工切换；总览展示 `ai_candidates`、`manual_mode`、当前目标和不可达原因
- 对 REALITY 这类直连流量，建议保持 `CF_DNS_RECORD_TTL=60` 以尽快生效

相关接口：

- `GET /api/dns-failover`
- `POST /api/dns-failover/check`
- `POST /api/dns-failover/switch`

## 灾备归档与 R2 上传

默认情况下，`xray-routing-panel-db-backup` 每天 `03:00 UTC` 生成一次本地 SQLite 备份和带节点恢复清单的灾备归档；Compose 通过内网直连 SSH 以只读方式采集普通数据面 `root@100.116.187.106:22`，本机 AI 备用配置随 `app/xray/.env` 和运行时目录一并归档。

Compose 备份服务默认会在备份完成后自动加密并上传到 Cloudflare R2；首次部署前请在根 `.env` 中填入：

- 在根 `.env` 中设置 `DB_BACKUP_R2_ENABLED=1`
- 设置 `DB_BACKUP_R2_ENDPOINT`、`DB_BACKUP_R2_BUCKET`
- 设置 `DB_BACKUP_R2_ACCESS_KEY_ID`、`DB_BACKUP_R2_SECRET_ACCESS_KEY`
- 设置独立的 `DB_BACKUP_ENCRYPTION_PASSWORD`

R2 仅作为低频异地灾备保存通道，不进入 DNS 故障切换路径；从归档快速准备替换节点见 [node-recovery.md](node-recovery.md)，归档结构和远端采集见 [disaster-backup.md](disaster-backup.md) 与 [remote-node-backup.md](remote-node-backup.md)。

先验证本地归档和 R2 配置：

```bash
python3 -m unittest tests.test_backup_cycle tests.test_upload_backup_r2
```

手动触发一轮“备份后上传 R2”：

```bash
docker compose run --rm xray-routing-panel-db-backup \
  python3 /app/scripts/run_db_backup_cycle.py
```

## 常用接口摘要

管理后台（需管理员会话 / Basic X

- `GET /`: 管理后台 SPA 壳
- `GET /api/dashboard`: 首页完整状态
- `POST /api/ports`: 新建监听端口
- `PUT /api/ports/<port_id>`: 更新端口配置
- `POST /api/plans` / `PUT /api/plans/<id>`: 套餐增改
- `GET /api/orders` / `POST /api/orders/<id>/{fulfill,reject,cancel}`: 订单审核与开通
- `POST /api/data-plane/restart`: 重启数据面
- `GET /api/ai-node/status`: 获取 AI 节点状态
- `POST /api/ai-node/restart`: 重启 AI 节点
- `GET /api/ai-nodes/status`: 获取多节点 AI 纳管状态
- `POST /api/ai-nodes/<node_id>/restart`: 单独重启指定 AI 节点
- `GET /api/dns-failover`: 获取 DNS 故障切换状态
- `POST /api/dns-failover/check`: 立即执行一次 DNS 检测
- `POST /api/dns-failover/switch`: 手动切主备

订阅者门户（客户会话）：

- `GET /portal`、`GET /portal/<path>`: 门户 SPA 壳（vue-router history）
- `GET /api/customer/{me,overview,subscriptions[/<id>],orders[/<no>],plans}`: 门户数据
- `POST /api/customer/orders/<order_no>/payment-proof`、`.../<id>/renew`: 为已有订单传支付凭证、续费；新购套餐不提供客户侧预订单接口
- `POST /api/customer/auth/{login,register,logout}`: 客户认证

租户直达（token / 每端口凭据）：

- `GET /tenant/<tenant_token>`: 门户单订阅只读模式壳
- `GET /api/tenant/<tenant_token>/subscription`、`POST .../login`

公共与其他：

- `GET /healthz`: 返回 `{"ok": <bool>, "data_plane_running": <bool>}`
- `GET /metrics`: Prometheus 文本格式指标（需 `METRICS_TOKEN`，`Authorization: X <token>`）；管理后台「监控」标签把这些指标经 Grafana 内嵌出图（需 `GRAFANA_PUBLIC_URL`）
- `GET /probe-dashboard`: TCP 探针监控页
- `GET /ai-domain-dashboard`: AI 域名统计页

完整接口说明见 [api.md](api.md)。

## 核心配置摘要

- 根目录 `.env`
  - 面板地址、管理员认证、数据面接入参数、DNS 故障切换
- `DNS_FAILOVER_*` / `CF_*`
  - Cloudflare DNS 故障切换和自动回切
- `CONTROL_PLANE_BACKUP_XRAY_ENABLED`
  - 是否启用"控制面本机公网 IP + 备用 Xray"自动备用模式（relay / 直出双模式）
- `AI_NODE_*`
  - AI 节点 SSH 纳管参数，详见 [ai-node-deployment.md](ai-node-deployment.md)
- `DB_BACKUP_*`
  - 本地 SQLite 快照、控制面/远端节点配置归档和 R2 灾备上传组件配置
- `app/xray/.env`
  - REALITY 基础参数、AI 上游、分类器和 MCP 配置

完整变量清单见 [configuration.md](configuration.md)。

## 代码入口

后端（`app/` 已包化，`app/panel.py` 为入口，导出 `app`/`state`/`main`）：

- [../app/web/](../app/web/): app factory（`create_app`）+ 按域分的视图模块（`admin_views`、`admin_api`、`customer_api`、`customer_views`、`portal_views`、`tenant_views`、`subscription_views`、`health`）与共享 `core.py`（presenter、auth 守卫、`@route` 收集器）
- [../app/state/](../app/state/): `PanelState` facade，组合域 service（`CoreService`、`PortsService`、`TrafficService`、`ProbesService`、`DnsFailoverService`、`AiRoutingService`、`CommerceService`、`DiagnosticsService`）——控制逻辑、维护循环、统计同步、探针、DNS 故障切换、商业化。持有 `data_plane` 和 `ai_node` 两个受管节点控制器
- [../app/config/](../app/config/) / [../app/auth/](../app/auth/): 配置常量/解析器、三套会话（管理员/租户/客户）与 CSRF
- [../app/dns_failover.py](../app/dns_failover.py): Cloudflare API 客户端与切换策略
- [../app/xray/render_config.py](../app/xray/render_config.py): 渲染 Xray 服务端和客户端产物（普通数据面、AI 节点、控制面备用）
- [../app/xray/node/](../app/xray/node/): `NodeController` / `NodeBackend`——按 SSH、Docker、本地进程分离节点管理、文件同步与探测，统一用于普通数据面和 AI 节点
- [../app/xray/node_control.py](../app/xray/node_control.py): 旧导入路径兼容 facade，继续导出历史控制器名称
- [../app/xray/ai_routing/runner.py](../app/xray/ai_routing/runner.py): AI 路由定时任务和 CLI 入口
- [../app/xray/ai_routing/manager.py](../app/xray/ai_routing/manager.py): AI 路由单轮编排

前端与运维：

- [../frontend/](../frontend/): 前端源码快照；实际部署使用已生成的 `app/static/{admin,portal,landing}` 发布资源
- [disaster-backup.md](disaster-backup.md): 配置归档、R2 灾备保留和离线恢复边界
- [remote-node-backup.md](remote-node-backup.md): 通过严格只读 SSH 采集普通数据面实际配置；本机 AI 配置随控制面归档
- [node-recovery.md](node-recovery.md): 节点备份完整性、校验和快速准备替换节点
- [db-backup-uploader.md](db-backup-uploader.md): 加密和 R2 上传组件
- [../Dockerfile](../Dockerfile): 复制静态发布资源并安装 Python 依赖
- [../docker-compose.yml](../docker-compose.yml): 本地 compose 栈

## 开发与测试

```bash
# 后端测试（Python，需先装依赖；项目用 pytest 跑现有 unittest）
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q

# Admin 前端测试与构建
cd frontend && npm ci && npm test && npm run build
```

## 文档导航

- [根目录 README](../README.md)：仓库根目录入口
- [panel-migration.md](panel-migration.md): 面板迁移
- [fault-tolerance.md](fault-tolerance.md): 三节点故障容错边界
