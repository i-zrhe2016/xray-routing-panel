# Repository Current State

Last verified: 2026-09-08 @ 34b99c7

## Current Focus

- AI Routing 第一阶段 Python 拆分与 NodeController 第二阶段均已合并到 `main`；本机默认 Docker Compose 栈仍运行上一版已验证部署。

## Implemented

- 面板、商业履约、维护任务与 AI 域名管理器共用跨进程应用锁，避免 SQLite/运行时配置锁顺序反转。
- Xray 配置、动态路由、订阅、AI 决策和报告使用原子写入；远端生成文件通过唯一临时文件校验后替换。
- 外部 reloader 可校验配置、等待旧进程退出和新进程出现，失败保留待应用标记并重试；面板在该模式不直接重启数据面。
- Compose 注入管理器执行模式、共享数据库路径和外部 reloader 开关。
- 已移除未使用的编排清单和专属部署文档；备份 allowlist 与文档导航仅保留 Compose 入口。
- 强制回退报告不再假设上游 host/port；未管理数据面报告为 `unmanaged`，不执行不存在的重启。
- AI 路由实现已拆分到 `app/xray/ai_routing/`：`runner.py` 负责 CLI/定时任务，`manager.py` 负责编排，其余模块分别负责观测、分类、候选、选择、SQLite 仓储和产物；旧 `app/xray/ai_domain_manager.py` 保留为兼容 facade，并完整保留历史 helper 导出。
- NodeController 已拆分到 `app/xray/node/` 并合并到 `main`：`controller.py` 只负责 backend 选择和编排，`backend.py` 定义节点契约，`ssh.py`、`docker.py`、`local.py` 分别实现传输/进程操作，`probes.py` 与 `files.py` 分离探测和文件同步；旧 `app/xray/node_control.py` 保留为兼容 facade。
- 本机默认控制面和数据库备份服务已按 `62e52a8` 重建并启动；面板 `/healthz` 返回 `ok=true`，控制面容器为 `healthy`。Xray profile 当前未启用。

## In Progress

- None

## Known Issues / Failing Checks

- 全量 `.venv/bin/python -m pytest -q` 通过：246 passed、1 skipped；唯一跳过项需要设置 `XRAY_TEST_BINARY` 并安装 HAProxy 才能执行真实传输测试。
- NodeController 聚焦测试通过：37 passed；新增 backend 选择测试覆盖 SSH、local、Docker、unmanaged 和旧控制器别名。
- 新增 `app/xray/node/` 已通过 Ruff、Black、`py_compile`、导入 smoke check 与 `git diff --check`。原生 `codex review --uncommitted` 已启动但因持续扫描生成资源未进入结论；其 sandbox 全量测试受 socket 权限限制，工作区全量测试结果以上述 246 passed 为准。
- 本机部署使用了 `docker compose up -d --build`；仓库文档引用的 CPU-aware 构建脚本在本机不存在，因此未使用该脚本。

## Constraints

- Python >=3.10，Flask 固定为 2.2.5；SQLite 部署保持单副本。
- 外部 reloader 模式要求 watcher 能访问共享运行卷、面板镜像中的 Xray/procps，以及共享 `/var/log/xray`。

## Architecture Snapshot

- Flask 控制面通过 `app/state/` 服务管理 SQLite，`app/xray/node/` 通过 `NodeController` 选择并编排本地、Docker、SSH 或 unmanaged backend；`node_control.py` 仅保留兼容导出。
- AI 管理器与面板通过运行时目录中的应用锁和待应用标记协作；启用外部 reloader 时由 watcher 负责 Xray 进程重载。
- AI Routing 包的模块边界和数据流见 [AI 路由](ai-routing.md) 与 [小时分析流程图](diagrams/ai-hourly-analysis.svg)。
- 报告由管理器写入共享 `app/xray/reports`，面板从同一卷读取；详细流程见 [AI 路由](ai-routing.md)。

## Next

- 按需重建本机 Compose 栈。
