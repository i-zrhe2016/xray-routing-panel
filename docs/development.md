# 开发与启动

## 前置条件

- Linux 宿主机
- Docker 和 Docker Compose
- 如果启用完整数据面，确认 `443` 未被其他进程占用
- 如果启用 Codex 域名分类，宿主机需要可用的 `codex` CLI 登录态

控制面镜像使用官方 `python:3.12-slim` 运行时基础镜像，应用依赖由
`requirements.txt` 通过 pip 安装。构建请使用项目约定的 CPU-aware Docker
构建脚本，避免绕过统一的 BuildKit 资源限制：

```bash
/root/docker-build-cpu/scripts/docker-build.sh \
  xray-routing-panel-xray-routing-panel:latest .
```

## 推荐本地路径：完整栈

```bash
./app/xray/generate-secrets.sh
cp app/xray/.env.example app/xray/.env
cp .env.example .env
python -m app.xray.render_config
docker compose --profile xray up -d --build
```

这会启动：

- `xray-routing-panel`
- `xray-routing-panel-db-backup`
- `xray-reality`
- `xray-ai-domain-manager`

## 只启动面板

```bash
docker compose up -d --build
```

适合先验证 UI、数据库和租户流程。此模式下：

- 面板仍会渲染 Xray 配置文件
- 如果没有单独运行的数据面，端口不会真正承载流量
- `/healthz` 如需仅检查面板本身，设置 `PANEL_HEALTH_REQUIRES_XRAY=0`

## 本地二进制 / 外部 Xray

如果你不使用 compose 里的 `xray-reality` 容器：

- 设置 `DATAPLANE_LOCAL_BIN=/path/to/xray`
- 设置 `DATAPLANE_API_SERVER=redacted-ip-007:10085`
- 让外部 Xray 进程自行加载 `app/xray/runtime/config.json`

注意：

- 面板可以渲染并执行 `xray run -test`
- 面板不会替你守护或重启这个外部进程

## 远端控制面 / 数据面分离

至少配置：

- `DATAPLANE_SSH_TARGET`
- `DATAPLANE_CONFIG_PATH`
- `DATAPLANE_PANEL_PORTS_PATH`
- `DATAPLANE_ACCESS_LOG_PATH`

按需补充：

- `DATAPLANE_DYNAMIC_ROUTING_PATH`
- `DATAPLANE_AI_REPORT_PATH`
- `DATAPLANE_PANEL_DB_PATH`
- `DATAPLANE_RESTART_COMMAND`
- `DATAPLANE_CONTAINER_NAME`

远端模式下，探针目标不要继续使用本地回环：

- 把 `DATAPLANE_PROBE_HOST` 设置成远端入口 IP 或域名

## AI 节点纳管

当前 AI 备用是控制面本机 Docker `xray-ai-node`；设置 `AI_NODE_SSH_TARGET` 后才切换为远端独立 Xray 的 SSH 纳管。部署与认证方式见 [AI 节点部署与 SSH 纳管](ai-node-deployment.md)。

至少配置：

- `AI_NODE_SSH_TARGET`
- `AI_NODE_SSH_BIN` / `AI_NODE_SSH_OPTIONS`
- `AI_NODE_API_SERVER`
- `AI_NODE_PROBE_HOST`
- `AI_UPSTREAM_HOST` / `AI_UPSTREAM_PORT`（在 `app/xray/.env` 中）

AI 节点使用独立 REALITY 凭据，不能复用普通数据面参数。开发环境只有在具备独立 AI 凭据输入和完整回滚验证时才设置 `AI_NODE_CONFIG_PATH`；生产当前保持为空以禁用上传。详见 [AI 节点独立凭据](ai-node-credentials.md)。

## 常用命令

渲染配置：

```bash
python -m app.xray.render_config
```

查看完整栈状态：

```bash
docker compose --profile xray ps
```

查看面板日志：

```bash
docker compose logs -f xray-routing-panel
```

查看数据面日志：

```bash
docker compose --profile xray logs -f xray-reality
```

启动本地 AI 节点容器（测试用）：

```bash
docker compose --profile ai-node up -d xray-ai-node
docker compose --profile ai-node logs -f xray-ai-node
```

启动控制面备用 Xray：

```bash
docker compose --profile backup-xray up -d xray-reality-backup
docker compose --profile backup-xray logs -f xray-reality-backup
```

获取 AI 节点状态（目标态 API）：

```bash
curl -s -u admin:secret http://redacted-ip-007:18080/api/ai-node/status | python3 -m json.tool
```

重启 AI 节点（目标态 API）：

```bash
curl -s -u admin:secret -X POST http://redacted-ip-007:18080/api/ai-node/restart | python3 -m json.tool
```

手动跑一轮 AI 域名分析：

```bash
docker compose --profile xray run --rm xray-ai-domain-manager python -m app.xray.ai_routing.runner --once
```

手动跑一轮数据库备份和上传链路：

```bash
docker compose run --rm xray-routing-panel-db-backup \
  python3 /app/scripts/run_db_backup_cycle.py
```

只验证备份上传逻辑，不连接真实 R2：

```bash
docker compose run --rm \
  -e DB_BACKUP_R2_ENABLED=0 \
  xray-routing-panel-db-backup \
  python3 /app/scripts/run_db_backup_cycle.py
```

验证归档完整性并准备替换节点：

```bash
python3 scripts/node_recovery.py validate --bundle ./backups/<bundle>.tar.gz --require-ready --json
python3 scripts/node_recovery.py prepare \
  --bundle ./backups/<bundle>.tar.gz \
  --node normal-data-plane \
  --output-dir /tmp/xray-node-recovery
docker compose -f /tmp/xray-node-recovery/docker-compose.node.yml config
```

仅直接启动面板进程（非 Docker）：

```bash
# 直接运行使用仓库中的 app/static/ 发布资源
python app/panel.py
```

这会调用 `app.web.main()`（定义在 `app/web/core.py`），启动维护线程并监听 `PANEL_HOST:PANEL_PORT`。

## 前端发布资源

管理后台、订阅者门户和 Landing 页的发布资源已保存在
`app/static/{admin,portal,landing}`。控制面镜像直接复制这些静态文件，运行时不安装
JavaScript 构建工具。

Admin 控制台源码和构建配置位于 `frontend/`。修改 Admin 前端后执行：

```bash
cd frontend
npm ci
npm test
npm run build
```

`npm run build` 会将 Admin 入口输出为 `app/static/admin/admin.js` 和
`app/static/admin/admin.css`；构建产物必须随变更一起提交，Docker 不在镜像构建阶段安装
Node 或 npm。

后端测试用 pytest 直接跑现有 unittest：

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

## Landing 页面资源

Landing 页的字体、图标和分享图片已随发布资源保存在
`app/static/landing/`，部署时由镜像直接提供。
