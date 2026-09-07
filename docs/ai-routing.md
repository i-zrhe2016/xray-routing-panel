# AI 路由

## 主链路

AI 路由由控制面容器中的 `xray-ai-domain-manager` 驱动，通过内网 SSH 或共享工作目录管理普通数据面，默认流程如下：

![AI 域名路由与回退流程](diagrams/ai-routing-flow.svg)

[查看 PlantUML 源文件](diagrams/ai-routing-flow.puml)

每小时流量分析与入库/分流细节见：[PlantUML 流程图](diagrams/ai-hourly-analysis.svg) · [源文件](diagrams/ai-hourly-analysis.puml)

1. 每小时读取最近一小时普通数据面 `access.log`；远端 SSH 模式直接在数据面读取，避免把整份日志复制到控制面
2. 先应用内建 AI 域名规则
3. 对未知域名优先调用本机 `codex`
4. 如 `codex` 不可用，再回退到 OpenAI 兼容接口
5. 仅将已观测且分类为 `ai` 的域名写入 `panel.db` 的 `ai_domains` 和 `ai_domain_observations`，历史 AI 域名保留累计结果
6. 生成只包含 AI 域名的动态路由、小时报表
7. 探测主、备 AI 候选并按当前模式选择目标
8. 路由变化时重新渲染并重启数据面

内建强制 AI 域名族覆盖 ChatGPT/OpenAI（`chatgpt.com`、`openai.com`、`oaistatic.com`、`oaiusercontent.com`）、Claude/Anthropic（`claude.ai`、`anthropic.com`、`claude.com`、`claudeusercontent.com`）和 AWS。AWS 规则覆盖服务端点（`amazonaws.com`、`amazonaws.com.cn`、`amazonwebservices.com.cn`、`api.aws`、`on.aws`）、控制台与静态资源（`aws.amazon.com`、`awsstatic.com`、`awsplayer.com`、`awscloud.com`）、Identity Center（`awsapps.com`、`awsapps.cn`）以及 AWS 专用域名族（`aws.dev`、`aws`、`aws.a2z.com`、`aws.a2z.org.cn`）。这些域名的子域名也会匹配；`amazon.com`、`cloudfront.net` 和 `live-video.net` 属于共享范围较大的域名族，未纳入全量规则，以免把非 AWS 流量一并转发；实际观测到的域名才写入数据库聚合表。

AI 域名流量最终由 `dynamic-routing.json` 送入 `ai_proxy` VLESS + REALITY outbound，再转发到选中的 AI 上游并由其 freedom 直出。非 AI 域名以及尚未完成分类的域名不进入动态规则，继续使用普通 DMIT 数据面的默认 `freedom` outbound 直出。该 outbound 必须使用与对应 AI inbound 独立且完整匹配的凭据，不能从普通数据面 `XRAY_*` 盲目派生。当前生产候选为主 `nat.qq.pw:27166`、备 `redacted-ip-004:27166`；截至 2026 年 8 月 23 日，主候选不可达，动态路由已选中备用候选。

## 输入与输出

手动强制回退同样通过域名管理器重新渲染完整配置、同步并重载数据面，不仅删除动态片段。
渲染前会在配置文件旁创建 `<配置文件名>.pending-apply` 标记；应用失败时保留，后续管理周期即使
配置内容未变化也会重试重载，成功后清除。备用节点重启返回失败或超时时，端口事务补偿也会尝试
重新加载原备用配置。管理器还会使用共享运行目录中的文件锁，避免常驻周期任务与手动 `--once`
同时改写配置。以上恢复依赖管理进程继续运行及节点重新可达。

输入：

- 普通数据面 `access.log`（SSH 模式由 `DATAPLANE_ACCESS_LOG_PATH` 指定；留空时可从 `DATAPLANE_CONFIG_PATH` 推导）
- `app/xray/.env`
- 可选 `app/xray/ai-proxy-outbound.json`

输出：

- `app/xray/runtime/ai-domain-decisions.json`
- `app/xray/runtime/dynamic-routing.json`
- `app/xray/reports/hourly-domains/latest.json`
- `app/xray/reports/hourly-domains/latest.txt`
- `data/panel.db` 中仅保存已观测且分类为 `ai` 的域名及每小时窗口观测

## AI 上游选择

AI 上游即 AI 节点的公网入口地址。常见配置方式有两种：

- 主上游 + 追加备用：
  - `AI_UPSTREAM_HOST`
  - `AI_UPSTREAM_PORT`
  - `AI_UPSTREAM_FALLBACKS`
- 直接提供完整优先级列表：
  - `AI_UPSTREAMS`

主 AI 上游也可能使用独立的 UUID、REALITY 公钥、Short ID 和 SNI。主数据面 `ai_proxy` outbound 与 AI inbound 的字段契约见 [AI 节点独立凭据](ai-node-credentials.md)。备用上游使用不同凭据时，应提供完整且受保护的分享链接：

- 使用 `AI_UPSTREAM_FALLBACK_URL`

配置 `AI_NODE_SSH_TARGET` 只代表控制面能够纳管节点，不证明隧道凭据匹配，也不会安全地产生 relay URL。启用控制面备用 relay 时，必须显式提供与 AI inbound 匹配的 `CONTROL_PLANE_BACKUP_UPSTREAM_URL`；否则保持 relay 能力关闭。

管理器优先从普通数据面探测 AI 上游。模板或分享链接提供 REALITY SNI 时执行握手探测，否则使用 TCP 探测；首个不可达时切换到下一个可达上游。

选择模式：

- `auto`：按候选顺序探测，优先选择第一个可达节点；当前顺序是主、备。
- `primary`：人工固定主候选；主候选不可达时不自动改选备用，而是停用动态路由并报告 `manual_target_unreachable`。
- `backup`：人工固定备用候选；备用候选不可达时同样停用动态路由，不静默改回主候选。
- `forced_fallback`：人工强制删除动态 AI 路由，所有 AI 域名回到数据面 freedom 直出。

控制台「AI 出口选择」面板展示两个候选的可达状态和当前选中状态。人工切换会把目标模式作为
一次性参数传给 AI 管理器；配置实际应用成功后才写入 `panel.db` 的 `app_state`，失败时保持之前
的人工模式。若面板状态提交失败，系统会尽力把数据面补偿回之前的模式。

Kubernetes 部署由 `xray-reloader` sidecar 监视共享配置并负责重载；此时设置
`DATAPLANE_EXTERNAL_RELOADER_ENABLED=1`，并用 `AI_DOMAIN_MANAGER_EXECUTION_MODE=local` 让面板在同一
Pod 内调用管理器。管理器会将配置应用标记为 delegated，不要求自身具备数据面重启命令。重试报告
会在 `route_status.config_retried` 标记，即使配置内容没有变化，首页也会显示“已重试应用”。

## 控制台操作

管理员首页的「AI 主备节点」控制台将当前策略、实际出口、主备候选和可达状态放在同一张卡片中：

- `切换到备用 AI`：人工固定备用候选，作为 AI 主节点异常时的人工回退动作。
- `固定主 AI`：人工固定主候选，不再依赖自动探测。
- `恢复自动探测`：恢复按候选可达性自动选择。
- `高级应急 / 强制直出`：移除动态 AI 路由，让 AI 域名回普通数据面 freedom 直出；该动作需要单独确认。

人工固定目标即使当前不可达也允许提交，但确认框会显示不可达状态；系统不会静默改选另一候选。所有人工切换完成后，页面会依据接口返回的最新 dashboard 状态更新当前路径和策略。

远端数据面模式通过控制面直接连接内网 SSH 目标 `root@100.116.187.106:22`，不使用或挂载私钥；认证由目标 SSH 服务提供密码/键盘交互方式。主机指纹仍通过受控 `known_hosts` 严格校验。

如果自动模式下所有 AI 上游都不可达，或人工固定的目标不可达：

- 不再下发 `ai_proxy` 动态路由
- 删除 `dynamic-routing.json`（`ai_domain_manager.py:1675`）
- 已命中的 AI 域名会回退到主链路流量（数据面 freedom 直出）
- 自动模式报表中的 `route_status` 会标记为 `fallback_to_primary`；人工模式标记为 `manual_target_unreachable`
- 回退判断由 `should_fallback_to_primary_route()`（`ai_domain_manager.py:1183`）完成
- **此回退不涉及 DNS 切换**

管理员也可以在控制台总览中主动执行“切到主 AI”“切到备用 AI”“恢复自动探测”或“AI 全部直出”。
管理器应用成功后才把这些模式写入控制面数据库的 `app_state`；API 形式见 [API 与页面路径](api.md)。

如果普通数据面管理通道本身探测失败，报告会标记 `probe_error`，并停止继续下发 AI 动态路由；修复 SSH 后下一轮会重新探测并恢复或回退。

AI 节点恢复后，下一轮探测到可达，重新生成 `dynamic-routing.json`，AI 流量恢复转发到 AI 节点。

## 代理模板

仓库默认提供：

- `app/xray/ai-proxy-outbound.json`

模板中的这些占位符会在运行时替换：

- `__AI_UPSTREAM_HOST__`
- `__AI_UPSTREAM_PORT__`
- `__PANEL_UPSTREAM_HOST__`
- `__PANEL_UPSTREAM_PORT__`
- `__PANEL_LISTEN_PORT__`

如果模板不存在，管理器会回退到内建 `freedom redirect`。

## Codex / OpenAI 兼容分类器

默认 compose 会挂载宿主机这些路径，以便容器调用本机 `codex`：

- `/root/.codex`
- `/root/.nvm/versions/node`

如果你的环境不是这些路径：

- 调整 `docker-compose.yml` 中的挂载
- 或在 `app/xray/.env` 中设置 `CODEX_CLI_JS` / `CODEX_BIN`

如果没有可用的 `codex` 或 OpenAI 兼容接口：

- 内建已知 AI 域名仍会命中
- 未知域名不会自动得到 AI / 非 AI 分类

## MCP 工具

仓库自带一个辅助 MCP server：

```bash
python -m app.xray.google_search_mcp
```

它不是主链路的自动步骤，只用于辅助人工或半自动归类。默认提供：

- `collect_uncategorized_domains`
- `search_domains_with_google`
- `classify_domains_with_google`

Google 搜索层直接抓取搜索结果页，不依赖 Google Search API；分类默认使用 OpenRouter 上的 `openai/gpt-5-nano`。

## 常用命令

手动跑一轮 AI 域名分析：

```bash
docker compose --profile xray run --rm xray-ai-domain-manager python -m app.xray.ai_domain_manager --once
```

查看 AI 管理器日志：

```bash
docker compose --profile xray logs -f xray-ai-domain-manager
```

查看最新报告：

```bash
cat app/xray/reports/hourly-domains/latest.txt
sed -n '1,220p' app/xray/reports/hourly-domains/latest.json
```

## 源码与运行目录

`app/xray/` 是 AI 路由和 Xray 配置子系统的代码目录，文档统一维护在本目录。常用入口如下：

- `render_config.py`：渲染 `config.json`、`client-test.json` 和分享链接
- `ai_domain_manager.py`：域名分类、动态路由、小时报表
- `google_search_mcp.py`：辅助归类用 MCP server
- `runtime/`：渲染产物和运行时缓存
- `reports/`：小时域名报告
