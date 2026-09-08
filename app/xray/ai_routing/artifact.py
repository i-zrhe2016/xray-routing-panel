"""Generate and apply AI routing artifacts and hourly reports."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from app.xray.config import DEFAULT_RENDER_MODULE

from .candidates import join_host_port
from .common import PLACEHOLDER_RE, env_bool, env_int, format_timestamp, save_json
from .selector import summarize_ai_target_for_report

UNSET_PROXY_PROTOCOL = "replace_me"


def build_proxy_sockopt_payload():
    sockopt = {}
    if env_bool("XRAY_TCP_FAST_OPEN", True):
        sockopt["tcpFastOpen"] = True

    keepalive_idle = env_int("XRAY_TCP_KEEPALIVE_IDLE", 180)
    if keepalive_idle > 0:
        sockopt["tcpKeepAliveIdle"] = keepalive_idle

    keepalive_interval = env_int("XRAY_TCP_KEEPALIVE_INTERVAL", 30)
    if keepalive_interval > 0:
        sockopt["tcpKeepAliveInterval"] = keepalive_interval

    return sockopt


def apply_default_proxy_sockopt(outbounds):
    default_sockopt = build_proxy_sockopt_payload()
    if not default_sockopt:
        return

    for outbound in outbounds:
        if not isinstance(outbound, dict):
            continue
        stream_settings = outbound.get("streamSettings")
        if not isinstance(stream_settings, dict):
            continue
        network = str(stream_settings.get("network", "tcp")).strip().lower()
        if network and network != "tcp":
            continue

        existing = stream_settings.get("sockopt", {})
        if not isinstance(existing, dict):
            existing = {}
        merged = dict(existing)
        for key, value in default_sockopt.items():
            merged.setdefault(key, value)
        merged.setdefault("domainStrategy", "UseIPv4")
        stream_settings["sockopt"] = merged


def build_default_proxy_payload(ai_target):
    return {
        "outbounds": [
            {
                "tag": "ai_proxy",
                "protocol": "freedom",
                "settings": {
                    "domainStrategy": "UseIPv4",
                    "redirect": join_host_port(ai_target["upstream_host"], ai_target["upstream_port"]),
                    "proxyProtocol": 0,
                    "finalRules": [{"action": "allow"}],
                },
            }
        ]
    }


def render_proxy_template(template_path, ai_target, panel_target):
    override_payload = ai_target.get("proxy_payload_override")
    if isinstance(override_payload, dict):
        outbounds = override_payload.get("outbounds", [])
        if not isinstance(outbounds, list) or not outbounds:
            return None, "proxy_payload_override_has_no_outbounds"
        apply_default_proxy_sockopt(outbounds)
        return {"outbounds": outbounds}, "share_url_override"

    if not template_path or not template_path.is_file():
        return build_default_proxy_payload(ai_target), "builtin_freedom_redirect"
    raw = template_path.read_text(encoding="utf-8")
    replacements = {
        "AI_UPSTREAM_HOST": str(ai_target["upstream_host"]),
        "AI_UPSTREAM_PORT": str(ai_target["upstream_port"]),
        "PANEL_LISTEN_PORT": str(panel_target["listen_port"]) if panel_target else "",
        "PANEL_UPSTREAM_HOST": str(panel_target["upstream_host"]) if panel_target else str(ai_target["upstream_host"]),
        "PANEL_UPSTREAM_PORT": str(panel_target["upstream_port"]) if panel_target else str(ai_target["upstream_port"]),
    }

    def replace(match):
        return replacements.get(match.group(1), match.group(0))

    rendered = PLACEHOLDER_RE.sub(replace, raw)
    try:
        parsed = json.loads(rendered)
    except json.JSONDecodeError as exc:
        return None, f"invalid_proxy_template_json: {exc}"

    if isinstance(parsed, dict) and "outbounds" in parsed:
        outbounds = parsed.get("outbounds")
    elif isinstance(parsed, list):
        outbounds = parsed
    else:
        outbounds = [parsed]

    if not isinstance(outbounds, list) or not outbounds:
        return None, "proxy_template_has_no_outbounds"

    first = outbounds[0]
    if not isinstance(first, dict):
        return None, "proxy_template_first_outbound_invalid"
    if str(first.get("protocol", "")).strip() == UNSET_PROXY_PROTOCOL:
        return None, "proxy_template_protocol_placeholder_not_replaced"
    if str(first.get("tag", "")).strip() != "ai_proxy":
        first["tag"] = "ai_proxy"
    apply_default_proxy_sockopt(outbounds)
    return {"outbounds": outbounds}, ""


def extract_probe_server_name(proxy_payload):
    if not isinstance(proxy_payload, dict):
        return ""
    for outbound in proxy_payload.get("outbounds", []):
        if not isinstance(outbound, dict) or outbound.get("tag") != "ai_proxy":
            continue
        stream_settings = outbound.get("streamSettings", {})
        reality_settings = stream_settings.get("realitySettings", {}) if isinstance(stream_settings, dict) else {}
        if isinstance(reality_settings, dict):
            server_name = str(reality_settings.get("serverName", "")).strip()
            if server_name:
                return server_name
    return ""


def resolve_probe_server_name(template_path, candidate, panel_target, explicit_server_name=""):
    candidate_server_name = str(candidate.get("probe_server_name", "")).strip()
    if candidate_server_name:
        return candidate_server_name
    explicit = str(explicit_server_name or "").strip()
    if explicit:
        return explicit
    try:
        proxy_payload, _reason = render_proxy_template(template_path, candidate, panel_target)
    except (OSError, ValueError, TypeError):
        return ""
    return extract_probe_server_name(proxy_payload)


def write_routing_fragment(path, ai_domains, proxy_payload):
    if not ai_domains or proxy_payload is None:
        path.unlink(missing_ok=True)
        return False
    fragment = {
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                {
                    "type": "field",
                    "domain": [f"domain:{domain}" for domain in sorted(ai_domains)],
                    "outboundTag": "ai_proxy",
                }
            ],
        },
        "outbounds": proxy_payload["outbounds"],
    }
    save_json(path, fragment)
    return True


def build_domain_report(state, cutoff, now, decisions, ai_target, panel_target, route_status):
    route_status_code = str(route_status.get("status", "unknown") or "unknown").strip()

    def domain_route(classification):
        if classification != "ai":
            return {
                "outbound_tag": "direct",
                "path": "normal_data_plane",
                "target": None,
                "status": route_status_code,
                "reason": "classification_not_ai",
            }
        if route_status_code == "applied":
            target = None
            if isinstance(ai_target, dict):
                target_host = str(ai_target.get("upstream_host", "")).strip()
                try:
                    target_port = int(ai_target.get("upstream_port"))
                except (TypeError, ValueError):
                    target_port = None
                if target_host and target_port:
                    target = {"upstream_host": target_host, "upstream_port": target_port}
            return {
                "outbound_tag": "ai_proxy",
                "path": "ai_node",
                "target": target,
                "status": route_status_code,
                "reason": str(route_status.get("reason", "") or "").strip(),
            }
        if route_status_code in {
            "disabled",
            "idle",
            "fallback_to_primary",
            "manual_fallback",
            "manual_target_unreachable",
            "pending_proxy_template",
        }:
            return {
                "outbound_tag": "direct",
                "path": "normal_data_plane",
                "target": None,
                "status": route_status_code,
                "reason": str(route_status.get("reason", "") or "").strip() or "ai_route_not_applied",
            }
        return {
            "outbound_tag": "unknown",
            "path": "unknown",
            "target": None,
            "status": route_status_code,
            "reason": str(route_status.get("reason", "") or "").strip() or "route_status_unavailable",
        }

    domains = {}
    protocols = {}
    for item in state["events"]:
        decision = decisions["domains"].get(item["domain"], {})
        domain_item = domains.setdefault(
            item["domain"],
            {
                "domain": item["domain"],
                "hits": 0,
                "first_seen": item["seen_at"],
                "last_seen": item["seen_at"],
                "protocols": set(),
                "classification": decision.get("classification", "unknown"),
                "reason": decision.get("reason", ""),
                "source": str(decision.get("source", "") or "").strip(),
                "model": str(decision.get("model", "") or "").strip(),
            },
        )
        domain_item["hits"] += 1
        domain_item["protocols"].add(item["protocol"])
        domain_item["first_seen"] = min(domain_item["first_seen"], item["seen_at"])
        domain_item["last_seen"] = max(domain_item["last_seen"], item["seen_at"])
        protocols[item["protocol"]] = protocols.get(item["protocol"], 0) + 1

    domain_items = sorted(
        (
            {
                "domain": item["domain"],
                "hits": item["hits"],
                "first_seen": format_timestamp(item["first_seen"]),
                "last_seen": format_timestamp(item["last_seen"]),
                "protocols": sorted(item["protocols"]),
                "classification": item["classification"],
                "reason": item["reason"],
                "source": item["source"] or "unknown",
                "model": item["model"],
                "traffic_route": domain_route(item["classification"]),
            }
            for item in domains.values()
        ),
        key=lambda item: (-item["hits"], item["domain"]),
    )
    ai_domains = [item["domain"] for item in domain_items if item["classification"] == "ai"]
    return {
        "generated_at": format_timestamp(now),
        "window_start": format_timestamp(cutoff),
        "window_end": format_timestamp(now),
        "unique_domains": len(domain_items),
        "ai_domains": ai_domains,
        "domains": domain_items,
        "protocols": [{"protocol": protocol, "hits": hits} for protocol, hits in sorted(protocols.items())],
        "ai_target": summarize_ai_target_for_report(ai_target) if isinstance(ai_target, dict) else None,
        "panel_target": panel_target,
        "route_status": route_status,
    }


def write_domain_report(output_dir, report):
    output_dir.mkdir(parents=True, exist_ok=True)
    history_dir = output_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    latest_json = output_dir / "latest.json"
    latest_txt = output_dir / "latest.txt"
    stamp = report["window_end"].replace(":", "").replace("-", "").replace("+00:00", "Z")
    history_json = history_dir / f"{stamp}.json"
    history_txt = history_dir / f"{stamp}.txt"

    payload = json.dumps(report, indent=2, ensure_ascii=True) + "\n"
    from app.xray.file_io import write_text_atomic

    write_text_atomic(latest_json, payload)
    write_text_atomic(history_json, payload)

    lines = [
        f"generated_at: {report['generated_at']}",
        f"window_start: {report['window_start']}",
        f"window_end: {report['window_end']}",
        f"unique_domains: {report['unique_domains']}",
        f"ai_domains: {len(report['ai_domains'])}",
        f"route_status: {report['route_status'].get('status', 'unknown')}",
    ]
    if report["route_status"].get("config_retried"):
        lines.append("config_retried: true")
    if report["route_status"].get("config_apply_status"):
        lines.append(f"config_apply_status: {report['route_status']['config_apply_status']}")
    if report.get("panel_db_status"):
        lines.append(
            "panel_db_status: "
            f"{report['panel_db_status'].get('status', 'unknown')} "
            f"(ai_domains={report['panel_db_status'].get('domains_upserted', 0)}, "
            f"observations={report['panel_db_status'].get('observations_upserted', 0)})"
        )
    if report.get("ai_target"):
        target = report["ai_target"]
        if not isinstance(target, dict):
            lines.append("ai_target: unavailable")
        else:
            target_host = str(target.get("upstream_host", "")).strip()
            target_port = target.get("upstream_port")
            lines.append(
                f"ai_target: {target_host}:{target_port}" if target_host and target_port else "ai_target: unavailable"
            )
            try:
                candidate_count = int(target.get("candidate_count", 0) or 0)
            except (TypeError, ValueError):
                candidate_count = 0
            if candidate_count > 1:
                lines.append(
                    "ai_target_selection: "
                    f"{target.get('selected_number', '?')}/{candidate_count} "
                    f"({'fallback' if target.get('failover_active') else 'primary'})"
                )
            if target.get("probe_status"):
                lines.append(f"ai_target_probe_status: {target['probe_status']}")
            candidates = target.get("candidates", [])
            if candidates:
                candidate_lines = []
                for item in candidates:
                    if not isinstance(item, dict):
                        continue
                    host = str(item.get("upstream_host", "")).strip()
                    port = item.get("upstream_port")
                    if host and port:
                        candidate_lines.append(f"{host}:{port}({'ok' if item.get('is_reachable') else 'down'})")
                if candidate_lines:
                    lines.append("ai_target_candidates: " + ", ".join(candidate_lines))
    if report["panel_target"]:
        lines.append(
            "panel_target: "
            f"{report['panel_target']['upstream_host']}:{report['panel_target']['upstream_port']} "
            f"(listen_port={report['panel_target']['listen_port']})"
        )
    lines.append("")
    if report["domains"]:
        for item in report["domains"]:
            protocols = ",".join(item["protocols"])
            lines.append(
                f"{item['domain']}\thits={item['hits']}\tclass={item['classification']}\t"
                f"source={item.get('source', 'unknown')}\troute={item.get('traffic_route', {}).get('outbound_tag', 'unknown')}\t"
                f"last_seen={item['last_seen']}\tprotocols={protocols}"
            )
    else:
        lines.append("no domains observed in the last window")
    text = "\n".join(lines) + "\n"
    write_text_atomic(latest_txt, text)
    write_text_atomic(history_txt, text)


def rerender_config(render_script, env_file, config_out, client_out, share_out, dynamic_routing_file):
    render_entry = str(render_script).strip() or DEFAULT_RENDER_MODULE
    panel_ports_file = Path(config_out).with_name("panel-ports.json")
    command = [sys.executable]
    if render_entry.endswith(".py") or "/" in render_entry or "\\" in render_entry:
        command.append(render_entry)
    else:
        command.extend(["-m", render_entry])
    command.extend(
        [
            "--env-file",
            str(env_file),
            "--config-out",
            str(config_out),
            "--client-out",
            str(client_out),
            "--share-out",
            str(share_out),
            "--dynamic-routing-file",
            str(dynamic_routing_file),
            "--panel-ports-file",
            str(panel_ports_file),
        ]
    )
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "render_config failed"
        raise RuntimeError(detail)


def restart_xray_container(container_name, timeout_seconds):
    if not container_name:
        return
    completed = subprocess.run(
        ["docker", "restart", container_name], capture_output=True, text=True, check=False, timeout=timeout_seconds
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "docker restart failed"
        raise RuntimeError(detail)


def restart_xray_command(command, timeout_seconds):
    if not command:
        return
    completed = subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=timeout_seconds, shell=True
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "restart command failed"
        raise RuntimeError(detail)


__all__ = [
    "apply_default_proxy_sockopt",
    "build_default_proxy_payload",
    "build_domain_report",
    "build_proxy_sockopt_payload",
    "extract_probe_server_name",
    "render_proxy_template",
    "rerender_config",
    "resolve_probe_server_name",
    "restart_xray_command",
    "restart_xray_container",
    "write_domain_report",
    "write_routing_fragment",
]
