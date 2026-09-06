import base64
import json
from urllib.parse import quote, urlencode

from .config import PANEL_SUBSCRIPTION_PUBLIC_URL, XRAY_CLIENT_CONFIG_PATH
from .helpers import external_url_for, normalize_subscription_name, yaml_quote


def parse_xray_client_profile():
    if not XRAY_CLIENT_CONFIG_PATH.is_file():
        return None, f"未找到 Xray 客户端配置：{XRAY_CLIENT_CONFIG_PATH}"

    try:
        payload = json.loads(XRAY_CLIENT_CONFIG_PATH.read_text(encoding="utf-8"))
        outbounds = payload.get("outbounds", [])
        if not isinstance(outbounds, list) or not outbounds:
            raise ValueError("outbounds 为空")
        outbound = next((item for item in outbounds if item.get("protocol") == "vless"), outbounds[0])
        vnext = outbound["settings"]["vnext"][0]
        user = vnext["users"][0]
        reality = outbound["streamSettings"]["realitySettings"]
        profile = {
            "server": str(vnext["address"]).strip(),
            "uuid": str(user["id"]).strip(),
            "flow": str(user.get("flow", "")).strip(),
            "server_name": str(reality["serverName"]).strip(),
            "public_key": str(reality["publicKey"]).strip(),
            "short_id": str(reality["shortId"]).strip(),
            "fingerprint": str(reality.get("fingerprint", "chrome")).strip() or "chrome",
        }
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return None, f"Xray 客户端配置解析失败：{exc}"

    missing = [key for key, value in profile.items() if not value]
    if missing:
        return None, f"Xray 客户端配置缺少字段：{', '.join(missing)}"
    if "panelSubscription" in payload:
        metadata = payload["panelSubscription"]
        if not isinstance(metadata, dict) or not isinstance(metadata.get("users"), dict):
            return None, "统一入口订阅配置无效"
        try:
            profile["unified_port"] = int(metadata["port"])
            if not 1 <= profile["unified_port"] <= 65535:
                raise ValueError("invalid port")
        except (KeyError, TypeError, ValueError):
            return None, "统一入口订阅端口无效"
        profile["user_uuids"] = metadata["users"]
    return profile, ""


def subscription_endpoint(profile, listen_port):
    if profile.get("unified_port"):
        # Missing/disabled accounts must never fall back to another user's ID.
        return int(profile["unified_port"]), profile["user_uuids"][str(int(listen_port))]
    return int(listen_port), profile["uuid"]


def build_vless_share_link(profile, listen_port, note):
    public_port, client_uuid = subscription_endpoint(profile, listen_port)
    params = urlencode(
        {
            "encryption": "none",
            "flow": profile["flow"],
            "security": "reality",
            "sni": profile["server_name"],
            "fp": profile["fingerprint"],
            "pbk": profile["public_key"],
            "sid": profile["short_id"],
            "type": "tcp",
            "headerType": "none",
        }
    )
    tag = quote(normalize_subscription_name(note, listen_port), safe="")
    return f"vless://{client_uuid}@{profile['server']}:{public_port}?{params}#{tag}"


def build_v2ray_subscription_content(profile, listen_port, note):
    share_link = build_vless_share_link(profile, listen_port, note)
    return base64.b64encode(f"{share_link}\n".encode("utf-8")).decode("ascii")


_CLASH_RULE_PROVIDER_BASE_URL = "https://cdn.jsdelivr.net/gh/ACL4SSR/ACL4SSR@master/Clash/Providers"

# Keep the provider set deliberately small. The old subscription downloaded
# five overlapping ad lists and several redundant China lists on every client.
# Fewer downloads reduce startup latency and avoid a partially usable rule
# chain when one remote provider is temporarily unavailable.
_CLASH_RULE_PROVIDERS = (
    ("LAN", "LocalAreaNetwork.yaml"),
    ("UNBAN", "UnBan.yaml"),
    ("ADS", "BanAD.yaml"),
    ("PROGRAM_ADS", "BanProgramAD.yaml"),
    ("DOWNLOAD", "Download.yaml"),
    ("CHINA_DOMAIN", "ChinaDomain.yaml"),
    ("CHINA_IP", "ChinaIp.yaml"),
    ("CHINA_MEDIA", "ChinaMedia.yaml"),
    ("PROXY_GFW", "ProxyGFWlist.yaml"),
    ("PROXY_MEDIA", "ProxyMedia.yaml"),
    ("APPLE", "Ruleset/Apple.yaml"),
    ("BILIBILI", "Ruleset/Bilibili.yaml"),
    ("BILIBILI_HMT", "Ruleset/BilibiliHMT.yaml"),
    ("GOOGLE", "Ruleset/Google.yaml"),
    ("GOOGLE_CN", "Ruleset/GoogleCN.yaml"),
    ("GOOGLE_FCM", "Ruleset/GoogleFCM.yaml"),
    ("MICROSOFT", "Ruleset/Microsoft.yaml"),
    ("NETEASE", "Ruleset/NetEaseMusic.yaml"),
    ("NETFLIX", "Ruleset/Netflix.yaml"),
    ("ONEDRIVE", "Ruleset/OneDrive.yaml"),
    ("SPOTIFY", "Ruleset/Spotify.yaml"),
    ("STEAM", "Ruleset/Steam.yaml"),
    ("STEAM_CN", "Ruleset/SteamCN.yaml"),
    ("TELEGRAM", "Ruleset/Telegram.yaml"),
    ("YOUTUBE", "Ruleset/YouTube.yaml"),
)

# Clash evaluates rules top-to-bottom and stops at the first match. Keep
# narrow exceptions and explicit overseas services before broad China and
# proxy sets; otherwise a domain present in both sets is routed incorrectly
# by whichever broad provider appears first.
_CLASH_RULES = (
    "RULE-SET,LAN,DIRECT",
    "DOMAIN,localhost,DIRECT",
    "DOMAIN-SUFFIX,local,DIRECT",
    "DOMAIN-SUFFIX,lan,DIRECT",
    "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve",
    "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
    "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve",
    "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
    "IP-CIDR6,::1/128,DIRECT,no-resolve",
    "IP-CIDR6,fc00::/7,DIRECT,no-resolve",
    "RULE-SET,UNBAN,DIRECT",
    "RULE-SET,ADS,REJECT",
    "RULE-SET,PROGRAM_ADS,REJECT",
    "RULE-SET,DOWNLOAD,DIRECT",
    # Service-specific rules must precede broad geolocation rules.
    "RULE-SET,GOOGLE_FCM,PROXY",
    "RULE-SET,GOOGLE_CN,DIRECT",
    "RULE-SET,GOOGLE,PROXY",
    "RULE-SET,TELEGRAM,PROXY",
    "RULE-SET,NETFLIX,PROXY",
    "RULE-SET,SPOTIFY,PROXY",
    "RULE-SET,STEAM_CN,DIRECT",
    "RULE-SET,STEAM,PROXY",
    "RULE-SET,YOUTUBE,PROXY",
    "RULE-SET,BILIBILI_HMT,PROXY",
    "RULE-SET,BILIBILI,DIRECT",
    "RULE-SET,NETEASE,DIRECT",
    "RULE-SET,APPLE,PROXY",
    "RULE-SET,MICROSOFT,PROXY",
    "RULE-SET,ONEDRIVE,PROXY",
    "RULE-SET,CHINA_MEDIA,DIRECT",
    "RULE-SET,PROXY_MEDIA,PROXY",
    "RULE-SET,PROXY_GFW,PROXY",
    # ChinaIp covers the old ChinaCompanyIp provider without another download.
    "RULE-SET,CHINA_DOMAIN,DIRECT",
    "RULE-SET,CHINA_IP,DIRECT",
    "GEOIP,CN,DIRECT",
    "MATCH,PROXY",
)


def _append_clash_rule_providers(lines):
    lines.append("rule-providers:")
    for index, (name, relative_url) in enumerate(_CLASH_RULE_PROVIDERS):
        if index:
            lines.append("")
        lines.extend(
            (
                f"  {name}:",
                "    type: http",
                "    behavior: classical",
                f"    url: {_CLASH_RULE_PROVIDER_BASE_URL}/{relative_url}",
                f"    path: ./ruleset/{name}.yaml",
                "    interval: 86400",
            )
        )


def build_clash_subscription_content(profile, listen_port, note):
    public_port, client_uuid = subscription_endpoint(profile, listen_port)
    proxy_name = normalize_subscription_name(note, listen_port)
    lines = [
        "port: 7890",
        "socks-port: 7891",
        "allow-lan: true",
        "mode: rule",
        "log-level: info",
        "ipv6: true",
        "",
        "dns:",
        "  enable: true",
        "  listen: 0.0.0.0:1053",
        "  ipv6: true",
        "  enhanced-mode: fake-ip",
        "  nameserver:",
        # Preserve the control-plane deployment's existing China-friendly DNS
        # defaults while changing only the subscription rule chain below.
        "    - https://dns.alidns.com/dns-query",
        "    - https://doh.pub/dns-query",
        "",
        "proxies:",
        f"  - name: {yaml_quote(proxy_name)}",
        "    type: vless",
        f"    server: {yaml_quote(profile['server'])}",
        f"    port: {public_port}",
        f"    uuid: {yaml_quote(client_uuid)}",
        "    udp: true",
        "    tls: true",
        "    network: tcp",
        f"    servername: {yaml_quote(profile['server_name'])}",
        f"    flow: {yaml_quote(profile['flow'])}",
        "    reality-opts:",
        f"      public-key: {yaml_quote(profile['public_key'])}",
        f"      short-id: {yaml_quote(profile['short_id'])}",
        f"    client-fingerprint: {yaml_quote(profile['fingerprint'])}",
        "",
        "proxy-groups:",
        "  - name: PROXY",
        "    type: select",
        "    proxies:",
        f"      - {yaml_quote(proxy_name)}",
        "      - DIRECT",
        "",
        "  - name: AUTO",
        "    type: url-test",
        "    url: http://www.gstatic.com/generate_204",
        "    interval: 300",
        "    proxies:",
        f"      - {yaml_quote(proxy_name)}",
        "",
    ]
    _append_clash_rule_providers(lines)
    lines.extend(("", "rules:"))
    lines.extend(f"  - {rule}" for rule in _CLASH_RULES)
    lines.append("")
    return "\n".join(lines)


def build_port_access_payload(port, subscription_profile):
    tenant_panel_path = f"/tenant/{port['tenant_token']}"
    payload = {
        "tenant_panel_url": external_url_for("tenant_panel", tenant_token=port["tenant_token"]),
        "tenant_login_url": external_url_for("login", next=tenant_panel_path),
        "tenant_username": str(port.get("tenant_username") or ""),
        "tenant_password": str(port.get("tenant_password") or ""),
        "tenant_subscription_default_url": "",
        "tenant_subscription_clash_url": "",
        "tenant_subscription_v2ray_url": "",
        "share_link": "",
    }
    if subscription_profile is None:
        return payload

    payload["tenant_subscription_default_url"] = external_url_for(
        "tenant_subscription_default",
        base_url=PANEL_SUBSCRIPTION_PUBLIC_URL,
        subscription_token=port["subscription_token"],
    )
    payload["tenant_subscription_clash_url"] = external_url_for(
        "tenant_subscription_clash",
        base_url=PANEL_SUBSCRIPTION_PUBLIC_URL,
        subscription_token=port["subscription_token"],
    )
    payload["tenant_subscription_v2ray_url"] = external_url_for(
        "tenant_subscription_v2ray",
        base_url=PANEL_SUBSCRIPTION_PUBLIC_URL,
        subscription_token=port["subscription_token"],
    )
    if not subscription_profile.get("unified_port") or str(port["listen_port"]) in subscription_profile["user_uuids"]:
        payload["share_link"] = build_vless_share_link(subscription_profile, port["listen_port"], port["note"])
    return payload
