# Clash 统一 443 入口

启用后，新获取的 Clash 和 V2Ray 订阅统一连接 `server:443`。已有订阅 URL、
HTTP 80/8080 和 HTTPS 443 的订阅下载保持可用；未更新的客户端仍可使用原代理端口。
此功能需要部署网关后显式开启，单独修改订阅端口不会自动迁移线上监听。

```text
Clash 新订阅 ───────────────────→ 443 ─→ 独立用户 UUID 的 REALITY 入站
Clash 旧端口 → TCP 转发至本机 443 ─┘  └→ 对应旧端口的 REALITY 兼容入站
HTTPS 订阅 / 网站 ──────────────→ 443 ─→ 127.0.0.1:18443 原 HTTPS 服务
```

443 由 HAProxy 监听：REALITY SNI 进入代理，其余 SNI 进入原 HTTPS 服务。
原代理端口由独立的 TCP 转发器监听。转发器发送带原目标端口和源 IP 的 PROXY v2 头，
443 再交给对应的兼容入站。内部入站使用 Unix socket，不另外暴露 TCP 代理端口。
这里保留内部兼容入站是为了让原来的共享 UUID 和按端口配额继续有效。

## 配置与计费

在控制面的 `app/xray/.env` 设置：

```dotenv
XRAY_UNIFIED_PORT=443
XRAY_UNIFIED_UUID_SECRET=<至少 32 字符的私有随机值>
```

统一入口端口不能同时作为某个租户的旧监听端口；如果数据库中已经有租户占用
`443`，须先迁移该租户的旧端口，再启用统一入口。

每个账号按原端口标识通过 HMAC 派生独立 UUID。这个密钥须备份；更换它会改变
新订阅 UUID，不能用所有旧客户均可见的 `XRAY_CLIENT_UUID` 代替。
新的 UUID 只在 443 有效，旧 UUID 只在旧兼容端口有效。
旧端口流量与新 `panel-user-{port}` 用户流量相加，仍计入原来的账号配额。
停用、到期或超额后，配置会同时移除该账号的新 UUID 与旧端口兼容入站。
没有活跃账号时保持空客户端列表，不回退为共享账号。

## 部署顺序

普通数据面必须安装、验证此入口，之后才能发布 443 订阅。若启用了控制面备用
Xray，控制面也必须部署等价的 TCP 网关；控制面现有 Nginx 通常已经占用 443，
不能直接套用本目录的普通数据面 unit，必须先完成 Nginx/备用入口的端口切换设计。
AI 节点使用独立凭据和监听端口，不参与此迁移。

1. 备份两端实际 Xray 配置、控制面 `.env`、客户端配置、订阅服务及其 systemd
   配置。检查 443 当前所有使用者；本部署的 HTTPS 服务为 `verge-sub`。
2. 安装 HAProxy（已验证 2.8 配置语法）。将 `scripts/render_entry_gateway.py`
   和 `scripts/run_subscription_backend.py` 安装至 `/usr/local/lib/xray-entry/`。
3. 安装 `deploy/normal-data-plane/xray-entry.service`、
   `xray-legacy-forwarder.service`、`xray-entry-refresh.service` 和
   `xray-entry-refresh.path` 至 `/etc/systemd/system/`。配置 `/etc/xray-entry.env`：

   ```dotenv
   XRAY_ENTRY_CONFIG=/root/xray-routing-panel/app/xray/runtime/config.json
   XRAY_ENTRY_SOCKET_DIR=/root/xray-routing-panel/app/xray/logs
   ```

   这里必须指向 Docker 实际挂载的配置和日志目录。若路径不同，也须修改 `.path`
   监听的路径。`.path` 在增删账号或到期移除端口后重新生成并平滑重载 HAProxy。
4. 暂停控制面的配置写入进程，生成统一模式的候选 Xray 配置。使用实际运行版本的
   `xray run -test -config ...` 校验；用 `render_entry_gateway.py` 校验 HAProxy
   配置。保留已有 REALITY SNI、密钥、AI 路由和备用出站配置。
5. 将 `verge-sub-unified.conf` 安装为
   `/etc/systemd/system/verge-sub.service.d/unified-entry.conf`，执行
   `systemctl daemon-reload`。该包装器只把原服务的 TLS 监听改到回环 18443，
   不修改原服务源码。切换 Xray、重启 `verge-sub` 并启动 `xray-entry`。
6. 普通数据面验证 HTTPS 订阅/网站、新 443 和每个旧端口的真实 REALITY 握手与 HTTP
   请求；确认新旧账号统计均增加。控制面备用只有在完成独立网关和 Nginx 端口切换
   后才做同样的验证。成功后启用网关及路径监听的开机启动，更新订阅生成代码、
   恢复控制面配置写入进程。原 `verge_sub/node_info.json` 若也提供节点
   订阅，必须同步为对应活跃账号的 443 UUID，不能保留旧共享 UUID。

HAProxy 仅对来自 `127.0.0.2` 的内部转发接受 PROXY 头。公网连接不接受客户端
伪造的 PROXY 元数据；Xray 的 Unix socket 不对公网开放。

## 验证与回退

`scripts/smoke_unified_entry.py` 接收实际的 `--server-config`、`--client-config`、
`--host` 和 `--xray` 路径，验证每个新账号和旧端口，且验证旧共享 UUID 在 443
被拒绝。不打印 UUID、私钥或订阅令牌。需要在两端都运行，并从公网客户端验证
入口防火墙与 DNS 路径。

集成测试可执行：

```bash
XRAY_TEST_BINARY=/path/to/xray python -m pytest -q tests/test_unified_entry_integration.py
```

此测试启动临时 Xray、HAProxy、HTTP/HTTPS 服务，使用临时凭据，REALITY 握手
目标为 `www.amazon.com:443`，需要能够直连该站点。

若切换失败，先暂停配置写入与路径监听，停止 `xray-entry`，恢复备份的多端口
Xray 配置、`.env`、客户端订阅文件及原订阅服务启动配置，再重启 Xray 和
`verge-sub`。必须同时恢复订阅输出，避免仍发布未监听的 443 代理节点。
