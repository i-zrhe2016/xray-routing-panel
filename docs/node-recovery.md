# 节点备份完整性与快速恢复

本模块把灾备归档变成可验证的节点恢复包。当前支持两类可替换节点：普通数据面和 AI 数据面。恢复准备只在新主机本地写入文件，不会通过 SSH 修改旧节点。

![节点快速恢复流程](diagrams/node-recovery-flow.svg)

[PlantUML 源文件](diagrams/node-recovery-flow.puml)

## 备份覆盖范围

每个新的 `*-disaster-*.tar.gz` 都包含：

| 范围 | 必需内容 | 可选内容 |
| --- | --- | --- |
| 共享控制面状态 | 一致性 `panel.db` 快照、节点恢复清单 | `ops.db`、`data/uploads`、报告和运行时文件 |
| 普通数据面 | 远端实际 `config.json`、远端 `.env` | `panel-ports.json`、`dynamic-routing.json`、客户端测试产物、最新 AI 报告 |
| AI 数据面 | 远端模式：远端 `config.json` + `.env`；本机 Docker 模式：控制面 `config-ai-node.json` + `.env` | — |

普通数据面默认通过内网直连的严格只读 SSH 采集，AI 节点有远端目标时同样采集；本机 Docker AI 节点直接读取控制面运行时目录。SSH 登录私钥、known_hosts、部署 Secret 和 R2 密钥不进入归档，必须放在独立的 Secret 管理位置。

默认远端路径如下；部署目录不同时必须显式设置 `DB_BACKUP_DATAPLANE_REMOTE_PATHS`：

```text
/root/xray-routing-panel/app/xray/runtime/config.json
/root/xray-routing-panel/app/xray/.env
/root/xray-routing-panel/app/xray/runtime/panel-ports.json
/root/xray-routing-panel/app/xray/runtime/dynamic-routing.json
/root/xray-routing-panel/app/xray/runtime/client-test.json
/root/xray-routing-panel/app/xray/runtime/client-share.txt
/root/xray-routing-panel/app/xray/reports/hourly-domains/latest.json
```

`config.json` 和 `.env` 是节点恢复的必需文件；其余路径缺失不会掩盖必需文件缺失，而会在清单中标记为可选缺失。

## 完整性状态

归档根部的两个清单职责不同：

- `backup-manifest.json`：覆盖归档内每个文件的大小和 SHA-256。
- `node-recovery-manifest.json`：按节点列出来源、目标恢复路径、必需/可选文件和 `recoveryReady`。

备份任务完成后会在本地写出 `node-recovery-status.json`。`recoveryReady=true` 的含义是：共享 `panel.db` 存在，且当前已配置节点的必需配置和 `.env` 都已采集并通过哈希校验。

节点暂时失联时，默认仍保留控制面数据库备份，但状态会明确显示该版本不能作为完整节点恢复包。可用最近一个 `recoveryReady=true` 的归档恢复；需要把完整性作为备份门禁时设置：

```dotenv
DB_BACKUP_RECOVERY_REQUIRED=1
```

这会在生成归档并校验后阻止该不完整版本继续上传。节点失联时不要删除此前完整归档。

## 校验归档

在控制面或隔离恢复机上执行，不会写入新节点：

```bash
python3 scripts/node_recovery.py validate \
  --bundle /backups/xray-routing-panel-disaster-20260829T030000Z.tar.gz \
  --require-ready \
  --json
```

命令会重新读取归档内文件，校验 `size` 和 SHA-256，并检查节点恢复清单引用的每个文件。任何篡改、缺失或路径穿越都会直接失败。

## 新建节点并恢复

新主机只需要先准备 Docker、网络/防火墙和 Xray 镜像拉取能力。恢复普通数据面：

```bash
python3 scripts/node_recovery.py prepare \
  --bundle /backups/xray-routing-panel-disaster-20260829T030000Z.tar.gz \
  --node normal-data-plane \
  --output-dir /root/xray-routing-panel

cd /root/xray-routing-panel
docker compose -f docker-compose.node.yml up -d
docker compose -f docker-compose.node.yml ps
```

恢复 AI 数据面使用 `--node ai-data-plane` 和一个新的空目录。`prepare` 会生成标准目录、权限为 `0600` 的配置/`.env`、独立的 `docker-compose.node.yml` 和 `node-recovery.json`；默认拒绝向非空目录写入。只有明确确认目标内容后才使用 `--force`。

恢复完成后按顺序执行：

1. 用 `docker compose -f docker-compose.node.yml logs` 和 Xray 配置测试确认服务健康。
2. 将新主机加入内网，确认 SSH 密码/键盘交互认证可用，并人工核对后更新 known_hosts。
3. 在控制面更新对应的 `DATAPLANE_SSH_TARGET` 或 `AI_NODE_SSH_TARGET`、远端配置路径和探测地址；AI 节点还要确认公网地址/端口与 `AI_UPSTREAM_*` 一致。
4. 先做配置同步/探针/业务连接验证，再切换 DNS 或恢复流量。

归档不携带 SSH 私钥、主机密钥、云主机创建凭据和防火墙规则，因此这些基础设施步骤不能由恢复命令静默代替。节点恢复命令只负责把经过校验的业务配置快速落盘并启动 Xray。

## 生产验收

至少每个保留周期执行一次完整演练：

```bash
python3 scripts/node_recovery.py validate --bundle <bundle> --require-ready
python3 scripts/node_recovery.py prepare --bundle <bundle> --node normal-data-plane --output-dir /tmp/xray-node-restore
docker compose -f /tmp/xray-node-restore/docker-compose.node.yml config
```

演练结束后删除隔离目录；不要把包含 `.env` 的恢复目录提交 Git 或复制到公开位置。
