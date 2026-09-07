# Fluent Bit 日志采集

本模块按控制面、普通数据面和 AI 备用三个角色采集 Docker 主机日志和关键错误日志，经 Tailscale 发送到远端 Loki，再由控制面 Grafana 查询。当前 AI 备用与控制面共用主机，不额外启动一台日志主机。

![Fluent Bit log collection](diagrams/logging-fluent-bit.svg)

[PlantUML 源文件](diagrams/logging-fluent-bit.puml)

## 组件边界

| 组件 | 部署位置 | 职责 |
| --- | --- | --- |
| Fluent Bit Agent | 三台 Docker 主机 | 读取 Docker JSON 日志、Xray `error.log`/`ai-error.log`、allowlist 内的 systemd 错误日志；本地 filesystem 缓冲；通过 Tailscale 推送 |
| Loki | 独立日志中心主机 | 单实例 filesystem 存储、LogQL 查询、7 天保留 |
| Grafana | 控制面监控主机 | 通过 `GRAFANA_LOKI_URL` 查询远端 Loki |
| Tailscale | 四个主机端点 | 提供 Agent 到 Loki、Grafana 到 Loki 的 tailnet 网络边界 |

首期只支持 Docker Compose 节点。日志采集不进入用户代理流量路径，也不依赖控制面业务进程。控制面业务日志只写 stdout/stderr，不新增 SQLite 审计表；Loki 保持当前 7 天（168 小时）保留策略。

## 当前生产部署

截至 **2026-08-20**，控制面和普通数据面日志链路已完成部署；AI 备用复用控制面主机的监控链路。控制面业务容器已加载 `app/observability/logging.py`，业务 JSON 经控制面 Fluent Bit 转发到 Loki；普通数据面和 AI 备用不运行控制面业务模块。

| 节点 | Tailscale 地址 | Fluent Bit 角色 | Agent 配置目录 | Xray 日志目录 |
| --- | --- | --- | --- | --- |
| 控制面 / AI 备用 | `redacted-ip-004` | `control_plane` / `ai_data_plane` | `/root/xray-routing-panel/monitoring/fluent-bit` | `/root/ai-routing-panel/app/xray/logs` |
| 普通数据面 | `redacted-ip-003` | `normal_data_plane` | `/root/xray-fluent-bit` | `/root/xray-routing-panel/app/xray/logs` |
| AI 备用 | `redacted-ip-004` | `ai_data_plane` | 控制面监控栈 | `/root/ai-routing-panel/app/xray/logs` |

控制面 Loki 绑定 `redacted-ip-004:3100`，Grafana 使用现有本机 `3001` 入口。控制面和普通数据面 Agent 使用相同的 parser 和低基数 label 配置，原配置会在滚动更新前保留为带时间戳的 `.bak` 文件。

当前验收结果：控制面 `/healthz` 返回 `ok=true` 且数据面可达；Loki 可按 `category="business"` 查询到 `dns_failover.checked` 业务事件；Grafana 的 `Control Plane Business Logs` dashboard 已加载。AI 备用的 `ai-error.log` 纳入控制面 Agent 的采集范围；高频 `ai-access.log` 只保留在本机，不进入 Loki。

## 控制面业务日志

控制面使用单行 JSON 协议输出业务事件和必要的 HTTP 请求事件。请求成功的普通 `GET` 不记录；所有写请求、慢请求（默认 `PANEL_SLOW_REQUEST_MS=1000` 毫秒）以及 `4xx/5xx` 请求记录。每个响应都返回 `X-Request-ID`：合法入站值透传，缺失、超长或包含非法字符时生成新的随机 ID。

业务事件覆盖管理员、客户、租户、订单、端口、订阅、DNS 故障切换、节点控制、备份、维护、探针和 AI 路由。`actor_id` 只使用管理员/客户内部 ID 或端口 ID，不记录邮箱、密码、租户 token、订阅 token、Cookie、Authorization、CSRF、支付凭证内容、请求 query 和 body。异常 stacktrace 会作为 JSON 字符串写入，不会破坏单行格式。

运行时配置在控制面 `.env`：

```env
PANEL_LOG_LEVEL=INFO
PANEL_SLOW_REQUEST_MS=1000
```

日志写入失败不会影响业务响应；Fluent Bit filesystem buffer 和 Loki 重试负责短时不可用场景。Agent 达到磁盘上限时允许有界丢失，本方案不提供超过 Loki 7 天保留期的合规审计保证。

## 日志范围

采集：

- `/var/lib/docker/containers/*/*-json.log` 中的容器 stdout/stderr；
- 配置目录映射到 `/var/log/xray/error.log` 和 `/var/log/xray/ai-error.log` 的 Xray 错误日志；
- `docker.service`、`containerd.service`、`tailscaled.service` 和可选 `xray.service` 的 journal 错误级别日志。

明确不采集：

- Xray `access.log`；
- Xray `ai-access.log`（仅本机用于域名/端口分析，不进入集中日志）；
- 请求 body、数据库、`.env`、REALITY 私钥、SSH 私钥、Cloudflare Token；
- 未列入 allowlist 的 systemd unit；
- 客户端订阅 token、Authorization header 和其他敏感业务字段。

应用和 Xray 仍然不得主动把密钥写入 stdout/stderr 或 `error.log`；Fluent Bit 的输入 allowlist 不是敏感信息治理替代品。

## 部署

### 1. 日志中心

在加入 Tailscale 的日志中心主机上：

```bash
cd monitoring/loki
cp .env.example .env
```

编辑 `.env`：

```env
LOKI_TAILNET_BIND_ADDRESS=100.x.y.z
LOKI_HTTP_PORT=3100
```

启动：

```bash
docker compose up -d
curl -fsS http://100.x.y.z:3100/ready
```

`LOKI_TAILNET_BIND_ADDRESS` 必须是日志中心主机的 Tailscale 地址。Loki 不绑定公网网卡；首期使用单实例和专用 Docker volume `loki-data`，保留周期为 168 小时。

### 2. Fluent Bit Agent

在三台 Docker 主机分别执行：

```bash
cd monitoring/fluent-bit
cp .env.example .env
```

按主机修改：

```env
FLUENT_BIT_NODE_ROLE=control_plane
FLUENT_BIT_HOST=control-plane
FLUENT_BIT_LOKI_HOST=loki.tailnet.example
FLUENT_BIT_XRAY_LOG_DIR=/root/ai-routing-panel/app/xray/logs
FLUENT_BIT_STORAGE_LIMIT=2G
```

角色只能使用以下值：

- `control_plane`
- `normal_data_plane`
- `ai_data_plane`

`FLUENT_BIT_XRAY_LOG_DIR` 是宿主机目录。普通数据面和 AI 数据面使用其实际 Xray 日志目录，不要把 `/var/log/xray` 容器内路径直接当作宿主机路径。

启动和查看状态：

```bash
docker compose -f docker-compose.agent.yml up -d
docker compose -f docker-compose.agent.yml ps
docker compose -f docker-compose.agent.yml logs -f fluent-bit
```

### 3. Grafana

在控制面 `monitoring/.env` 设置：

```env
GRAFANA_ADMIN_PASSWORD=replace-with-a-long-random-password
GRAFANA_LOKI_URL=http://loki.tailnet.example:3100
```

启动 Grafana 和 Prometheus：

```bash
cd monitoring
docker compose -f docker-compose.monitoring.yml up -d prometheus grafana
```

Grafana 通过 proxy 模式访问 Loki，浏览器不需要直接访问 Loki 端口。

### 4. 已部署环境检查

在控制面执行：

```bash
curl -fsS http://redacted-ip-007:18080/healthz
curl -fsS http://redacted-ip-004:3100/ready
curl -fsS http://redacted-ip-007:2020/api/v1/metrics
```

查询控制面业务日志：

```bash
curl -G http://redacted-ip-004:3100/loki/api/v1/query_range \
  --data-urlencode 'query={job="platform-logs",node_role="control_plane",category="business"}' \
  --data-urlencode 'limit=100'
```

Grafana 中打开 `Control Plane Business Logs` dashboard，或在 Explore 使用相同 LogQL。不要在命令行、文档或工单中记录 Grafana、面板、Cloudflare 或 SSH 凭据。

## Tailscale 网络边界

建议给四个端点配置明确标签：

```text
tag:log-agent  = control-plane / normal-data-plane / ai-data-plane
tag:log-store  = Loki 日志中心
```

可合并使用 [Tailscale ACL 示例](tailscale-log-acl.hujson.example)。该文件只提供策略模板，不会自动修改 tailnet policy。

最小访问关系：

```text
tag:log-agent  -> tag:log-store:3100
Grafana host   -> tag:log-store:3100
运维终端       -> tag:log-store:3100（按需）
```

不要开放公网 `3100`。Loki 的 `auth_enabled` 首期保持关闭，安全边界由 Tailscale ACL 和主机防火墙提供；如果未来日志中心跨出 tailnet，再增加 TLS 和 Loki 认证，不在 Agent 配置中提交密钥。

## 标签与查询

固定标签：

| 标签 | 取值 |
| --- | --- |
| `job` | `platform-logs` |
| `node_role` | 三种节点角色之一 |
| `host` | `.env` 中配置的稳定主机名 |
| `category` | `business`、`request`、`runtime` 等低基数字段 |
| `level` | `info`、`warning`、`error` 等低基数字段 |
| `source` | `docker`、`xray_error`、`systemd` |

容器 ID、路径、`event`、`request_id`、订单号和用户 ID 保留在 JSON 日志体中，不作为 Loki label，避免高基数。

常用 LogQL：

```logql
{job="platform-logs"}
```

```logql
{job="platform-logs",node_role="control_plane"}
```

```logql
{job="platform-logs",node_role="control_plane",category="business"} | json
```

前端请求错误记录为 `event="frontend.fetch_failed"`，Vue/浏览器运行时错误记录为 `event="frontend.runtime_error"`；两者都包含接口路径、HTTP 方法、状态码、错误来源和脱敏堆栈：

```logql
{job="platform-logs",node_role="control_plane",category="business"} | json | event="frontend.fetch_failed"
```

```logql
{job="platform-logs",node_role="control_plane",category="business"} | json | event="frontend.runtime_error"
```

```logql
{job="platform-logs",node_role="control_plane"} | json | event=~"auth\\..*|port\\..*|order\\..*|dns_failover\\..*"
```

```logql
{job="platform-logs",node_role="control_plane"} | json | level="error"
```

```logql
{job="platform-logs",source="xray_error"} |= "error"
```

Grafana 已预置 `Control Plane Business Logs` dashboard；也可以在 Explore 中使用上述查询按 `category`、`level` 和 JSON 字段 `event` 筛选。

## 缓冲和故障语义

- 每个 Agent 使用 `/buffers` filesystem storage 保存采集状态和待发送数据。
- `FLUENT_BIT_STORAGE_LIMIT` 默认 `2G`，通过 Loki output 的 `storage.total_limit_size` 限制待发送队列。
- Loki 或 Tailscale 暂时不可用时，Agent 重试并继续写本地队列；业务容器不需要切换 logging driver，也不会因日志中心不可达而被阻塞。
- 队列达到上限后允许有界丢失，必须通过 Fluent Bit metrics、容器日志和磁盘监控发现。
- Loki 恢复后，未超出保留期且仍在本地队列中的日志应自动补发。

## 排障

Agent 健康检查：

```bash
curl -s http://redacted-ip-007:2020/api/v1/metrics
docker compose -f docker-compose.agent.yml logs --tail=200 fluent-bit
```

检查 Tailscale 到 Loki：

```bash
tailscale ping loki.tailnet.example
curl -fsS http://loki.tailnet.example:3100/ready
```

如果 Grafana 查不到日志，按顺序检查：

1. Loki 绑定地址是日志中心的 Tailscale 地址，而不是 `redacted-ip-007`。
2. Tailscale ACL 允许对应 Agent 到 `tag:log-store:3100`。
3. Agent `.env` 的 `FLUENT_BIT_LOKI_HOST`、节点角色和 Xray 日志目录正确。
4. `/var/lib/docker/containers` 和 Xray 日志目录以只读方式挂载成功。
5. Grafana 的 `GRAFANA_LOKI_URL` 使用日志中心 Tailscale 地址。
6. 先查询 `{job="platform-logs"}`，再按 `node_role` 和 `source` 缩小范围。

## 变更验收

1. 三台主机各写入一条测试 stdout/stderr，Grafana 能按 `host` 和 `node_role` 查到。
2. 写入 Xray `error.log` 测试行，能查到 `source="xray_error"`。
3. 写入 `access.log` 测试行，Loki 中不存在该行。
4. 停止 Loki，确认 Agent 本地队列增长且业务容器保持运行；恢复后确认日志补发。
5. 阻断 Tailscale ACL，确认投递失败进入重试和缓冲。
6. 重启容器、轮转 Docker 日志和重启 Agent，确认无持续重复采集。
7. 检查 Loki 数据目录、Agent buffer 目录和所有日志容器均未暴露公网监听。

生产部署已完成后的最小验收门槛：

1. `xray-routing-panel`、`loki`、`grafana`、`fluent-bit-agent` 容器均为运行状态。
2. 控制面健康检查返回 `ok=true`，且响应包含 `X-Request-ID`。
3. Loki 的业务查询至少返回一条 `category="business"` 日志。
4. 普通数据面查询能看到 `node_role="normal_data_plane"`；AI 数据面无日志源时只要求 Agent 无投递错误。
5. 业务日志中不出现密码、Authorization、Cookie、CSRF、租户 token 或订阅 token。

## 回滚

控制面镜像更新前会保留旧镜像标签。回滚前先确认当前容器和数据目录状态，再执行：

```bash
cd /root/xray-routing-panel
docker tag xray-routing-panel-xray-routing-panel:pre-business-logs-20260820 \
  xray-routing-panel-xray-routing-panel:latest
docker compose up -d --no-build xray-routing-panel
docker compose ps xray-routing-panel
```

Fluent Bit 回滚使用对应主机配置目录中的最新 `.bak.<timestamp>` 文件，恢复 `fluent-bit.conf` 和 `parsers.conf` 后重启 `fluent-bit-agent`。回滚不删除 Loki 数据卷，也不回滚 SQLite 或 Xray 业务数据。
