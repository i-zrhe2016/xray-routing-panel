# Repository Current State

Last verified: 2026-09-09 @ working tree

## Current Focus

- AI Routing 第一阶段 Python 拆分与 NodeController 第二阶段均已合并到 `main`；本机默认控制面与数据库备份服务已按 `6c8eef3` 重建并通过健康检查，Xray profile 未启用。
- PanelState 第三阶段依赖收缩已完成：它保留组合根/兼容 facade 角色，域 service 改为持有显式 repository、renderer、node controller、锁和兄弟 service 依赖。
- 应用级 Core 协调器第四阶段已完成：SQLite 连接/状态存储和 schema bootstrap 位于 `app/storage/`，Xray apply 事务位于 `app/xray/apply.py`，节点 fleet helper、Xray stats reader、runtime workers 和 `ApplicationLifecycle` 均已从旧 Core 边界移出。
- 第五阶段已开始：`app/bootstrap.py` 是控制面 composition root，`app/panel.py` 构造并注入 Application，Web factory 不再在 import-time 创建 PanelState；架构边界检查和 Application 分域 API 已加入测试保护。

## Implemented

- 面板、商业履约、维护任务与 AI 域名管理器共用跨进程应用锁，避免 SQLite/运行时配置锁顺序反转。
- Xray 配置、动态路由、订阅、AI 决策和报告使用原子写入；远端生成文件通过唯一临时文件校验后替换。
- 外部 reloader 可校验配置、等待旧进程退出和新进程出现，失败保留待应用标记并重试；面板在该模式不直接重启数据面。
- Compose 注入管理器执行模式、共享数据库路径和外部 reloader 开关。
- 已移除未使用的编排清单和专属部署文档；备份 allowlist 与文档导航仅保留 Compose 入口。
- 强制回退报告不再假设上游 host/port；未管理数据面报告为 `unmanaged`，不执行不存在的重启。
- AI 路由实现已拆分到 `app/xray/ai_routing/`：`runner.py` 负责 CLI/定时任务，`manager.py` 负责编排，其余模块分别负责观测、分类、候选、选择、SQLite 仓储和产物；旧 `app/xray/ai_domain_manager.py` 保留为兼容 facade，并完整保留历史 helper 导出。
- NodeController 已拆分到 `app/xray/node/` 并合并到 `main`：`controller.py` 只负责 backend 选择和编排，`backend.py` 定义节点契约，`ssh.py`、`docker.py`、`local.py` 分别实现传输/进程操作，`probes.py` 与 `files.py` 分离探测和文件同步；T5.6 已删除 `app/xray/node_control.py`，仓库内统一使用 canonical imports。
- `app/state/` 域 service 不再保存 `PanelState` back-reference；`PanelState` 的旧扁平调用面由具体 delegate 保留，已移除通用属性代理。
- `app/storage/sqlite.py` 提供带共享写锁的 `SQLiteDatabase.connect()` 与显式 `transaction()`；`app/storage/schema.py` 只持有通用 `app_state` DDL，端口、流量、探针、AI、DNS 和商业表由各自 service 的幂等 schema hook 创建/迁移，并由生命周期在同一连接上按依赖顺序调用。
- `app/bootstrap.py` 集中创建 `SQLiteDatabase`、`SchemaBootstrap`、NodeController、域 service、runtime worker 和 `ApplicationLifecycle`；`PanelState` 只保留兼容 facade 和显式组件图接收逻辑。
- `Application` 的公开分域入口固定为 `ports`、`traffic`、`probes`、`nodes`、`ai_routing`、`dns_failover`、`commerce`、`diagnostics` 和 `lifecycle`；`NodesService` 负责数据面/AI 节点状态聚合、同步和重启，旧扁平节点方法委托到该服务。
- `app/web/core.py` 的 `create_app(application)` 只消费调用方提供的 Application/State，将同一实例保存到 `flask_app.extensions["application"]`；路由模块在注入后注册，Web 不再负责业务对象构造。
- Dashboard 与 health 的读取路径已开始使用 `application.<domain>.<method>()`；其余 view 继续通过旧扁平 delegate 迁移，避免一次性改动。
- `tests/architecture/` 使用标准库 AST 检查 `xray/storage/runtime` 到 Web/State 的禁止依赖、Web 业务依赖构造，以及 canonical 包与兼容 facade 的方向；不依赖生产 singleton 或网络服务。
- `app/xray/apply.py` 的 `XrayApplyService` 负责数据库变更后的 panel-ports/config render、校验、远端同步、主/备 Xray 重载、提交和失败回滚；`PortsService`、`TrafficService`、`CommerceService` 显式持有它作为 apply collaborator。
- 商业履约通过 `XrayApplyService.apply_lock()` 与 `persist_and_reload_locked()` 接入已有事务，不再访问 apply service 的私有锁/回滚方法。
- `app/xray/node/fleet.py` 提供无状态的多 AI 节点状态聚合、同步和重启 helper；`app/xray/stats.py` 的 `XrayStatsReader` 隔离 `statsquery` 解析，`TrafficService` 直接依赖它。
- `app/xray/ai_routing/launcher.py` 承担 AI domain manager 的本地/容器进程边界；`AiRoutingService` 只通过显式 runner collaborator 触发重算，不再直接调用 subprocess。
- `app/runtime/maintenance.py` 与 `app/runtime/dns_failover.py` 只负责调度、停止事件和异常边界；它们调用应用 service，不包含 SQL、Xray command 或 DNS policy。
- `app/state/lifecycle.py` 的 `ApplicationLifecycle` 负责 schema、端口初始化、流量同步、初始配置 apply、DNS 快照，以及 runtime worker 的启动和停止编排；Web 入口只调用 lifecycle。
- `PanelState` 保留扁平兼容 facade，但所有域 service 直接依赖 `SQLiteDatabase`、Xray apply、节点控制器和 stats reader，不再经过 Core 转发；新的 `Application` 由 `app/bootstrap.py` 返回。
- 本机默认控制面和数据库备份服务已按 `6c8eef3` 重建并启动；面板 `/healthz` 连续三次返回 `ok=true`，控制面容器为 `healthy`，备份容器的 cron 正常运行。Xray profile 当前未启用。

## In Progress

- T5.5 Application facade 公共 API 和 dashboard/health 的首批分域读取已完成；其余 view 的扁平 facade 调用仍在迁移期保留。
- T5.6 已完成：仓库内不再依赖 `node_control.py`，旧导入路径删除策略已由架构测试和文档固定；下一张票据是 T5.7。

## Known Issues / Failing Checks

- 全量 `.venv/bin/python -m pytest -q` 通过：283 passed、1 skipped；唯一跳过项需要设置 `XRAY_TEST_BINARY` 并安装 HAProxy 才能执行真实传输测试。
- NodeController、fleet、stats 与 runtime 聚焦测试均通过；backend 选择覆盖 SSH、local、Docker、unmanaged 和旧控制器别名。
- Phase 4 聚焦回归通过：生命周期、storage、组合、节点控制、DNS、商业和 unified entry 均通过；新增领域 schema hook、SQLite 共享写锁、runtime worker、node fleet、Xray stats 与 AI launcher 测试均通过 Ruff。修改后的 state/storage/web 文件通过 `py_compile` 和 `git diff --check`；`ruff check app/state` 仍报告既有的 `BLE001`、`SIM117` 等规则告警。
- 本机部署使用了 `docker compose up -d --build`；仓库文档引用的 CPU-aware 构建脚本在本机不存在，因此未使用该脚本。

## Constraints

- Python >=3.10，Flask 固定为 2.2.5；SQLite 部署保持单副本。
- 外部 reloader 模式要求 watcher 能访问共享运行卷、面板镜像中的 Xray/procps，以及共享 `/var/log/xray`。

## Architecture Snapshot

- Flask 控制面由 `app/panel.py` 通过 `app/bootstrap.py` 组装 Application，再交给 `app/web/` 的 `create_app(application)`；`app/state/` 服务管理 SQLite，`app/xray/node/` 通过 `NodeController` 选择并编排本地、Docker、SSH 或 unmanaged backend，节点类型统一从 canonical package 导入。
- `Application` 暴露 `ports`、`traffic`、`probes`、`nodes`、`ai_routing`、`dns_failover`、`commerce`、`diagnostics` 和 `lifecycle`；各域 service 通过显式 collaborator 访问数据库、渲染、节点控制、锁和跨域能力，不再通过全局 facade 回溯依赖。领域 schema 初始化也由各 service 持有，`ApplicationLifecycle` 只负责调用顺序。
- AI 管理器与面板通过运行时目录中的应用锁和待应用标记协作；启用外部 reloader 时由 watcher 负责 Xray 进程重载。
- AI Routing 包的模块边界和数据流见 [AI 路由](ai-routing.md) 与 [小时分析流程图](diagrams/ai-hourly-analysis.svg)。
- 报告由管理器写入共享 `app/xray/reports`，面板从同一卷读取；详细流程见 [AI 路由](ai-routing.md)。

## Next

- 进入 T5.7，先确认 `ai_domain_manager.py` 的 CLI、运维脚本和测试调用方，再迁移调用并评估兼容 facade 的最小保留面；如需启用本机 Xray profile，另按完整栈验收流程执行。
