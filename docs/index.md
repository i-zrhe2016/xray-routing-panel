# 文档首页

`docs/` 是项目详细文档的权威目录；根目录的 [README.md](../README.md) 是项目入口、快速开始和完整导航。本页按使用场景组织所有模块，并区分当前文档与历史/停用记录。

## 建议阅读顺序

1. [项目概览](project-overview.md)：了解项目定位、核心能力和最小启动方式。
2. [架构说明](architecture.md)：理解控制面、普通数据面、AI 数据面和组件边界。
3. [配置说明](configuration.md)：配置根 `.env`、Xray 参数、节点和可选组件。
4. [开发与启动](development.md)：本地开发、Docker 启动、测试和调试。
5. [运维与排障](operations.md) / [Clash REALITY 健康检查超时排障](troubleshooting/clash-reality-health-check-timeout.md) / [API 与页面路径](api.md) / [Fluent Bit 日志采集](logging-fluent-bit.md)：运行检查、监控、日志、接口和常见运维动作。
6. [AI 节点部署](ai-node-deployment.md) → [AI 节点独立凭据](ai-node-credentials.md) → [内网 SSH 纳管](ssh-key-access.md)：部署本机 AI 备用或纳管独立远端 AI 数据面。
7. [AI 路由](ai-routing.md) / [ChatGPT 路由排障](chatgpt-routing-troubleshooting.md)：理解 AI 流量链路并执行分层排障。
8. [DNS 故障切换](dns-failover.md) → [三节点故障容错](fault-tolerance.md)：配置切换机制、备用 Xray 和验收边界。
9. [Prometheus-only 运维分析](ops-reporting/index.md)：部署观测组件、生成日报、灰度发布和回滚。
10. [面板迁移](panel-migration.md) / [AWS 普通数据面迁移与回退](aws-normal-data-plane-migration.md) / [灾备归档](disaster-backup.md) / [远端节点配置采集](remote-node-backup.md) / [节点快速恢复](node-recovery.md)：迁移、备份和节点替换。

## 开始使用

- [仓库当前状态](Repo_Current_State.md) — 已核实的实现、验证限制与当前工作状态。
- [项目概览](project-overview.md) — 项目能力、架构摘要和快速开始。
- [架构说明](architecture.md) — 组件职责、数据流、运行产物和边界。
- [配置说明](configuration.md) — 根 `.env`、Xray 环境变量及模块配置。
- [开发与启动](development.md) — 本地开发、Docker 启动、测试和调试。

## 部署、凭据与迁移

- [AI 节点部署与 SSH 纳管](ai-node-deployment.md) — 本机 AI 备用、独立 AI 数据面部署、状态检查和同步保护。
- [AI 节点独立凭据](ai-node-credentials.md) — AI inbound/outbound 凭据契约和轮换边界。
- [Cloudflare Access 邮箱登录](cloudflare-access-email-login.md) — 控制面 Email OTP 登录、Access 策略和源站边界。
- [内网 SSH 纳管](ssh-key-access.md) — 控制面直连普通数据面的认证、主机指纹校验与验证。
- [K3s 部署](kubernetes.md) — Kubernetes 分阶段部署结构和边界。
- [面板迁移](panel-migration.md) — 控制面数据、配置和服务迁移。
- [AWS 普通数据面迁移与回退](aws-normal-data-plane-migration.md) — 普通数据面 AWS 灰度迁移、切换和回退。

## 运行、接口与故障处理

- [运维与排障](operations.md) — 健康检查、Prometheus、Grafana、备份和常见故障。
- [API 与页面路径](api.md) — 页面、认证、订阅接口和 JSON API。
- [Fluent Bit 日志采集](logging-fluent-bit.md) — 三节点 Docker stdout/stderr 到 Fluent Bit、Loki 和 Grafana，含当前生产部署状态与回滚。
- [三节点故障容错](fault-tolerance.md) — 控制面、普通数据面和 AI 数据面故障矩阵。
- [DNS 故障切换](dns-failover.md) — 探测、Cloudflare DNS、备用 Xray 和自动回切。
- [ChatGPT 路由排障](chatgpt-routing-troubleshooting.md) — 客户端、入口、路由、AI 节点和出口排障。
- [Clash REALITY 健康检查超时排障](troubleshooting/clash-reality-health-check-timeout.md) — TCP 可达但完整 REALITY 握手失败时的诊断证据、修复步骤和验收门禁。

## AI 路由与备份

- [AI 路由](ai-routing.md) — 域名分类、动态规则、AI 上游选择和回退。
- [灾备归档与 Cloudflare R2 上传通道](disaster-backup.md) — 配置文件等内容的归档、R2 异地保留和离线恢复边界。
- [节点备份完整性与快速恢复](node-recovery.md) — 节点必需材料、完整性门禁、校验和可直接启动的替换目录。

## Prometheus-only 运维分析

- [模块首页](ops-reporting/index.md) — 模块边界、生产状态和子模块导航。
- [Exporter 部署与网络隔离](ops-reporting/exporter-deployment.md) — Node Exporter、cAdvisor 和网络访问边界。
- [Prometheus Targets 与 Labels](ops-reporting/prometheus-targets.md) — 抓取目标、标签和查询约束。
- [故障判定规则边界](ops-reporting/fault-classification.md) — 可判定能力、证据组合和 unknown 边界。
- [每日日报器](ops-reporting/daily-reporter.md) — 日报生成流程和职责边界。
- [报告契约](ops-reporting/report-contract.md) — 报告结构、字段和输出约束。
- [报告运行审计与历史归档](ops-reporting/report-run-audit.md) — SQLite 审计和保留边界。
- [灰度发布与回滚](ops-reporting/rollout.md) — 分阶段发布、观察期和回滚条件。
- [验收标准](ops-reporting/acceptance.md) — 上线门禁和验收指标。
- [故障排查](ops-reporting/troubleshooting.md) — Target、AI 监控和日报异常处理。

## 历史与停用记录

以下文档用于追溯历史决策，不代表当前推荐部署方式：

- [Reality dest 修复与多端口最终状态](PORT443_PER_USER_MIGRATION.md) — 历史生产修复和验证记录。
- [Prometheus-only 生产部署状态](ops-reporting/deployment.md) — 历史部署状态记录；现行入口见模块首页。
- [SSH 日志采集器停用说明](ops-reporting/log-collector.md) — 已停用方案及迁移背景。

## 文档维护约定

- 新增功能时，在 `docs/` 新建只关注一个模块的文档，并在本页和根 README 同步添加入口。
- 详细配置、状态机、API 字段和排障步骤放在对应模块文档；README 只保留摘要和导航。
- 历史方案不删除，需在标题或导航中标记状态，避免与现行部署指南混淆。
