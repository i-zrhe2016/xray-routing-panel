"""Build and probe AI upstream candidates."""

from __future__ import annotations

import re
import socket
from urllib.parse import parse_qsl, unquote, urlparse

from app.xray.node.probes import reality_handshake_probe

from .common import format_timestamp, utc_now

UPSTREAM_LIST_SEPARATOR_RE = re.compile(r"[\n,;]+")


def join_host_port(host, port):
    host_text = str(host).strip()
    if ":" in host_text and not host_text.startswith("["):
        host_text = f"[{host_text}]"
    return f"{host_text}:{int(port)}"


def build_template_upstream_candidate(host, port):
    return {
        "upstream_host": str(host).strip(),
        "upstream_port": int(port),
        "candidate_type": "template",
    }


def parse_upstream_endpoint(raw, default_port=None, field_name="AI_UPSTREAMS"):
    text = str(raw or "").strip()
    if not text:
        raise ValueError(f"{field_name} contains an empty upstream entry")

    host = ""
    port_text = None
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise ValueError(f"{field_name} entry {text!r} has an invalid IPv6 format")
        host = text[1:end].strip()
        remainder = text[end + 1 :].strip()
        if remainder:
            if not remainder.startswith(":"):
                raise ValueError(f"{field_name} entry {text!r} must use [host]:port for IPv6")
            port_text = remainder[1:].strip()
    else:
        colon_count = text.count(":")
        if colon_count == 0:
            host = text
        elif colon_count == 1:
            host, port_text = text.rsplit(":", 1)
        else:
            host = text

    host = host.strip()
    if not host:
        raise ValueError(f"{field_name} entry {text!r} is missing a host")

    if port_text is None or not port_text:
        if default_port is None:
            raise ValueError(f"{field_name} entry {text!r} is missing a port")
        port = int(default_port)
    else:
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ValueError(f"{field_name} entry {text!r} has an invalid port") from exc

    if port <= 0 or port > 65535:
        raise ValueError(f"{field_name} entry {text!r} must use a port in 1..65535")

    return {"upstream_host": host, "upstream_port": port}


def parse_vless_fallback_url(raw, field_name="AI_UPSTREAM_FALLBACK_URL"):
    text = str(raw or "").strip()
    if not text:
        return None

    parsed = urlparse(text)
    if parsed.scheme.lower() != "vless":
        raise ValueError(f"{field_name} must use a vless:// URL")
    if not parsed.username:
        raise ValueError(f"{field_name} is missing the VLESS UUID")
    if not parsed.hostname:
        raise ValueError(f"{field_name} is missing the upstream host")
    if parsed.port is None:
        raise ValueError(f"{field_name} is missing the upstream port")

    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    network = str(params.get("type", "tcp")).strip().lower() or "tcp"
    if network != "tcp":
        raise ValueError(f"{field_name} currently supports only type=tcp")

    security = str(params.get("security", "none")).strip().lower() or "none"
    encryption = str(params.get("encryption", "none")).strip() or "none"
    user = {"id": unquote(parsed.username), "encryption": encryption}
    flow = str(params.get("flow", "")).strip()
    if flow:
        user["flow"] = flow

    outbound = {
        "tag": "ai_proxy",
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
        "streamSettings": {"network": network, "security": security},
    }

    if security == "reality":
        sni = str(params.get("sni", "")).strip()
        fingerprint = str(params.get("fp", "")).strip()
        public_key = str(params.get("pbk", "")).strip()
        short_id = str(params.get("sid", "")).strip()
        if not sni or not fingerprint or not public_key or not short_id:
            raise ValueError(f"{field_name} must include sni, fp, pbk, and sid when security=reality")
        reality_settings = {
            "serverName": sni,
            "fingerprint": fingerprint,
            "publicKey": public_key,
            "shortId": short_id,
        }
        pq_verify = str(params.get("pqv", "")).strip()
        if pq_verify:
            reality_settings["mldsa65Verify"] = pq_verify
        spider_x = str(params.get("spx", "")).strip()
        if spider_x:
            reality_settings["spiderX"] = spider_x
        outbound["streamSettings"]["realitySettings"] = reality_settings
    elif security not in {"none", ""}:
        raise ValueError(f"{field_name} currently supports only security=reality or security=none")

    return {
        "upstream_host": parsed.hostname,
        "upstream_port": int(parsed.port),
        "candidate_type": "share_url",
        "candidate_label": unquote(parsed.fragment).strip(),
        "proxy_payload_override": {"outbounds": [outbound]},
        "probe_server_name": str(
            (outbound.get("streamSettings", {}).get("realitySettings", {}) or {}).get("serverName", "")
        ).strip(),
    }


def parse_upstream_list(raw, default_port=None, field_name="AI_UPSTREAMS"):
    text = str(raw or "").strip()
    if not text:
        return []
    candidates = []
    for token in UPSTREAM_LIST_SEPARATOR_RE.split(text):
        token = token.strip()
        if token:
            candidates.append(parse_upstream_endpoint(token, default_port=default_port, field_name=field_name))
    return candidates


def dedupe_upstream_candidates(candidates):
    unique = []
    seen = set()
    for candidate in candidates:
        key = (
            str(candidate["upstream_host"]).strip().lower(),
            int(candidate["upstream_port"]),
            str(candidate.get("candidate_type", "template")).strip().lower(),
            str(candidate.get("candidate_label", "")).strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        normalized = dict(candidate)
        normalized["upstream_host"] = str(candidate["upstream_host"]).strip()
        normalized["upstream_port"] = int(candidate["upstream_port"])
        unique.append(normalized)
    return unique


def build_ai_upstream_candidates(
    primary_host,
    primary_port,
    upstreams_raw="",
    fallbacks_raw="",
    fallback_share_url="",
):
    if str(upstreams_raw or "").strip():
        candidates = [
            build_template_upstream_candidate(item["upstream_host"], item["upstream_port"])
            for item in parse_upstream_list(upstreams_raw, default_port=primary_port, field_name="AI_UPSTREAMS")
        ]
    else:
        candidates = [build_template_upstream_candidate(primary_host, primary_port)]
        fallback_candidate = parse_vless_fallback_url(fallback_share_url, field_name="AI_UPSTREAM_FALLBACK_URL")
        if fallback_candidate:
            candidates.append(fallback_candidate)
        candidates.extend(
            [
                build_template_upstream_candidate(item["upstream_host"], item["upstream_port"])
                for item in parse_upstream_list(
                    fallbacks_raw,
                    default_port=primary_port,
                    field_name="AI_UPSTREAM_FALLBACKS",
                )
            ]
        )

    candidates = dedupe_upstream_candidates(candidates)
    if not candidates:
        raise ValueError("at least one AI upstream must be configured")
    return candidates


def probe_ai_upstream_candidate(candidate, timeout_seconds, probe_controller=None):
    checked_at = format_timestamp(utc_now())
    reachable = False
    failure_reason = ""
    probe_server_name = str(candidate.get("probe_server_name", "")).strip()
    if probe_controller is not None:
        if probe_server_name:
            probe_result = probe_controller.probe_reality_endpoint(
                candidate["upstream_host"], candidate["upstream_port"], probe_server_name, timeout_seconds
            )
        else:
            probe_result = probe_controller.probe_tcp_endpoint(
                candidate["upstream_host"], candidate["upstream_port"], timeout_seconds
            )
        if not isinstance(probe_result, dict):
            probe_result = {
                "ok": False,
                "error": "AI 上游探测返回格式无效",
                "management_error": True,
                "method": "unknown",
            }
        reachable = bool(probe_result.get("ok"))
        failure_reason = str(probe_result.get("error", "")).strip()[:200]
        probe_method = str(probe_result.get("method", "tcp")).strip() or "tcp"
        management_error = bool(probe_result.get("management_error"))
    elif probe_server_name:
        probe_result = reality_handshake_probe(
            candidate["upstream_host"], candidate["upstream_port"], probe_server_name, timeout=timeout_seconds
        )
        reachable = bool(probe_result.get("ok"))
        failure_reason = str(probe_result.get("error", "")).strip()[:200]
        probe_method = "reality"
        management_error = False
    else:
        try:
            with socket.create_connection(
                (candidate["upstream_host"], int(candidate["upstream_port"])), timeout=timeout_seconds
            ):
                reachable = True
        except OSError as exc:
            failure_reason = str(exc)[:200]
        probe_method = "tcp"
        management_error = False

    result = dict(candidate)
    result.update(
        {
            "upstream_host": candidate["upstream_host"],
            "upstream_port": int(candidate["upstream_port"]),
            "is_reachable": reachable,
            "failure_reason": failure_reason,
            "checked_at": checked_at,
            "probe_method": probe_method,
            "probe_management_error": management_error,
        }
    )
    return result


def summarize_ai_target_candidate(candidate):
    host = str(candidate.get("upstream_host", "")).strip()
    port = candidate.get("upstream_port")
    try:
        port = int(port) if port is not None and str(port).strip() else None
    except (TypeError, ValueError):
        port = None
    summary = {
        "candidate_type": candidate.get("candidate_type", "template"),
        "is_reachable": bool(candidate.get("is_reachable")),
        "failure_reason": str(candidate.get("failure_reason", "")).strip(),
        "checked_at": str(candidate.get("checked_at", "")).strip(),
        "probe_method": str(candidate.get("probe_method", "tcp")).strip() or "tcp",
    }
    if host:
        summary["upstream_host"] = host
    if port:
        summary["upstream_port"] = port
    if candidate.get("probe_management_error"):
        summary["probe_management_error"] = True
    label = str(candidate.get("candidate_label", "")).strip()
    if label:
        summary["candidate_label"] = label
    return summary


__all__ = [
    "build_ai_upstream_candidates",
    "build_template_upstream_candidate",
    "dedupe_upstream_candidates",
    "join_host_port",
    "parse_upstream_endpoint",
    "parse_upstream_list",
    "parse_vless_fallback_url",
    "probe_ai_upstream_candidate",
    "summarize_ai_target_candidate",
]
