# 配置说明

- 配置入口
  - 根目录 `.env`：面板地址、管理员认证、AI 路由开关、远端数据面接入参数、AI 节点纳管参数
  - `app/xray/.env`：REALITY 基础参数、AI 上游、分类器、MCP 和 Xray 渲染参数

仓库根目录的 `.env.example` 只覆盖高频项；`docker-compose.yml` 里还会注入一批固定运行时默认值。

## 根 `.env` 常用变量

| 变量 | 说明 |
| --- | --- |
| `PANEL_PUBLIC_URL` | 面板对外地址；影响订阅链接和安全 Cookie |
| `PANEL_USERNAME` / `PANEL_PASSWORD` | 管理员认证；任一设置后首页、探针页和 `/api/*` 都要求登录 |
| `PANEL_SECRET_KEY` | Session 签名密钥；不设置则每次启动随机生成 |
| `PANEL_LOG_LEVEL` | 控制面 JSON 日志最低级别，默认 `INFO` |
| `PANEL_SLOW_REQUEST_MS` | 慢请求阈值（毫秒），默认 `1000`；普通成功 GET 仍不记录 |
| `METRICS_TOKEN` | Prometheus `/metrics` 抓取令牌；不设置则 `/metrics` 返回 404，设置后需 `Authorization: X <token>` |
| `METRICS_DP_TTL` | `/metrics` 缓存数据面存活检测的秒数，默认 `30`（抓取路径上唯一的 SSH 调用） |
| `GRAFANA_PUBLIC_URL` | 生产统一使用 `https://xray.zrhe2016.cc/grafana/`，由 Cloudflare Access 保护；管理后台「监控」标签使用该同源地址 |
| `GRAFANA_OBSERVABILITY_UID` | 「监控」标签内嵌所用 Grafana dashboard 的 UID，默认 `xray-observability` |
| `AI_ROUTING_ENABLED` | 是否展示 AI 路由状态和相关统计 |
| `DATAPLANE_SSH_TARGET` | 远端数据面内网 SSH 目标；默认 `root@100.116.187.106`（Compose） |
| `DATAPLANE_SSH_OPTIONS` | SSH 额外参数，按 shell words 解析；认证固定为密码/键盘交互，禁止注入私钥 |
| `DATAPLANE_SSH_KNOWN_HOSTS` | 数据面主机密钥文件；默认 `/root/.ssh/known_hosts`，严格校验且不接受未知主机 |
| `DATAPLANE_REMOTE_COMMAND_TIMEOUT` | 单次远程 SSH/Docker 命令的控制面超时，默认 `8` 秒；避免数据面失联拖住控制面任务 |
| `DATAPLANE_API_SERVER` | 数据面 Xray API 地址，默认 `redacted-ip-007:10085` |
| `DATAPLANE_CONFIG_PATH` | 远端或本地数据面使用的 `config.json` 路径 |
| `DATAPLANE_DYNAMIC_ROUTING_PATH` | 远端 `dynamic-routing.json` 路径 |
| `DATAPLANE_AI_REPORT_PATH` | 远端 `reports/hourly-domains/latest.json` 路径 |
| `DATAPLANE_PANEL_DB_PATH` | 远端 `panel.db` 路径，用于回传 AI 域名聚合快照 |
| `DATAPLANE_PANEL_PORTS_PATH` | 远端 `panel-ports.json` 路径 |
| `DATAPLANE_ACCESS_LOG_PATH` | 远端 `access.log` 路径；AI 管理器每小时通过 SSH 增量读取；留空且 `DATAPLANE_CONFIG_PATH` 使用 `.../runtime/config.json` 时，自动推导同级 `.../logs/access.log` |
| `DATAPLANE_RESTART_COMMAND` | 远端数据面重启命令 |
| `DATAPLANE_EXTERNAL_RELOADER_ENABLED` | 数据面由外部 watcher（例如 Kubernetes `xray-reloader` sidecar）重载时设为 `1`；默认 `0` |
| `AI_DOMAIN_MANAGER_LOCK_PATH` | AI 管理器跨进程锁路径；默认与 `XRAY_CONFIG_OUT` 同目录的 `.ai-domain-manager.lock` |
| `AI_DOMAIN_MANAGER_MANUAL_LOCK_PATH` | 常驻任务与面板手动切换共享的互斥锁路径；默认与 `XRAY_CONFIG_OUT` 同目录的 `.ai-domain-manager-manual.lock` |
| `AI_DOMAIN_MANAGER_EXECUTION_MODE` | 面板触发管理器的方式：`docker`（默认）或同容器/Pod 内 `local` |
| `DATAPLANE_PROBE_HOST` | TCP 探针连接目标；远端模式下应指向远端入口 IP 或域名 |
| `DB_BACKUP_RECOVERY_REQUIRED` | 节点恢复材料不完整时是否阻止灾备归档继续上传；默认 `0`，完整性状态仍会写入报告 |
| `DB_BACKUP_RECOVERY_STATUS_PATH` | 最近一次节点恢复完整性报告路径 |

AI 上游探测优先从普通数据面执行。若 AI 上游模板或分享链接包含 REALITY SNI，管理器会执行 REALITY 握手；否则回退到 TCP 探测。可通过 `AI_UPSTREAM_PROBE_SERVER_NAME` 为模板显式指定 SNI。

常见但通常不需要手动覆盖的运行时变量：

- `PANEL_PORT`
- `DEFAULT_UPSTREAM_HOST`
- `DEFAULT_UPSTREAM_PORT`
- `SEED_LISTEN_PORT`
- `PROBE_ENABLED`
- `PROBE_INTERVAL`
- `PROBE_TEST_LISTEN_PORT`
- `PANEL_HEALTH_REQUIRES_XRAY`
- `PANEL_INTERNAL_HOSTS`：免管理员登录和 CSRF 的内网 Host 列表，默认包含 `100.112.13.103`

Fluent Bit 日志采集使用 `monitoring/fluent-bit/.env`，远端 Loki 使用 `monitoring/loki/.env`，Grafana 使用 `monitoring/.env` 中的 `GRAFANA_LOKI_URL`。三节点生产路径和实际主机角色见 [Fluent Bit 日志采集](logging-fluent-bit.md#当前生产部署)。

## AI 节点纳管变量

| 变量 | 说明 |
| --- | --- |
| `AI_NODE_SSH_TARGET` | 可选远端 AI 节点 SSH 目标；当前本机备用为空 |
| `AI_NODE_SSH_TARGETS` | 多个远端 AI 节点 SSH 目标，按逗号或换行分隔；与 `AI_NODE_IDS`、`AI_NODE_LABELS` 按顺序对应 |
| `AI_NODE_IDS` | 多节点稳定 ID，按逗号或换行分隔；用于逐节点重启 API 和界面定位 |
| `AI_NODE_LABELS` | 多节点显示名，按逗号或换行分隔 |
| `AI_NODE_SSH_BIN` | SSH 可执行文件；默认 `ssh` |
| `AI_NODE_SSH_OPTIONS` | SSH 额外参数，按 shell words 解析；远端纳管使用内网直连和密码/键盘交互认证 |
| `AI_NODE_SSH_KNOWN_HOSTS` | AI 节点主机密钥文件；默认 `/root/.ssh/known_hosts_ai` |
| `AI_NODE_CONTAINER_NAME` | 本机 AI 备用 Xray 容器名；当前为 `xray-ai-node` |
| `AI_NODE_CONTAINER_NAMES` | 多节点远端容器名，按顺序对应 SSH 目标 |
| `AI_NODE_RESTART_COMMAND` | 自定义重启命令（优先于容器名） |
| `AI_NODE_RESTART_COMMANDS` | 多节点自定义重启命令，按顺序对应；留空时使用容器名执行 `docker restart` |
| `AI_NODE_CONFIG_PATH` | AI 节点真实宿主配置路径；显式留空会禁用配置上传 |
| `AI_NODE_CONFIG_PATHS` | 多节点配置路径；当前多节点纳管建议留空，避免控制面配置覆盖独立节点配置 |
| `AI_NODE_API_SERVER` | AI 节点 Socket 存活检查地址；本机 Docker 生产当前为 `redacted-ip-007:27166` |
| `AI_NODE_API_SERVERS` | 多节点 Socket 存活检查地址，按顺序对应；支持远端回环地址，如 `127.0.0.1:27166` |
| `AI_NODE_METRICS_URL` | 面板读取 AI Xray `/debug/vars` 的地址；本机默认 `http://redacted-ip-007:31097/debug/vars`，只允许回环或受控管理网 |
| `AI_NODE_ACCESS_LOG_PATH` | AI access log 路径；默认 `/app/xray/logs/ai-access.log`，只供控制面做本机域名/端口分析 |
| `AI_NODE_DESTINATION_WINDOW_SECONDS` | AI 域名/端口请求分析窗口，默认 `600` 秒 |
| `AI_NODE_DESTINATION_MAX_LABELS` | 每次展开的高流量域名/端口 Top 数，默认 `50`，用于限制 Prometheus 标签基数 |
| `AI_NODE_PROBE_HOST` | AI 节点可达性探测目标；当前为 `redacted-ip-004` |
| `AI_NODE_PROBE_HOSTS` | 多节点探测目标，按顺序对应；仅用于需要生成分享地址的兼容场景 |

节点备份默认请求普通数据面的配置、`.env`、运行时辅助文件和最新报告；远端部署根可用 `DB_BACKUP_DATAPLANE_DEPLOY_ROOT` / `DB_BACKUP_AI_NODE_DEPLOY_ROOT` 配置。节点备份清单和恢复命令见[节点备份完整性与快速恢复](node-recovery.md)。

说明：

- AI 节点使用独立 REALITY 凭据，不能复用或由普通数据面的 `XRAY_*` 参数覆盖
- `AI_UPSTREAM_HOST` / `AI_UPSTREAM_PORT`（在 `app/xray/.env` 中）定义主数据面 VLESS outbound 的目标，生产为 `nat.qq.pw:27166`
- 当前生产保持 `AI_NODE_CONFIG_PATH=`，由本机 Docker 挂载 `config-ai-node.json`，不通过 SSH 上传
- 详见 [AI 节点部署与 SSH 纳管](ai-node-deployment.md)和 [AI 节点独立凭据](ai-node-credentials.md)

## DNS 故障切换变量

| 变量 | 说明 |
| --- | --- |
| `DNS_FAILOVER_ENABLED` | 是否启用 Cloudflare DNS 故障切换 |
| `DNS_FAILOVER_INTERVAL` | 后台检测周期，默认 `15` 秒 |
| `DNS_FAILOVER_TIMEOUT` | 单次 TCP 探测超时 |
| `DNS_FAILOVER_FAILURE_THRESHOLD` | 连续失败多少次切到备用 |
| `DNS_FAILOVER_RECOVERY_THRESHOLD` | 连续成功多少次回切主数据面 |
| `DNS_FAILOVER_PROBE_HOST` / `DNS_FAILOVER_PROBE_PORT` | 只用于自动切换判定的数据面公网 TCP 探测目标 |
| `CF_API_TOKEN` | Cloudflare API Token，至少需要目标 Zone 的 DNS 编辑权限 |
| `CF_ZONE_ID` | Cloudflare Zone ID |
| `CF_DNS_RECORD_ID` | 要切换的单条 DNS Record ID |
| `CF_DNS_RECORD_TYPE` | 当前支持 `A` / `AAAA` / `CNAME` |
| `CF_DNS_RECORD_NAME` | 记录名，例如 `edge.example.com` |
| `CF_DNS_RECORD_PROXIED` | 是否保持 Cloudflare 代理 |
| `CF_DNS_RECORD_TTL` | 记录 TTL；非代理记录建议 `60` 以尽快生效 |
| `DNS_FAILOVER_PRIMARY_CONTENT` | 主数据面入口 IP 或 CNAME；远端数据面模式必须显式填写，本地模式可留空自动获取 |
| `CONTROL_PLANE_BACKUP_XRAY_ENABLED` | 是否启用"控制面本机公网 IP + 备用 Xray"自动备用模式（relay / 直出双模式） |
| `CONTROL_PLANE_BACKUP_UPSTREAM_URL` | relay 模式的完整 `vless://` 上游 URL；必须受保护并与 AI 节点独立 inbound 凭据完整匹配，不得从普通数据面 `XRAY_*` 盲目派生 |
| `DNS_FAILOVER_BACKUP_CONTENT` | 控制面备用节点 IP 或 CNAME；留空时自动获取控制面本机公网 IP |
| `DNS_FAILOVER_BACKUP_LABEL` | 首页展示用备用节点名称 |
| `DNS_FAILOVER_PEAK_ENABLED` | 是否启用“高峰窗口优先专用节点” |
| `DNS_FAILOVER_PEAK_START` / `DNS_FAILOVER_PEAK_END` | 高峰窗口起止时间，格式 `HH:MM` |
| `DNS_FAILOVER_PEAK_TIMEZONE` | 高峰窗口时区；支持 `Asia/Shanghai` 或 `+08:00` |

说明：

- 当前只支持通过 `CF_DNS_RECORD_ID` 更新单条记录
- 自动切换只看 `DNS_FAILOVER_PROBE_HOST:DNS_FAILOVER_PROBE_PORT`
- DNS 故障切换运行在独立 worker 中，不依赖数据面日志、Xray API、流量统计或配置同步
- 数据面远程命令受 `DATAPLANE_REMOTE_COMMAND_TIMEOUT` 限制；SSH 连接参数仍建议通过 `DATAPLANE_SSH_OPTIONS` 配置连接超时和 keepalive。控制面直接连接 `100.116.187.106:22`，不使用私钥。
- AI 候选故障不触发 DNS 切换：`auto` 模式优先切换到另一候选，全部候选不可达时由 `ai_domain_manager` 回退；数据面故障时 DNS 切到控制面备用，AI 节点健康度决定备用是 relay 还是直出模式
- 若启用高峰窗口，窗口内会把备用/专用节点视为首选目标；窗口外恢复主节点优先
- 如果是本地数据面且 `DNS_FAILOVER_PRIMARY_CONTENT` 留空，控制面会自动获取当前数据面的公网 IP；远端数据面必须显式填写，避免数据面失联时 DNS worker 依赖数据面 SSH
- 如果 `CONTROL_PLANE_BACKUP_XRAY_ENABLED=1` 且 `DNS_FAILOVER_BACKUP_CONTENT` 留空，控制面会自动获取本机公网 IP，适合作为控制面备用 Xray 的 DNS 指向
- 如果 `CONTROL_PLANE_BACKUP_XRAY_ENABLED=0`，则必须显式填写 `DNS_FAILOVER_BACKUP_CONTENT`
- 对 REALITY 这类直连流量，想让 IP 更快生效，优先把 `CF_DNS_RECORD_TTL` 设为 `60`
- 完整 DNS 故障切换机制详见 [dns-failover.md](dns-failover.md)

## 灾备归档与 R2 上传变量

| 变量 | 说明 |
| --- | --- |
| `DB_BACKUP_R2_ENABLED` | 是否在每日本地备份成功后上传；Compose 备份服务默认 `1`，Kubernetes/直接执行脚本需显式启用 |
| `DB_BACKUP_R2_ENDPOINT` | Cloudflare R2 S3 endpoint，必须使用 HTTPS |
| `DB_BACKUP_R2_BUCKET` | R2 bucket 名称 |
| `DB_BACKUP_R2_ACCESS_KEY_ID` / `DB_BACKUP_R2_SECRET_ACCESS_KEY` | R2 S3 凭据，只通过部署环境或 Secret 注入 |
| `DB_BACKUP_R2_REGION` | R2 S3 region，默认 `auto` |
| `DB_BACKUP_R2_PREFIX` | 对象 key 前缀，默认 `xray-routing-panel` |
| `DB_BACKUP_R2_RECORD_PATH` | 本地上传记录，默认 `/backups/r2-upload-record.json` |
| `DB_BACKUP_ENCRYPTION_PASSWORD` | AES-256-GCM 归档密码，必须与 R2 secret 分离保存 |
| `DB_BACKUP_BUNDLE_ENABLED` | 是否生成包含数据库和配置文件的灾备归档；默认 `1` |
| `DB_BACKUP_EXTRA_PATHS` | 逗号/换行分隔的额外文件、目录或 glob |
| `DB_BACKUP_BUNDLE_DIR` / `DB_BACKUP_BUNDLE_KEEP_DAYS` | 本地归档目录和保留天数 |
| `DB_BACKUP_SSH_COLLECTION_ENABLED` | 是否通过只读 SSH 采集普通数据面；本机 AI 备用随控制面运行时目录归档 |

R2 对象不会由备份任务删除；生命周期规则在 Cloudflare 侧配置。恢复时人工下载、解密、校验 manifest，再恢复数据库和配置。

SSH 采集的详细安全边界、`remote-node-collection.json` 字段和只读验证命令见[远端节点配置采集](remote-node-backup.md)。

## `app/xray/.env` 必填 REALITY 参数

以下参数是渲染 `config.json` 的基础输入：

- `XRAY_PUBLIC_HOST`
- `XRAY_CLIENT_UUID`
- `XRAY_REALITY_PRIVATE_KEY`
- `XRAY_REALITY_PUBLIC_KEY`
- `XRAY_REALITY_SHORT_ID`
- `XRAY_SERVER_NAME`
- `XRAY_DEST`

常用补充项：

- `XRAY_LISTEN_PORT`
- `XRAY_PUBLIC_PORT`
- `XRAY_UNIFIED_PORT`（启用统一 Clash 入口时设为 `443`）
- `XRAY_UNIFIED_UUID_SECRET`（统一入口按原端口派生 UUID 的私有密钥，至少 32 字符）
- `XRAY_API_SERVER`
- `XRAY_NODE_TAG`
- `XRAY_LOGLEVEL`

## AI 上游和分类器变量

### AI 上游

- `AI_UPSTREAM_HOST`
- `AI_UPSTREAM_PORT`
- `AI_UPSTREAM_FALLBACK_URL`
- `AI_UPSTREAM_FALLBACKS`
- `AI_UPSTREAMS`
- `AI_UPSTREAM_PROBE_TIMEOUT_SECONDS`
- `PANEL_ROUTE_LISTEN_PORT`

说明：

- `AI_UPSTREAM_HOST` / `AI_UPSTREAM_PORT` 是主上游
- `AI_UPSTREAM_FALLBACKS` 在主上游后追加多个备用上游
- `AI_UPSTREAMS` 直接覆盖完整优先级列表
- `AI_UPSTREAM_FALLBACK_URL` 适合备用上游使用不同 UUID / `pbk` / `sid` / `sni`
- 当前生产候选为主 `nat.qq.pw:27166`、备 `redacted-ip-004:27166`；备用节点使用独立 REALITY 凭据
- 主 AI 上游同样可能使用独立凭据；动态 VLESS outbound 必须与 AI inbound 完整匹配，不能从普通数据面 `XRAY_*` 盲目派生
- 如果全部 AI 上游 TCP 探测都失败，AI 动态路由会撤销，流量回退到主链路
- `AI_NODE_SSH_TARGET` 只启用 SSH 纳管；它不证明节点凭据匹配，也不应自动派生独立 AI 节点的 relay URL

控制台人工切换使用 `POST /api/ai-routing/switch`：`primary` 和 `backup` 固定对应候选，`auto` 恢复自动探测，`forced_fallback` 让 AI 流量回到数据面直出。固定候选不可达时不会自动改选另一候选，而是报告 `manual_target_unreachable`。

### 域名分类器

- `CODEX_CLASSIFIER_ENABLED`
- `CODEX_TIMEOUT_SECONDS`
- `CODEX_MODEL`
- `CODEX_CLI_JS`
- `CODEX_BIN`
- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `OPENAI_BASE_URL`
- `OPENAI_ALLOW_NO_KEY`

如果本机 `codex` 不可用，AI 管理器会回退到 OpenAI 兼容接口。

## 模式相关注意事项

### 远端模式

- `DATAPLANE_SSH_TARGET` 生效后，数据面模式优先变成 `ssh`
- 控制面会先在本地渲染，再通过 SSH 上传产物
- 如果要在首页展示远端 AI 报表，需要补齐 `DATAPLANE_AI_REPORT_PATH` 和 `DATAPLANE_PANEL_DB_PATH`

### 本地二进制模式

- 设置 `DATAPLANE_LOCAL_BIN` 后，模式变成 `local`
- 面板能做配置校验，但不会自动重启该进程

### Docker 模式

- 默认通过 `DATAPLANE_CONTAINER_NAME=xray-reality-local` 管理本地容器
- 如不使用本地容器，可显式清空并改用 `local` 或 `ssh`

### AI 节点模式

- `AI_NODE_SSH_TARGET` 生效后，AI 节点模式为 `ssh`；当前留空时由 `AI_NODE_CONTAINER_NAME=xray-ai-node` 使用本机 Docker 模式
- 多节点使用 `AI_NODE_SSH_TARGETS`；面板按 `AI_NODE_IDS` / `AI_NODE_LABELS` 分别展示状态，并通过 `POST /api/ai-nodes/<node_id>/restart` 单独重启
- `AI_NODE_CONFIG_PATH` 非空时控制面才具备上传 `config-ai-node.json` 的能力；生产当前显式留空以禁止上传
- `AI_NODE_API_SERVER` 用于 AI 业务 Socket 状态检查；`AI_NODE_METRICS_URL` 用于读取仅回环开放的 Xray expvar 流量指标
- AI 节点使用独立 REALITY 凭据，字段契约见 [AI 节点独立凭据](ai-node-credentials.md)
- 详见 [AI 节点部署与 SSH 纳管](ai-node-deployment.md)

完整变量模板见：

- [../.env.example](../.env.example)
- [../app/xray/.env.example](../app/xray/.env.example)
