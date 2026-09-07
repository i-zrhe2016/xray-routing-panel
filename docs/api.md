# API 与页面路径

## 认证规则

三套独立会话：**管理员**、**客户**、**租户**（每端口）。

- 管理员：生产公网入口使用 Cloudflare Access Email OTP（One-time PIN），允许邮箱为 `redacted-email-001 [at] example.invalid`；Access 认证后由 Nginx 反代到本机 `redacted-ip-007:18080`。控制面 Tailscale 内网入口 `http://100.112.13.103:18080` 免管理员登录和 CSRF，公网域名仍保留 Access/应用鉴权。
- 客户：`/api/customer/*`（除 `plans`、`auth/*`）需客户会话，未登录返回 JSON 401（`{"ok":false,"code":"auth_required"}`）；变更请求需 `X-CSRF-Token`
- 租户：`/api/tenant/<token>/*` 由管理员会话或该端口的租户会话放行
- `GET /healthz` 永远不要求登录

所有响应都会带 `X-Request-ID`。客户端可以发送由字母、数字、`.`、`_`、`:`、`-` 组成且不超过 128 字符的值；缺失或不合法时由控制面生成新值。该 ID 只用于跨请求排障，不应包含 token、邮箱或其他敏感信息。

## 页面与订阅路径

- `/`：管理后台 SPA（Vue + Naive UI）
- `/login`：管理员登录页
- `/probe-dashboard`：TCP 探针监控页
- `/ai-domain-dashboard`：AI 域名统计页
- `/portal`、`/portal/<path>`：订阅者门户 SPA（vue-router history）
- `/plans`：公共套餐信息页（只读；新购套餐不提供结账/预订单页）
- `/customer/login`、`/customer/register`：客户认证页
- `/tenant/<tenant_token>`：门户的单订阅只读模式壳（原“租户面板”，未认证时显示内联租户登录卡）
- `/tenant-subscriptions/<subscription_token>`：默认订阅
- `/tenant-subscriptions/<subscription_token>/clash`：Clash 订阅
- `/tenant-subscriptions/<subscription_token>/v2ray`：V2Ray 订阅

订阅 token 只对运行中的端口下发配置；端口停用、过期或达到流量上限时返回 `404`，避免客户端继续拿到无法连接的旧配置。

历史兼容订阅路径仍保留：

- `/<token>/<listen_port>`
- `/<token>/<listen_port>/clash`
- `/<token>/<listen_port>/v2ray`

## JSON API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/dashboard` | 获取首页完整状态 |
| `POST` | `/api/ports` | 新建监听端口 |
| `PUT` | `/api/ports/<port_id>` | 更新端口配置 |
| `POST` | `/api/ports/<port_id>/toggle` | 启用或停用端口 |
| `DELETE` | `/api/ports/<port_id>` | 删除端口 |
| `POST` | `/api/ports/<port_id>/reset-traffic` | 重置端口流量并重新启用 |
| `POST` | `/api/ports/<port_id>/rotate-tenant-token` | 重置租户访问地址（`/tenant/<token>`）|
| `POST` | `/api/ports/<port_id>/rotate-tenant-credentials` | 重置租户用户名和密码 |
| `POST` | `/api/ports/<port_id>/rotate-subscription-token` | 重置租户订阅地址 |
| `POST` | `/api/subscriptions/rotate` | 重置历史兼容的全局订阅 token |
| `POST` | `/api/plans` / `PUT /api/plans/<id>` | 套餐增改 |
| `GET` | `/api/orders` | 列出商业化订单 |
| `POST` | `/api/orders/<id>/{fulfill,reject,cancel}` | 订单开通 / 驳回 / 取消 |
| `GET`/`PUT` | `/api/commerce-settings` | 商业设置（收款说明、二维码、订单有效期）|
| `POST` | `/api/data-plane/restart` | 重启数据面 |
| `POST` | `/api/data-plane/diagnose` | 数据面体检（TCP 探测 + Reality 握手 + 配置一致性校验）|
| `GET` | `/api/ai-node/status` | 获取 AI 节点状态 |
| `POST` | `/api/ai-node/restart` | 重启 AI 节点 |
| `GET` | `/api/dns-failover` | 获取 DNS 故障切换状态 |
| `POST` | `/api/dns-failover/check` | 立即执行一次 DNS 检测 |
| `POST` | `/api/dns-failover/switch` | 手动切主备（`{"target": "primary\|backup"}`）|
| `POST` | `/api/ai-routing/switch` | 手动切换 AI 路由（`{"mode": "primary\|backup\|auto\|forced_fallback"}`）|
| `POST` | `/api/client-errors` | 前端上报网络、HTTP、解析和未捕获运行时错误（需 `X-CSRF-Token`；不接收请求体或敏感 Header）|

### 订阅者门户 API（客户会话 + CSRF）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/customer/me` / `/api/customer/overview` | 当前客户与门户首页数据 |
| `GET` | `/api/customer/subscriptions[/<id>]` | 订阅列表 / 详情（含 Clash/V2Ray/VLESS 链接、用量）|
| `POST` | `/api/customer/subscriptions/<id>/renew` | 续费下单 |
| `GET` | `/api/customer/orders[/<order_no>]` | 订单列表 / 详情 |
| `POST` | `/api/customer/orders/<order_no>/payment-proof` | 上传支付凭证（multipart）|
| `GET` | `/api/customer/plans` | 公开套餐列表（无需登录）|
| `POST` | `/api/customer/auth/{login,register,logout}` | 客户认证 |

### 租户直达 API（token / 每端口凭据）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/tenant/<tenant_token>/subscription` | 单订阅只读详情（管理员会话或该端口租户会话）|
| `POST` | `/api/tenant/<tenant_token>/login` | 每端口租户登录 |

## 创建 / 更新端口字段

- `listen_port`
  - 必填，范围 `1-65535`
- `expires_at`
  - 可选，格式示例：`2026-06-30T20:00`
- `traffic_limit`
  - 可选，支持 `10G`、`500MB`、`1048576`
- `note`
  - 可选，最多 `200` 字符

示例：

```bash
curl -u admin:secret http://redacted-ip-007:18080/api/dashboard

curl -u admin:secret \
  -H 'Content-Type: application/json' \
  -X POST http://redacted-ip-007:18080/api/ports \
  -d '{
    "listen_port": 32001,
    "expires_at": "2026-06-30T20:00",
    "traffic_limit": "20G",
    "note": "demo-tenant"
  }'
```

## 常见返回体

管理后台写操作成功后通常返回（携带重建后的完整首页状态）：

```json
{
  "ok": true,
  "message": "...",
  "level": "success",
  "dashboard": {
    "...": "最新首页状态"
  }
}
```

订阅者门户 / 租户接口统一用 `data` 携带受影响资源，不返回管理员 `dashboard`：

```json
{ "ok": true, "message": "...", "level": "success", "data": { "...": "受影响资源" } }
```

失败时通常返回：

```json
{
  "ok": false,
  "message": "错误信息"
}
```

健康检查返回：

```json
{
  "ok": true,
  "data_plane_running": true,
  "ai_node_running": true
}
```

其中：

- `ok` 受 `PANEL_HEALTH_REQUIRES_XRAY` 影响
- `data_plane_running` 反映当前普通数据面是否可用
- `ai_node_running` 反映 AI 节点是否可达（目标态）
