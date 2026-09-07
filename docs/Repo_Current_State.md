# Repository Current State

Last verified: 2026-09-07 @ working tree

## Current Focus

- AI 路由应用与失败恢复修复；代码和本地回归验证已完成，尚未部署。

## Implemented

- 商业订阅到期后自动停用并触发配置更新，保留服务记录与订阅凭据供续费；不影响其他有效账号。
- 数据面重载返回失败时阻止业务提交；数据库提交异常进入配置补偿路径。
- 失败时恢复原配置，并对已尝试重载的数据面重新加载原配置；远端同步或回滚重载失败记录 `node.data_plane.rollback_failed`。
- 维护规则及补偿边界见 [运维与排障](operations.md#自动维护规则)。
- 强制回退经管理器重新生成完整配置；待应用标记确保失败后在文件未变化时仍重试，成功后清除。
- 备用节点重启已尝试但返回失败时也补偿重载原配置；见 [AI 路由](ai-routing.md#输入与输出)。

## In Progress

- None（本次修复的实现和本地验证已完成）。

## Known Issues / Failing Checks

- 完整测试集为 229 passed、1 skipped；真实传输测试需要设置 `XRAY_TEST_BINARY` 并安装 HAProxy，当前未验证真实节点行为。
- 涉及文件存在既有 Ruff 告警，本次修改未新增；固定版本 Werkzeug 在 Python 3.12 下产生弃用警告。

## Constraints

- Python >=3.10，Flask 固定为 2.2.5；依赖定义见 `pyproject.toml`。
- 本地测试模拟节点和失败条件，不能代表生产部署验收；生产运行状态为 Unverified。

## Architecture Snapshot

- Flask 控制面通过 `app/state/` 业务服务管理 SQLite 状态，`app/xray/node_control.py` 负责数据面操作。
- 普通数据面承载代理流量，AI 路由子系统维护 AI 上游选择；模块介绍见 [架构说明](architecture.md)，其中生产拓扑未在本次核实。

## Next

- None（尚无已指定的后续任务）。
