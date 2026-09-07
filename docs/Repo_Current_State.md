# Repository Current State

Last verified: 2026-09-07 @ working tree

## Current Focus

- AI 路由主要 review findings 已修复并部署到控制面 Docker Compose；K3s 完整栈仍未部署。

## Implemented

- 面板、商业履约、维护任务与 AI 域名管理器共用跨进程应用锁，避免 SQLite/运行时配置锁顺序反转。
- Xray 配置、动态路由、订阅、AI 决策和报告使用原子写入；远端生成文件通过唯一临时文件校验后替换。
- Kubernetes 外部 reloader 校验配置、等待旧进程退出和新进程出现，失败保留待应用标记并重试；面板在该模式不直接重启数据面。
- Kubernetes 面板挂载共享报告目录；Compose 注入管理器执行模式、共享数据库路径和外部 reloader 开关。
- 强制回退报告不再假设上游 host/port；未管理数据面报告为 `unmanaged`，不执行不存在的重启。

## In Progress

- None

## Known Issues / Failing Checks

- 受影响定向回归通过：管理器、节点控制、商业履约共 68 项；本地 Xray、节点恢复、备用周期和渲染共 29 项。`py_compile`、Compose 配置、Kubernetes YAML 和 reloader shell 语法检查通过。
- 全量 `unittest discover` 在当前容器不干净：6 个 pytest 测试因镜像未安装 pytest 无法导入，另有 Google/本地 socket/管理员凭据相关环境失败；需在完整 CI 凭据与依赖环境复核。
- Open Code Review CLI preview 可生成变更清单，但实际 review 因未配置 `OCR_LLM_URL/OCR_LLM_TOKEN/OCR_LLM_MODEL`（或等效 Anthropic 配置）未执行。
- 当前控制面的 `xray-routing-panel` 已重建并健康运行，`/healthz` 返回 `ok=true`；本次未启动 Compose 的 Xray profile。
- 尚未在 K3s 节点验证 sidecar 实际滚动重载；K3s 运行状态为 Unverified。

## Constraints

- Python >=3.10，Flask 固定为 2.2.5；SQLite 部署保持单副本。
- 外部 reloader 模式要求共享运行卷、`shareProcessNamespace`、面板镜像中的 Xray/procps，以及共享 `/var/log/xray`。

## Architecture Snapshot

- Flask 控制面通过 `app/state/` 服务管理 SQLite，`app/xray/node_control.py` 管理本地、Docker 或 SSH 数据面。
- AI 管理器与面板通过运行时目录中的应用锁和待应用标记协作；Kubernetes `xray-reloader` 负责同 Pod Xray 进程重载。
- 报告由管理器写入共享 `app/xray/reports`，面板从同一卷读取；详细流程见 [AI 路由](ai-routing.md) 与 [K3s 部署](kubernetes.md)。

## Next

- 在具备 K3s 测试节点后执行 sidecar 滚动重载验收；在完整 CI 依赖与凭据环境复核全量测试。
