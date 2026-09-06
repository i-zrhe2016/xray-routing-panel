#!/usr/bin/env python3
import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse

from app.xray.config import BASE_DIR, REQUIRED_ENV_KEYS, RUNTIME_DIR
from app.xray.envfile import load_env_file
from app.xray.unified_entry import account_uuid, socket_path, unified_port


def validate_env(values: dict[str, str]) -> None:
    unified_port(values)
    missing = [key for key in REQUIRED_ENV_KEYS if not values.get(key)]
    if missing:
        raise ValueError(f"missing required values: {', '.join(missing)}")

    try:
        port = int(values["XRAY_LISTEN_PORT"])
    except ValueError as exc:
        raise ValueError("XRAY_LISTEN_PORT must be an integer") from exc
    if port < 1 or port > 65535:
        raise ValueError("XRAY_LISTEN_PORT must be in 1..65535")

    public_port_value = values.get("XRAY_PUBLIC_PORT", "").strip()
    if public_port_value:
        try:
            public_port = int(public_port_value)
        except ValueError as exc:
            raise ValueError("XRAY_PUBLIC_PORT must be an integer") from exc
        if public_port < 1 or public_port > 65535:
            raise ValueError("XRAY_PUBLIC_PORT must be in 1..65535")

    short_id = values["XRAY_REALITY_SHORT_ID"]
    if not re.fullmatch(r"[0-9a-fA-F]{1,16}", short_id):
        raise ValueError("XRAY_REALITY_SHORT_ID must be 1-16 hex characters")

    if ":" not in values["XRAY_DEST"]:
        raise ValueError("XRAY_DEST must look like host:port")


def env_bool(values: dict[str, str], key: str, default: bool) -> bool:
    raw = str(values.get(key, "1" if default else "0")).strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


def env_nonnegative_int(values: dict[str, str], key: str, default: int) -> int:
    raw = str(values.get(key, default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be a non-negative integer") from exc
    if value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


def build_stream_sockopt(values: dict[str, str]) -> dict:
    sockopt: dict[str, object] = {}
    if env_bool(values, "XRAY_TCP_FAST_OPEN", True):
        sockopt["tcpFastOpen"] = True

    keepalive_idle = env_nonnegative_int(values, "XRAY_TCP_KEEPALIVE_IDLE", 180)
    if keepalive_idle > 0:
        sockopt["tcpKeepAliveIdle"] = keepalive_idle

    keepalive_interval = env_nonnegative_int(values, "XRAY_TCP_KEEPALIVE_INTERVAL", 30)
    if keepalive_interval > 0:
        sockopt["tcpKeepAliveInterval"] = keepalive_interval

    return sockopt


def load_optional_json(path: Path | None) -> dict | None:
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"dynamic routing file must contain a JSON object: {path}")
    return payload


def load_panel_ports(path: Path | None) -> list[int]:
    payload = load_optional_json(path)
    if not payload:
        return []

    ports = payload.get("ports", [])
    if not isinstance(ports, list):
        raise ValueError("panel ports file must use a JSON list in `ports`")

    normalized: list[int] = []
    seen: set[int] = set()
    for item in ports:
        try:
            listen_port = int(item)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid panel listen port: {item!r}") from exc
        if listen_port < 1 or listen_port > 65535:
            raise ValueError(f"invalid panel listen port: {listen_port}")
        if listen_port in seen:
            continue
        normalized.append(listen_port)
        seen.add(listen_port)
    return normalized


def merge_dynamic_routing(config: dict, dynamic_payload: dict | None) -> dict:
    if not dynamic_payload:
        return config

    extra_outbounds = dynamic_payload.get("outbounds", [])
    if extra_outbounds:
        if not isinstance(extra_outbounds, list):
            raise ValueError("dynamic outbounds must be a JSON list")
        config["outbounds"].extend(extra_outbounds)

    extra_routing = dynamic_payload.get("routing", {})
    if extra_routing:
        if not isinstance(extra_routing, dict):
            raise ValueError("dynamic routing must be a JSON object")
        routing = config.setdefault("routing", {})
        if "domainStrategy" in extra_routing:
            routing["domainStrategy"] = extra_routing["domainStrategy"]
        rules = extra_routing.get("rules", [])
        if rules:
            if not isinstance(rules, list):
                raise ValueError("dynamic routing rules must be a JSON list")
            routing.setdefault("rules", [])
            # Static rules (e.g. the QUIC block) are kept first so they take
            # priority over the dynamic AI-domain rules. A QUIC packet to an AI
            # domain matches both the block rule and the AI-domain rule; xray
            # uses first-match, so the block must win to force a TCP fallback.
            routing["rules"] = list(routing["rules"]) + list(rules)

    return config


def build_reality_inbound(values: dict[str, str], listen_port: int) -> dict:
    stream_sockopt = build_stream_sockopt(values)
    return {
        "tag": f"panel-{listen_port}",
        "listen": values["XRAY_LISTEN_HOST"],
        "port": int(listen_port),
        "protocol": "vless",
        "settings": {
            "clients": [
                {
                    "id": values["XRAY_CLIENT_UUID"],
                    "flow": values["XRAY_FLOW"],
                }
            ],
            "decryption": "none",
        },
        "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "sockopt": stream_sockopt,
            "realitySettings": {
                "show": False,
                "dest": values["XRAY_DEST"],
                "xver": 0,
                "serverNames": [values["XRAY_SERVER_NAME"]],
                "privateKey": values["XRAY_REALITY_PRIVATE_KEY"],
                "shortIds": [values["XRAY_REALITY_SHORT_ID"]],
            },
        },
        "sniffing": {
            "enabled": True,
            "destOverride": ["http", "tls", "quic"],
            "routeOnly": True,
        },
    }


def build_backup_relay_outbound(share_url: str) -> dict:
    """Build the default outbound for the control-plane backup node from a
    ``vless://`` share URL so every client connection is relayed to that upstream
    (e.g. a NAT-forwarded exit at nat.qq.pw) instead of leaving directly.

    The outbound is tagged ``direct`` so it becomes xray's default route and any
    existing routing rule that targets ``direct`` keeps working. Reality and
    plain (security=none) upstreams are supported; the parsing mirrors the AI
    upstream fallback URL handling so a single share link describes the hop.
    """
    text = str(share_url or "").strip()
    if not text:
        raise ValueError("CONTROL_PLANE_BACKUP_UPSTREAM_URL is empty")

    parsed = urlparse(text)
    if parsed.scheme.lower() != "vless":
        raise ValueError("CONTROL_PLANE_BACKUP_UPSTREAM_URL must use a vless:// URL")
    if not parsed.username:
        raise ValueError("CONTROL_PLANE_BACKUP_UPSTREAM_URL is missing the VLESS UUID")
    if not parsed.hostname:
        raise ValueError("CONTROL_PLANE_BACKUP_UPSTREAM_URL is missing the upstream host")
    if parsed.port is None:
        raise ValueError("CONTROL_PLANE_BACKUP_UPSTREAM_URL is missing the upstream port")

    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    network = str(params.get("type", "tcp")).strip().lower() or "tcp"
    if network != "tcp":
        raise ValueError("CONTROL_PLANE_BACKUP_UPSTREAM_URL currently supports only type=tcp")

    security = str(params.get("security", "none")).strip().lower() or "none"
    encryption = str(params.get("encryption", "none")).strip() or "none"
    user: dict[str, object] = {"id": unquote(parsed.username), "encryption": encryption}
    flow = str(params.get("flow", "")).strip()
    if flow:
        user["flow"] = flow

    outbound = {
        "tag": "direct",
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": parsed.hostname,
                    "port": parsed.port,
                    "users": [user],
                }
            ]
        },
        "streamSettings": {
            "network": network,
            "security": security,
        },
    }

    if security == "reality":
        sni = str(params.get("sni", "")).strip()
        fingerprint = str(params.get("fp", "")).strip()
        public_key = str(params.get("pbk", "")).strip()
        short_id = str(params.get("sid", "")).strip()
        if not sni or not fingerprint or not public_key or not short_id:
            raise ValueError(
                "CONTROL_PLANE_BACKUP_UPSTREAM_URL must include sni, fp, pbk, and sid when security=reality"
            )
        outbound["streamSettings"]["realitySettings"] = {
            "serverName": sni,
            "fingerprint": fingerprint,
            "publicKey": public_key,
            "shortId": short_id,
        }
    elif security not in {"none", ""}:
        raise ValueError(
            "CONTROL_PLANE_BACKUP_UPSTREAM_URL currently supports only security=reality or security=none"
        )

    return outbound


def build_server_config(
    values: dict[str, str],
    dynamic_payload: dict | None = None,
    panel_ports: list[int] | None = None,
    relay_outbound: dict | None = None,
) -> dict:
    entry_port = unified_port(values)
    panel_ports = list(panel_ports or [])
    if entry_port:
        if entry_port in panel_ports:
            raise ValueError("The unified entry port cannot also identify a legacy account")
        inbounds = [build_reality_inbound(values, port) for port in panel_ports]
        entry = build_reality_inbound(values, entry_port)
        entry["tag"] = f"unified-{entry_port}"
        entry["settings"]["clients"] = [
            {"id": account_uuid(values, port), "flow": values["XRAY_FLOW"],
             "email": f"panel-user-{port}", "level": 1}
            for port in panel_ports
        ]
        inbounds.append(entry)
        for inbound in inbounds:
            inbound["listen"] = socket_path(inbound["tag"])
            inbound.pop("port")
            inbound["streamSettings"]["sockopt"]["acceptProxyProtocol"] = True
    elif panel_ports:
        inbounds = [build_reality_inbound(values, listen_port) for listen_port in panel_ports]
    else:
        inbounds = [build_reality_inbound(values, int(values["XRAY_LISTEN_PORT"]))]

    # The backup node keeps the same Reality inbound (so failover clients connect
    # with their existing subscription) but swaps the direct exit for a relay
    # outbound that forwards every connection to the configured upstream.
    if relay_outbound is not None:
        default_outbound = dict(relay_outbound)
        default_outbound["tag"] = "direct"
        outbounds = [default_outbound]
    else:
        outbounds = [{"protocol": "freedom", "tag": "direct"}]
    routing_rules: list[dict] = []
    # Drop QUIC/HTTP3 (UDP 443) so clients fall back to TCP+TLS. Proxied QUIC —
    # especially AI traffic relayed through the xtls-rprx-vision AI upstream,
    # which is TCP-oriented — stalls and times out before falling back, which is
    # the main cause of the iOS ChatGPT app loading very slowly. Forcing TCP
    # makes the AI path reliable. Toggle off with XRAY_BLOCK_QUIC=0.
    if env_bool(values, "XRAY_BLOCK_QUIC", True):
        outbounds.append({"protocol": "blackhole", "tag": "block"})
        routing_rules.append(
            {
                "type": "field",
                "network": "udp",
                "port": 443,
                "outboundTag": "block",
            }
        )

    policy = {
        "system": {
            "statsInboundUplink": True,
            "statsInboundDownlink": True,
        }
    }
    if entry_port:
        policy["levels"] = {"1": {"statsUserUplink": True, "statsUserDownlink": True}}

    config = {
        "log": {
            "loglevel": values["XRAY_LOGLEVEL"],
            "access": "/var/log/xray/access.log",
            "error": "/var/log/xray/error.log",
        },
        "api": {
            "tag": "api",
            "listen": values.get("XRAY_API_SERVER", "127.0.0.1:10085").strip() or "127.0.0.1:10085",
            "services": ["StatsService", "HandlerService"],
        },
        "stats": {},
        "policy": policy,
        "inbounds": inbounds,
        "outbounds": outbounds,
    }
    if routing_rules:
        config["routing"] = {"rules": routing_rules}
    return merge_dynamic_routing(config, dynamic_payload)


def build_ai_node_values(values: dict[str, str]) -> dict[str, str]:
    """Build AI-node values without falling back to data-plane credentials."""
    required = {
        "AI_NODE_CLIENT_UUID": "XRAY_CLIENT_UUID",
        "AI_NODE_FLOW": "XRAY_FLOW",
        "AI_NODE_REALITY_PRIVATE_KEY": "XRAY_REALITY_PRIVATE_KEY",
        "AI_NODE_REALITY_PUBLIC_KEY": "XRAY_REALITY_PUBLIC_KEY",
        "AI_NODE_REALITY_SHORT_ID": "XRAY_REALITY_SHORT_ID",
        "AI_NODE_SERVER_NAME": "XRAY_SERVER_NAME",
        "AI_NODE_DEST": "XRAY_DEST",
        "AI_NODE_FINGERPRINT": "XRAY_FINGERPRINT",
    }
    missing = [key for key in required if not str(values.get(key, "")).strip()]
    if missing:
        raise ValueError(f"missing required AI node values: {', '.join(missing)}")

    ai_values = dict(values)
    ai_values.update(
        {
            "XRAY_CLIENT_UUID": values["AI_NODE_CLIENT_UUID"],
            "XRAY_FLOW": values["AI_NODE_FLOW"],
            "XRAY_REALITY_PRIVATE_KEY": values["AI_NODE_REALITY_PRIVATE_KEY"],
            "XRAY_REALITY_PUBLIC_KEY": values["AI_NODE_REALITY_PUBLIC_KEY"],
            "XRAY_REALITY_SHORT_ID": values["AI_NODE_REALITY_SHORT_ID"],
            "XRAY_SERVER_NAME": values["AI_NODE_SERVER_NAME"],
            "XRAY_DEST": values["AI_NODE_DEST"],
            "XRAY_FINGERPRINT": values["AI_NODE_FINGERPRINT"],
            "XRAY_LISTEN_HOST": values.get("AI_NODE_LISTEN_HOST", "0.0.0.0"),
            "XRAY_LOGLEVEL": values.get("AI_NODE_LOGLEVEL", values["XRAY_LOGLEVEL"]),
        }
    )
    return ai_values


def build_ai_node_config(values: dict[str, str]) -> dict:
    """Build the AI VLESS+REALITY config with local observability enabled."""
    ai_values = build_ai_node_values(values)
    listen_port = int(
        values.get("AI_NODE_LISTEN_PORT", "").strip()
        or values.get("AI_UPSTREAM_PORT", "").strip()
        or "27166"
    )
    metrics_listen = (
        str(values.get("AI_NODE_METRICS_LISTEN", "127.0.0.1:31097")).strip()
        or "127.0.0.1:31097"
    )
    return {
        "log": {
            "loglevel": ai_values["XRAY_LOGLEVEL"],
            "access": "/var/log/xray/ai-access.log",
            "error": "/var/log/xray/ai-error.log",
        },
        "metrics": {
            "tag": "ai-metrics",
            "listen": metrics_listen,
        },
        "stats": {},
        "policy": {
            "system": {
                "statsInboundUplink": True,
                "statsInboundDownlink": True,
                "statsOutboundUplink": True,
                "statsOutboundDownlink": True,
            }
        },
        "inbounds": [build_reality_inbound(ai_values, listen_port)],
        "outbounds": [{"protocol": "freedom", "tag": "direct"}],
    }


def resolve_public_port(values: dict[str, str]) -> int:
    entry_port = unified_port(values)
    if entry_port:
        return entry_port
    public_port_value = values.get("XRAY_PUBLIC_PORT", "").strip()
    if public_port_value:
        return int(public_port_value)
    return int(values["XRAY_LISTEN_PORT"])


def build_client_config(values: dict[str, str], panel_ports: list[int] | None = None) -> dict:
    public_port = resolve_public_port(values)
    stream_sockopt = build_stream_sockopt(values)
    config = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "listen": "127.0.0.1",
                "port": 10808,
                "protocol": "socks",
                "settings": {"udp": False},
            }
        ],
        "outbounds": [
            {
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": values["XRAY_PUBLIC_HOST"],
                            "port": public_port,
                            "users": [
                                {
                                    "id": values["XRAY_CLIENT_UUID"],
                                    "encryption": "none",
                                    "flow": values["XRAY_FLOW"],
                                }
                            ],
                        }
                    ]
                },
                "streamSettings": {
                    "network": "tcp",
                    "security": "reality",
                    "sockopt": stream_sockopt,
                    "realitySettings": {
                        "serverName": values["XRAY_SERVER_NAME"],
                        "fingerprint": values["XRAY_FINGERPRINT"],
                        "publicKey": values["XRAY_REALITY_PUBLIC_KEY"],
                        "shortId": values["XRAY_REALITY_SHORT_ID"],
                    },
                },
            }
        ],
    }
    if unified_port(values):
        users = {str(port): account_uuid(values, port) for port in (panel_ports or [])}
        config["panelSubscription"] = {"port": public_port, "users": users}
        # The diagnostic client uses an active account, never an unrestricted
        # shared credential on the unified entry. Empty account lists fail closed.
        config["outbounds"][0]["settings"]["vnext"][0]["users"][0]["id"] = next(
            iter(users.values()), "00000000-0000-4000-8000-000000000000"
        )
    return config


def build_share_url(values: dict[str, str]) -> str:
    public_port = resolve_public_port(values)
    params = urlencode(
        {
            "encryption": "none",
            "flow": values["XRAY_FLOW"],
            "security": "reality",
            "sni": values["XRAY_SERVER_NAME"],
            "fp": values["XRAY_FINGERPRINT"],
            "pbk": values["XRAY_REALITY_PUBLIC_KEY"],
            "sid": values["XRAY_REALITY_SHORT_ID"],
            "type": "tcp",
            "headerType": "none",
        }
    )
    tag = quote(values["XRAY_NODE_TAG"], safe="")
    return (
        f"vless://{values['XRAY_CLIENT_UUID']}@{values['XRAY_PUBLIC_HOST']}:"
        f"{public_port}?{params}#{tag}"
    )


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Render Xray REALITY config from .env")
    parser.add_argument("--env-file", default=str(BASE_DIR / ".env"))
    parser.add_argument("--config-out", default=str(RUNTIME_DIR / "config.json"))
    parser.add_argument("--client-out", default=str(RUNTIME_DIR / "client-test.json"))
    parser.add_argument("--share-out", default=str(RUNTIME_DIR / "client-share.txt"))
    parser.add_argument("--dynamic-routing-file", default=str(RUNTIME_DIR / "dynamic-routing.json"))
    parser.add_argument("--panel-ports-file", default=str(RUNTIME_DIR / "panel-ports.json"))
    parser.add_argument(
        "--backup-config-out",
        default="",
        help="Also render a control-plane backup node config that relays to --backup-upstream-url.",
    )
    parser.add_argument(
        "--backup-upstream-url",
        default="",
        help="vless:// URL the backup node relays every connection to (e.g. nat.qq.pw).",
    )
    parser.add_argument(
        "--ai-node-config-out",
        default="",
        help="Also render a minimal AI node config (freedom direct exit, same REALITY params).",
    )
    args = parser.parse_args()

    env_path = Path(args.env_file)
    if not env_path.is_file():
        print(f"env file not found: {env_path}", file=sys.stderr)
        return 1

    try:
        values = load_env_file(env_path)
        validate_env(values)
        dynamic_payload = load_optional_json(Path(args.dynamic_routing_file))
        panel_ports = load_panel_ports(Path(args.panel_ports_file))
        backup_config_out = str(args.backup_config_out or "").strip()
        backup_upstream_url = str(args.backup_upstream_url or "").strip()
        relay_outbound = (
            build_backup_relay_outbound(backup_upstream_url)
            if backup_config_out and backup_upstream_url
            else None
        )
        ai_node_config_out = str(args.ai_node_config_out or "").strip()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    write_json(Path(args.config_out), build_server_config(values, dynamic_payload, panel_ports))
    client_config = build_client_config(values, panel_ports)
    write_json(Path(args.client_out), client_config)

    if backup_config_out:
        # The backup node skips AI dynamic routing (which would re-add a
        # direct-exit branch). When a relay upstream URL is provided it forwards
        # all traffic to that upstream; when absent it exits via freedom direct.
        write_json(
            Path(backup_config_out),
            build_server_config(values, None, panel_ports, relay_outbound=relay_outbound),
        )

    if ai_node_config_out:
        write_json(Path(ai_node_config_out), build_ai_node_config(values))

    share_path = Path(args.share_out)
    share_path.parent.mkdir(parents=True, exist_ok=True)
    share_values = dict(values)
    share_values["XRAY_CLIENT_UUID"] = client_config["outbounds"][0]["settings"]["vnext"][0]["users"][0]["id"]
    share_path.write_text(build_share_url(share_values) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
