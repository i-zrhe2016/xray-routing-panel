# Cloudflare Access 邮箱登录

## 当前入口

控制面管理入口为：

```text
https://xray.zrhe2016.cc
```

公网请求先经过 Cloudflare Access，再由 Nginx 反向代理到控制面本机服务 `redacted-ip-007:18080`。控制面原始端口不作为公网管理入口。

控制面内网直连入口为：

```text
http://100.112.13.103:18080
```

该入口用于控制面 Tailscale 内网运维，免管理员登录和 CSRF；公网域名仍必须通过
Cloudflare Access 和应用鉴权。

## 登录方式

当前使用 Cloudflare Access **One-time PIN（Email OTP）**，不使用 Google 登录。

允许的邮箱：

```text
redacted-email-001 [at] example.invalid
```

登录步骤：

1. 打开 `https://xray.zrhe2016.cc`。
2. 输入允许的邮箱地址。
3. 查收 Cloudflare Access 发送的一次性验证码。
4. 输入验证码完成 Access 会话认证。
5. 进入控制面管理界面。

验证码由 Cloudflare Access 发送，不由本项目发送或保存。

## Cloudflare Dashboard 配置

在 Cloudflare Zero Trust 控制台：

1. `Settings → Authentication → Login methods`。
2. 启用 `One-time PIN`。
3. 可关闭不再使用的 `Google` 登录方式。
4. 进入 `Access → Applications`，编辑 `xray.zrhe2016.cc`。
5. Allow 策略选择 `Emails`，填入 `redacted-email-001 [at] example.invalid`。
6. 保存并等待策略生效。

不要启用 Cloudflare Tunnel；当前方案使用橙云 DNS + 源站 Nginx 443。

## 源站安全边界

- 控制面监听地址：`0.0.0.0:18080`；控制面 Tailscale 直连地址为 `100.112.13.103:18080`。
- 公网 HTTPS 入口：Nginx `443`。
- SSH `22`、Grafana `3001`、Prometheus `9090` 不属于本入口，保持独立管理。
- `18080` 用于控制面内网直连；公网管理仍应优先使用 Cloudflare Access 入口，避免绕过访问控制。
- `/healthz` 是机器健康检查接口，不是管理入口。

## 排障

查看 DNS/Access/Nginx 链路：

```bash
curl -I https://xray.zrhe2016.cc/
```

未认证时预期跳转到 Cloudflare Access 登录地址，而不是直接返回控制面登录页。

源站监听检查：

```bash
ss -lntp | grep -E ':(443|18080)\\b'
```

预期 `18080` 显示 `0.0.0.0:18080`，`443` 由 Nginx 监听。

如果没有收到验证码：

- 检查垃圾邮件和邮件过滤规则；
- 确认 Access 应用 Allow 策略仍包含 `redacted-email-001 [at] example.invalid`；
- 确认 One-time PIN 已启用；
- 清理浏览器中该域名的 Cloudflare Access Cookie 后重试。

## 安全说明

Cloudflare Access 的身份头不能单独作为可信凭据。生产集成应校验 `Cf-Access-Jwt-Assertion` 的签名、受众、签发者和有效期，再建立控制面 session。应用或反向代理变更后必须重新验证这一点，不能仅凭出现邮箱请求头就宣称认证已完成。
