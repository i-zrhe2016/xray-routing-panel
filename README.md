# xray-routing-panel

`xray-routing-panel` 是面向开发者和运维人员的 Xray REALITY 控制面，用于统一管理普通数据面、AI 数据面、订阅业务、流量观测、故障切换和灾备归档。

控制面负责状态、配置编排和运维决策；普通数据面只承载代理流量并执行下发配置；AI 数据面只接收 AI 路由流量并独立出站。详细介绍见[项目概览](docs/project-overview.md)。

## 核心能力

- 管理监听端口、租户凭据、套餐、订单、订阅、到期时间和流量上限。
- 生成并校验 Xray 配置，通过本地、Docker 或 SSH 模式管理数据面。
- 读取 Xray API 与访问日志，提供流量、连接速率、探针和节点健康状态。
- 识别 AI 域名并将相关流量转发到独立 AI 数据面，故障时自动回退。
- 通过 Cloudflare DNS API 实现普通数据面故障切换和自动回切。
- 使用 Prometheus、Grafana、Node Exporter 和 cAdvisor 提供可观测性与日报。
- 可选使用 Fluent Bit + Loki 通过 Tailscale 采集三节点 Docker stdout/stderr 和关键错误日志，并在 Grafana 中查询。
- 定时备份 `panel.db`、业务附件和节点实际配置，生成带完整性清单的灾备归档；可选通过 Cloudflare R2 做异地保存，并能快速准备可直接启动的替换节点目录。

## 架构概览

![Xray Routing Panel production architecture](docs/diagrams/system-architecture.svg)

[PlantUML 源文件](docs/diagrams/system-architecture.puml) · [详细架构](docs/architecture.md) · [三节点容错](docs/fault-tolerance.md)

| 组件 | 单一职责 |
| --- | --- |
| `xray-routing-panel` | 用户、订单、订阅、节点状态、配置编排和故障切换 |
| 普通数据面 | 承载 VLESS + REALITY 流量并执行控制面下发的配置 |
| AI 数据面 | 接收 AI 流量并独立出站，不执行域名分类或控制面逻辑 |
| `xray-ai-domain-manager` | 从访问日志生成 AI 域名路由产物和统计 |
| `xray-reality-backup` | 普通数据面故障时提供备用入口 |
| `upload_backup_r2.py` | 使用 AES-256-GCM 加密灾备归档并通过 R2 S3 API 上传 |

## 快速开始

### 环境要求

- Docker Engine 和 Docker Compose v2
- Python 3.10+（仅本地运行或开发需要）
- Xray REALITY 所需域名、密钥和客户端 UUID

### 1. 生成 REALITY 参数

```bash
./app/xray/generate-secrets.sh
```

### 2. 准备配置

```bash
cp .env.example .env
cp app/xray/.env.example app/xray/.env
```

在本地填写真实值，不要提交 `.env`、REALITY 私钥、Cloudflare Token 或数据库备份；SSH 纳管使用内网直连，不需要提交 SSH 私钥。变量说明见[配置说明](docs/configuration.md)。

### 3. 启动服务

仅启动控制面和数据库备份服务：

```bash
docker compose up -d --build
```

启动控制面、本地 Xray 和 AI 路由完整栈：

```bash
docker compose --profile xray up -d --build
```

启用控制面备用 Xray：

```bash
docker compose --profile backup-xray up -d xray-reality-backup
```

更多模式和排障命令见[开发与启动](docs/development.md)和[运维与排障](docs/operations.md)。

启用 Fluent Bit 三节点日志采集：

```bash
# 在日志中心主机
cd monitoring/loki
cp .env.example .env
# 编辑 .env，设置 LOKI_TAILNET_BIND_ADDRESS
docker compose up -d

# 在控制面、普通数据面、AI 数据面分别执行
cd ../fluent-bit
cp .env.example .env
# 编辑 .env，设置节点角色、主机名、Xray 日志目录和 Loki Tailscale 地址
docker compose -f docker-compose.agent.yml up -d

# 在 Grafana/Prometheus 所在控制面
cd ../..
cp monitoring/.env.example monitoring/.env
# 编辑 monitoring/.env，设置 GRAFANA_LOKI_URL
docker compose -f monitoring/docker-compose.monitoring.yml up -d prometheus grafana
```

日志采集边界见 [Fluent Bit 日志采集](docs/logging-fluent-bit.md)。

当前生产环境的控制面为内网地址 `100.92.231.104`，同时运行本机 AI 备用；普通数据面为 `100.116.187.106`，由控制面通过内网 SSH 直连纳管。控制面业务日志已进入 Loki。实际路径、验收结果和回滚方式见 [当前生产部署](docs/logging-fluent-bit.md#当前生产部署)。

启用配置归档并通过 Cloudflare R2 保存异地灾备版本（不用于快速恢复）：

```bash
DB_BACKUP_R2_ENABLED=1 \
DB_BACKUP_R2_ENDPOINT='https://<account-id>.r2.cloudflarestorage.com' \
DB_BACKUP_R2_BUCKET='xray-routing-panel-disaster' \
DB_BACKUP_R2_ACCESS_KEY_ID='replace-with-access-key' \
DB_BACKUP_R2_SECRET_ACCESS_KEY='replace-with-secret-key' \
DB_BACKUP_ENCRYPTION_PASSWORD='separate-archive-password' \
docker compose up -d --build xray-routing-panel-db-backup
```

控制面额外文件和本机 AI 配置通过 `DB_BACKUP_EXTRA_PATHS` 归档，普通数据面实际配置由只读 SSH 采集；完整边界见[灾备归档与 R2 上传通道](docs/disaster-backup.md)、[远端节点配置采集](docs/remote-node-backup.md)和[节点快速恢复](docs/node-recovery.md)。

### 默认访问地址

| 功能 | 地址 |
| --- | --- |
| 管理后台 | `http://服务器IP:18080/` |
| 订阅者门户 | `http://服务器IP:18080/portal` |
| 公共套餐页 | `http://服务器IP:18080/plans` |
| 租户订阅 | `http://服务器IP:18080/tenant/<tenant_token>` |
| 节点探针 | `http://服务器IP:18080/probe-dashboard` |
| AI 域名面板 | `http://服务器IP:18080/ai-domain-dashboard` |
| 健康检查 | `http://服务器IP:18080/healthz` |

页面、认证和 JSON API 见 [API 文档](docs/api.md)。

## 按场景阅读

| 场景 | 首选文档 |
| --- | --- |
| 第一次了解项目 | [项目概览](docs/project-overview.md) → [架构说明](docs/architecture.md) → [配置说明](docs/configuration.md) |
| 本地开发或启动控制面 | [开发与启动](docs/development.md) |
| 管理远端普通数据面 | [架构说明](docs/architecture.md) → [内网 SSH 纳管](docs/ssh-key-access.md) |
| 部署独立 AI 数据面 | [AI 节点部署](docs/ai-node-deployment.md) → [AI 节点独立凭据](docs/ai-node-credentials.md) |
| 查看 AI 主机与容器监控 | [AI 节点部署](docs/ai-node-deployment.md#ai-节点监控采集) → [运维与排障](docs/operations.md#prometheus-监控metrics) |
| 排查 ChatGPT/OpenAI 路由 | [ChatGPT 路由排障](docs/chatgpt-routing-troubleshooting.md) |
| 排查 Clash REALITY 节点测速超时 | [Clash REALITY 健康检查超时排障](docs/troubleshooting/clash-reality-health-check-timeout.md) |
| 配置故障切换 | [DNS 故障切换](docs/dns-failover.md) → [三节点容错](docs/fault-tolerance.md) |
| 查询三节点日志 | [Fluent Bit 日志采集](docs/logging-fluent-bit.md) |
| 监控节点和生成日报 | [Prometheus-only 运维分析](docs/ops-reporting/index.md) |
| 迁移或灾难恢复 | [面板迁移](docs/panel-migration.md) → [灾备归档](docs/disaster-backup.md) → [节点快速恢复](docs/node-recovery.md) |
| AWS 普通数据面迁移 | [AWS 普通数据面迁移与回退](docs/aws-normal-data-plane-migration.md) |

## 开发与验证

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
ruff check .
black --check .
```

前端发布资源仍随仓库保存在 `app/static/{admin,portal,landing}`，运行时和镜像内不需要 JavaScript 构建工具。
Admin 控制台的源码与 Vite 构建配置位于 `frontend/`；构建后会将 Admin bundle 写入
`app/static/admin/`，再由 Flask/Docker 直接发布。

完整流程见[开发与启动](docs/development.md)。

## 完整文档导航

`docs/` 是详细文档的权威目录；[文档首页](docs/index.md)提供与本节一致的内部索引。

### 开始使用

- [文档首页](docs/index.md) — 阅读顺序和完整索引。
- [仓库当前状态](docs/Repo_Current_State.md) — 已核实的实现、验证限制与当前工作状态。
- [项目概览](docs/project-overview.md) — 项目定位、核心能力、架构摘要和快速开始。
- [架构说明](docs/architecture.md) — 控制面、普通数据面、AI 数据面、组件边界和数据流。
- [配置说明](docs/configuration.md) — 根 `.env`、Xray 环境变量及各模块配置。
- [开发与启动](docs/development.md) — 本地开发、Docker 启动、测试和调试。

### 部署、凭据与迁移

- [AI 节点部署与 SSH 纳管](docs/ai-node-deployment.md) — 独立 AI 数据面的部署和控制面纳管。
- [AI 节点独立凭据](docs/ai-node-credentials.md) — AI inbound/outbound 凭据边界和轮换要求。
- [Cloudflare Access 邮箱登录](docs/cloudflare-access-email-login.md) — 控制面 Email OTP 登录、Access 策略和源站边界。
- [内网 SSH 纳管](docs/ssh-key-access.md) — 控制面直连普通数据面的认证、主机指纹校验与验证。
- [面板迁移](docs/panel-migration.md) — 控制面数据、配置和服务迁移流程。
- [AWS 普通数据面迁移与回退](docs/aws-normal-data-plane-migration.md) — 普通数据面灰度迁移、AWS 安全组门禁和回退步骤。

### 运行、接口与故障处理

- [运维与排障](docs/operations.md) — 健康检查、监控、备份和常见故障处理。
- [API 与页面路径](docs/api.md) — 页面、认证、订阅接口和 JSON API。
- [Fluent Bit 日志采集](docs/logging-fluent-bit.md) — 三节点 Docker/Xray 关键日志经 Tailscale 到远端 Loki 和 Grafana。
- [三节点故障容错](docs/fault-tolerance.md) — 控制面、普通数据面和 AI 数据面的故障边界。
- [DNS 故障切换](docs/dns-failover.md) — 探测、Cloudflare DNS 切换、备用 Xray 和自动回切。
- [ChatGPT 路由排障](docs/chatgpt-routing-troubleshooting.md) — 客户端、入口、路由、AI 节点和出口的分层排查。
- [Clash REALITY 健康检查超时排障](docs/troubleshooting/clash-reality-health-check-timeout.md) — TCP 可达但完整 REALITY 握手失败时的分层诊断、修复和验收记录。

### AI 路由与备份

- [AI 路由](docs/ai-routing.md) — 域名分类、动态规则、AI 上游选择和故障回退。
- [灾备归档与 R2 上传通道](docs/disaster-backup.md) — 配置文件等额外内容的归档、R2 异地保留和离线恢复边界。
- [远端节点配置采集](docs/remote-node-backup.md) — 通过严格只读 SSH 采集普通数据面实际配置；本机 AI 配置随控制面归档。
- [节点备份完整性与快速恢复](docs/node-recovery.md) — 节点必需材料校验和可直接启动的替换目录。
- [Cloudflare R2 灾备上传](docs/db-backup-uploader.md) — 加密上传、对象命名、安全边界和人工恢复。

### Prometheus-only 运维分析

- [模块首页](docs/ops-reporting/index.md) — 模块边界、生产状态和子模块导航。
- [Exporter 部署与网络隔离](docs/ops-reporting/exporter-deployment.md) — Node Exporter、cAdvisor 和网络访问边界。
- [Prometheus Targets 与 Labels](docs/ops-reporting/prometheus-targets.md) — 抓取目标、标签及查询约束。
- [故障判定规则边界](docs/ops-reporting/fault-classification.md) — 可判定能力、证据组合和 unknown 边界。
- [每日日报器](docs/ops-reporting/daily-reporter.md) — 日报生成流程和职责边界。
- [报告契约](docs/ops-reporting/report-contract.md) — 报告结构、字段和输出约束。
- [报告运行审计与历史归档](docs/ops-reporting/report-run-audit.md) — SQLite 审计记录和保留边界。
- [灰度发布与回滚](docs/ops-reporting/rollout.md) — 分阶段发布、观察期和回滚条件。
- [验收标准](docs/ops-reporting/acceptance.md) — 上线门禁和验收指标。
- [故障排查](docs/ops-reporting/troubleshooting.md) — Target、AI 监控和日报异常处理。

### 历史与停用文档

以下文档用于追溯历史决策，不代表当前推荐部署方式：

- [Reality dest 修复与多端口最终状态](docs/PORT443_PER_USER_MIGRATION.md) — 历史生产修复和验证记录。
- [Prometheus-only 生产部署状态](docs/ops-reporting/deployment.md) — 历史部署状态记录；现行入口见模块首页。
- [SSH 日志采集器停用说明](docs/ops-reporting/log-collector.md) — 已停用方案及迁移背景。

## 安全边界

- 不提交 `.env`、REALITY 私钥、Cloudflare Token、数据库快照或灾备归档；SSH 纳管不需要私钥。
- 控制面与数据面应使用独立主机、独立目录和最小权限凭据。
- Xray 配置必须先渲染和校验，再同步并确认健康检查、探针和监控恢复。
- Node Exporter、cAdvisor、Grafana、Loki、Fluent Bit 和管理接口应限制到受信任网络。
- 数据库备份不能只验证任务成功，还应定期验证实际恢复流程。
