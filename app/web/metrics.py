"""Prometheus text-format `/metrics` endpoint.

Exposes the panel's already-collected state (traffic, probes, DNS failover, AI
routing, data-plane status) for scraping by Prometheus. Host CPU/mem/disk come
from node_exporter, not this endpoint.

The handler is strictly read-only on the scrape path: it reads the tables the
background maintenance loop keeps fresh, reads the AI node's loopback-only
expvar endpoint, and incrementally tails the local AI access log for bounded
destination aggregates. It never calls ``sync_traffic_state``/
``dns_failover_status``/probes (any of which may do I/O). The data-plane status
check and AI metrics reads are wrapped in TTL caches.

The format is hand-rolled (no ``prometheus_client`` dependency) to keep the
panel on Flask + stdlib.
"""

import hmac
import json
import re
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from flask import Response, current_app, request

from ..config import (
    AI_NODE_ACCESS_LOG_PATH,
    AI_NODE_DESTINATION_MAX_LABELS,
    AI_NODE_DESTINATION_WINDOW_SECONDS,
    AI_NODE_METRICS_URL,
    METRICS_DP_TTL,
    METRICS_TOKEN,
    XRAY_STATS_QUERY_TIMEOUT,
)
from .core import route, state

_AI_ACCESS_LINE_RE = re.compile(
    r"\baccepted\s+(?P<network>tcp|udp):(?P<target>\S+)"
)
_AI_LOG_TIMESTAMP_RE = re.compile(
    r"^(?P<timestamp>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)"
)
_AI_DESTINATION_READ_CHUNK_BYTES = 8 * 1024 * 1024
_METRICS_STATE_EXTENSION = "panel.metrics"


def _new_metrics_state():
    return {
        # Cache for the data-plane running check (the one SSH call on this
        # path). All mutable scrape state belongs to one Flask application.
        "data_plane_cache": {"val": 0, "ts": 0.0},
        "ai_metrics_cache": {
            "ts": 0.0,
            "available": 0,
            "received": 0,
            "sent": 0,
            "egress_received": 0,
            "egress_sent": 0,
        },
        "destination_cache": {
            "ts": 0.0,
            "available": 0,
            "window_seconds": AI_NODE_DESTINATION_WINDOW_SECONDS,
            "requests": [],
            "other_requests": 0,
        },
        "destination_log_state": {
            "path": "",
            "inode": None,
            "offset": 0,
            "partial": "",
            "events": deque(),
        },
        "destination_lock": threading.Lock(),
    }


# Keep direct unit-level calls usable when no Flask application context exists.
# Requests and app-context calls use the per-Flask-app state below instead.
_FALLBACK_METRICS_STATE = _new_metrics_state()


def _metrics_state():
    try:
        extensions = current_app.extensions
    except RuntimeError:
        return _FALLBACK_METRICS_STATE

    metrics_state = extensions.get(_METRICS_STATE_EXTENSION)
    if metrics_state is None:
        metrics_state = _new_metrics_state()
        extensions[_METRICS_STATE_EXTENSION] = metrics_state
    return metrics_state


def _esc(value):
    """Escape a Prometheus label value (\\, ", newline)."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
    )


def _iso_to_epoch(value):
    """Parse a stored ISO timestamp to a Unix epoch float, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return None


def _data_plane_running_cached():
    cache = _metrics_state()["data_plane_cache"]
    now = time.monotonic()
    if now - cache["ts"] >= METRICS_DP_TTL:
        try:
            cache["val"] = 1 if state.data_plane_running() else 0
        except Exception:
            cache["val"] = 0
        cache["ts"] = now
    return cache["val"]


def _read_ai_node_metrics():
    """Read the loopback-only Xray expvar endpoint and keep only byte totals."""
    if not AI_NODE_METRICS_URL:
        return None
    parsed = urlsplit(AI_NODE_METRICS_URL)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    request = Request(AI_NODE_METRICS_URL, headers={"Accept": "application/json"})
    with urlopen(request, timeout=max(1, int(XRAY_STATS_QUERY_TIMEOUT))) as response:
        payload = json.loads(response.read(8 * 1024 * 1024).decode("utf-8"))
    return _parse_ai_node_metrics_payload(payload)


def _parse_ai_node_metrics_payload(payload):
    """Reduce Xray expvar data to bounded, direction-only byte counters."""
    if not isinstance(payload, dict) or not isinstance(payload.get("stats"), dict):
        return None

    stats = payload["stats"]
    inbound = stats.get("inbound")
    outbound = stats.get("outbound")
    if not isinstance(inbound, dict) or not isinstance(outbound, dict):
        return None

    result = {
        "available": 1,
        "received": 0,
        "sent": 0,
        "egress_received": 0,
        "egress_sent": 0,
    }
    for tag, counters in inbound.items():
        if not str(tag).startswith("panel-") or not isinstance(counters, dict):
            continue
        result["received"] += max(0, int(counters.get("uplink", 0) or 0))
        result["sent"] += max(0, int(counters.get("downlink", 0) or 0))
    for tag, counters in outbound.items():
        if str(tag) != "direct" or not isinstance(counters, dict):
            continue
        result["egress_sent"] += max(0, int(counters.get("uplink", 0) or 0))
        result["egress_received"] += max(0, int(counters.get("downlink", 0) or 0))
    return result


def _ai_node_metrics_cached():
    cache = _metrics_state()["ai_metrics_cache"]
    now = time.monotonic()
    if now - cache["ts"] >= METRICS_DP_TTL:
        try:
            result = _read_ai_node_metrics()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            result = None
        if result is None:
            result = {
                "available": 0,
                "received": 0,
                "sent": 0,
                "egress_received": 0,
                "egress_sent": 0,
            }
        cache.update(result)
        cache["ts"] = now
    return cache


def _parse_ai_log_timestamp(line, fallback):
    match = _AI_LOG_TIMESTAMP_RE.match(line)
    if not match:
        return fallback
    raw = match.group("timestamp")
    for format_string in ("%Y/%m/%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(raw, format_string).replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            continue
    return fallback


def _split_ai_destination(target):
    target = str(target or "").rstrip(".,")
    if target.startswith("["):
        closing = target.find("]")
        if closing < 0 or target[closing + 1 : closing + 2] != ":":
            return None
        host = target[1:closing]
        port = target[closing + 2 :]
    else:
        if ":" not in target:
            return None
        host, port = target.rsplit(":", 1)
    if not port.isdigit():
        return None
    port_number = int(port)
    if port_number < 0 or port_number > 65535:
        return None
    host = host.strip().strip("[]").lower().rstrip(".")
    if not host:
        return None
    return host, str(port_number)


def _parse_ai_access_line(line, fallback_timestamp=None):
    """Parse one Xray access-log accepted line into a bounded destination key."""
    if not isinstance(line, str):
        return None
    match = _AI_ACCESS_LINE_RE.search(line)
    if not match:
        return None
    destination = _split_ai_destination(match.group("target"))
    if destination is None:
        return None
    fallback = time.time() if fallback_timestamp is None else float(fallback_timestamp)
    host, port = destination
    return {
        "timestamp": _parse_ai_log_timestamp(line, fallback),
        "domain": host,
        "port": port,
        "network": match.group("network"),
    }


def _summarize_ai_destination_events(events, now, window_seconds, max_labels):
    """Return top recent destinations while bounding Prometheus label count."""
    cutoff = float(now) - int(window_seconds)
    counts = Counter()
    last_seen = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        try:
            timestamp = float(event["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        if timestamp < cutoff:
            continue
        key = (event.get("domain"), event.get("port"), event.get("network"))
        if not all(key):
            continue
        counts[key] += 1
        last_seen[key] = max(timestamp, last_seen.get(key, timestamp))

    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    top = ordered[: int(max_labels)]
    top_count = sum(count for _, count in top)
    return {
        "available": 1,
        "window_seconds": int(window_seconds),
        "requests": [
            {
                "domain": key[0],
                "port": key[1],
                "network": key[2],
                "requests": count,
                "requests_per_second": count / max(1, int(window_seconds)),
                "last_seen": last_seen[key],
            }
            for key, count in top
        ],
        "other_requests": max(0, sum(counts.values()) - top_count),
    }


def _read_ai_destination_metrics():
    """Tail the AI access log and aggregate recent accepted destinations."""
    if not AI_NODE_ACCESS_LOG_PATH:
        return {
            "available": 0,
            "window_seconds": AI_NODE_DESTINATION_WINDOW_SECONDS,
            "requests": [],
            "other_requests": 0,
        }

    path = Path(AI_NODE_ACCESS_LOG_PATH)
    now = time.time()
    try:
        info = path.stat()
    except OSError:
        return {
            "available": 0,
            "window_seconds": AI_NODE_DESTINATION_WINDOW_SECONDS,
            "requests": [],
            "other_requests": 0,
        }

    metrics_state = _metrics_state()
    with metrics_state["destination_lock"]:
        log_state = metrics_state["destination_log_state"]
        reset = (
            log_state["path"] != str(path)
            or log_state["inode"] != info.st_ino
            or info.st_size < log_state["offset"]
        )
        if reset:
            log_state.update(
                {
                    "path": str(path),
                    "inode": info.st_ino,
                    "offset": 0,
                    "partial": "",
                    "events": deque(),
                }
            )

        initial_tail = log_state["offset"] == 0 and info.st_size > _AI_DESTINATION_READ_CHUNK_BYTES
        if initial_tail:
            log_state["offset"] = info.st_size - _AI_DESTINATION_READ_CHUNK_BYTES

        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(log_state["offset"])
                chunk = handle.read(_AI_DESTINATION_READ_CHUNK_BYTES)
                log_state["offset"] = handle.tell()
        except OSError:
            return {
                "available": 0,
                "window_seconds": AI_NODE_DESTINATION_WINDOW_SECONDS,
                "requests": [],
                "other_requests": 0,
            }

        text = log_state["partial"] + chunk
        lines = text.splitlines()
        if text and not text.endswith(("\n", "\r")):
            log_state["partial"] = lines.pop() if lines else text
        else:
            log_state["partial"] = ""
        if initial_tail and lines:
            lines = lines[1:]

        for line in lines:
            event = _parse_ai_access_line(line, fallback_timestamp=now)
            if event is not None:
                log_state["events"].append(event)

        cutoff = now - AI_NODE_DESTINATION_WINDOW_SECONDS
        while log_state["events"] and log_state["events"][0]["timestamp"] < cutoff:
            log_state["events"].popleft()
        return _summarize_ai_destination_events(
            log_state["events"],
            now,
            AI_NODE_DESTINATION_WINDOW_SECONDS,
            AI_NODE_DESTINATION_MAX_LABELS,
        )


def _ai_destination_metrics_cached():
    cache = _metrics_state()["destination_cache"]
    now = time.monotonic()
    if now - cache["ts"] >= METRICS_DP_TTL:
        try:
            result = _read_ai_destination_metrics()
        except (OSError, TypeError, ValueError):
            result = {
                "available": 0,
                "window_seconds": AI_NODE_DESTINATION_WINDOW_SECONDS,
                "requests": [],
                "other_requests": 0,
            }
        cache.update(result)
        cache["ts"] = now
    return cache


class _Renderer:
    """Accumulates metric families into Prometheus 0.0.4 text format."""

    def __init__(self):
        self._lines = []

    def family(self, name, mtype, help_text):
        self._lines.append(f"# HELP {name} {help_text}")
        self._lines.append(f"# TYPE {name} {mtype}")

    def sample(self, name, value, labels=None):
        if labels:
            rendered = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
            self._lines.append(f"{name}{{{rendered}}} {value}")
        else:
            self._lines.append(f"{name} {value}")

    def text(self):
        return "\n".join(self._lines) + "\n"


def _collect():
    r = _Renderer()

    ports = state.query_ports()
    summary = state.query_summary(ports)

    # --- Liveness ---------------------------------------------------------
    r.family("xray_panel_up", "gauge", "Panel control plane is responding (always 1).")
    r.sample("xray_panel_up", 1)

    # --- Business: per-port traffic & connections -------------------------
    r.family(
        "xray_panel_port_traffic_bytes_total",
        "counter",
        "Cumulative bytes per listen port and direction (resets on quota/traffic reset).",
    )
    for p in ports:
        labels = {"port": p["listen_port"], "note": p.get("note") or ""}
        r.sample(
            "xray_panel_port_traffic_bytes_total",
            int(p["total_bytes_sent"]),
            {**labels, "direction": "sent"},
        )
        r.sample(
            "xray_panel_port_traffic_bytes_total",
            int(p["total_bytes_received"]),
            {**labels, "direction": "received"},
        )

    r.family(
        "xray_panel_port_connections_total",
        "counter",
        "Cumulative connections per listen port (resets on quota/traffic reset).",
    )
    for p in ports:
        r.sample(
            "xray_panel_port_connections_total",
            int(p["total_connections"]),
            {"port": p["listen_port"], "note": p.get("note") or ""},
        )

    # --- Business: port counts -------------------------------------------
    r.family("xray_panel_ports_total", "gauge", "Total configured ports.")
    r.sample("xray_panel_ports_total", summary["total_ports"])
    r.family("xray_panel_ports_enabled", "gauge", "Ports with enabled=1.")
    r.sample("xray_panel_ports_enabled", sum(1 for p in ports if p["enabled"]))
    r.family("xray_panel_ports_active", "gauge", "Ports in 'active' status.")
    r.sample("xray_panel_ports_active", summary["active_ports"])
    r.family("xray_panel_ports_expired", "gauge", "Ports in 'expired' status.")
    r.sample("xray_panel_ports_expired", summary["expired_ports"])
    r.family("xray_panel_ports_quota", "gauge", "Ports in 'quota' (traffic-exhausted) status.")
    r.sample("xray_panel_ports_quota", summary["quota_ports"])
    r.family("xray_panel_ports_disabled", "gauge", "Ports in 'disabled' status.")
    r.sample("xray_panel_ports_disabled", summary["disabled_ports"])

    # --- Availability: per-port probe ------------------------------------
    r.family(
        "xray_panel_port_reachable",
        "gauge",
        "Latest upstream probe result per port (1=reachable, 0=not). Omitted if never probed.",
    )
    for p in ports:
        reachable = p.get("probe_is_reachable")
        if reachable is None:
            continue
        r.sample(
            "xray_panel_port_reachable",
            1 if int(reachable) else 0,
            {"port": p["listen_port"], "note": p.get("note") or ""},
        )

    r.family(
        "xray_panel_port_probe_timestamp_seconds",
        "gauge",
        "Unix time of the latest probe per port (use time()-metric for probe age).",
    )
    for p in ports:
        epoch = _iso_to_epoch(p.get("probe_checked_at"))
        if epoch is not None:
            r.sample(
                "xray_panel_port_probe_timestamp_seconds",
                int(epoch),
                {"port": p["listen_port"]},
            )

    # --- Xray process / data plane ---------------------------------------
    mode = state.data_plane.mode
    r.family("xray_panel_data_plane_configured", "gauge", "Data plane is configured (1/0).")
    try:
        configured = 1 if state.data_plane_configured() else 0
    except Exception:
        configured = 0
    r.sample("xray_panel_data_plane_configured", configured, {"mode": mode})

    r.family("xray_panel_data_plane_running", "gauge", "Data plane (xray) is running (1/0). TTL-cached.")
    r.sample("xray_panel_data_plane_running", _data_plane_running_cached(), {"mode": mode})

    # --- DNS failover (read the state table directly; no resolve_public_ip) -
    config = state.dns_failover_manager.config
    r.family("xray_panel_dns_failover_enabled", "gauge", "DNS failover is enabled (1/0).")
    r.sample("xray_panel_dns_failover_enabled", 1 if config.enabled else 0)

    try:
        with state.connect() as conn:
            row = conn.execute(
                "SELECT * FROM dns_failover_state WHERE singleton_id = 1"
            ).fetchone()
        dns = dict(row) if row is not None else {}
    except Exception:
        dns = {}

    current_target = dns.get("current_target") or "primary"
    r.family(
        "xray_panel_dns_failover_target_info",
        "gauge",
        "Current DNS failover target (value=1 on the active target label).",
    )
    r.sample("xray_panel_dns_failover_target_info", 1, {"target": current_target})

    r.family(
        "xray_panel_dns_failover_last_probe_healthy",
        "gauge",
        "Last DNS failover probe was healthy (1/0).",
    )
    r.sample(
        "xray_panel_dns_failover_last_probe_healthy",
        1 if dns.get("last_probe_status") == "healthy" else 0,
    )
    r.family(
        "xray_panel_dns_failover_consecutive_failures",
        "gauge",
        "Consecutive failed DNS failover probes.",
    )
    r.sample(
        "xray_panel_dns_failover_consecutive_failures",
        int(dns.get("consecutive_failures") or 0),
    )
    r.family(
        "xray_panel_dns_failover_consecutive_successes",
        "gauge",
        "Consecutive successful DNS failover probes.",
    )
    r.sample(
        "xray_panel_dns_failover_consecutive_successes",
        int(dns.get("consecutive_successes") or 0),
    )

    peak = state.dns_failover_peak_window_status()
    r.family(
        "xray_panel_dns_failover_peak_window_active",
        "gauge",
        "Peak-window preference is currently active (1/0).",
    )
    r.sample(
        "xray_panel_dns_failover_peak_window_active",
        1 if peak.get("active") else 0,
    )

    # --- AI node ---------------------------------------------------------
    ai_node_status = state.ai_node_status()
    r.family(
        "xray_panel_ai_node_configured",
        "gauge",
        "AI node is managed via SSH (1/0).",
    )
    r.sample(
        "xray_panel_ai_node_configured",
        1 if ai_node_status.get("configured") else 0,
    )
    r.family(
        "xray_panel_ai_node_running",
        "gauge",
        "AI node is reachable (1/0).",
    )
    r.sample(
        "xray_panel_ai_node_running",
        1 if ai_node_status.get("reachable") else 0,
    )

    ai_metrics = _ai_node_metrics_cached()
    r.family(
        "xray_panel_ai_node_metrics_available",
        "gauge",
        "AI node Xray metrics endpoint is available (1/0).",
    )
    r.sample("xray_panel_ai_node_metrics_available", ai_metrics["available"])
    r.family(
        "xray_panel_ai_node_traffic_bytes_total",
        "counter",
        "Cumulative AI node inbound traffic bytes by proxy direction.",
    )
    r.sample(
        "xray_panel_ai_node_traffic_bytes_total",
        ai_metrics["received"],
        {"direction": "received"},
    )
    r.sample(
        "xray_panel_ai_node_traffic_bytes_total",
        ai_metrics["sent"],
        {"direction": "sent"},
    )
    r.family(
        "xray_panel_ai_node_egress_bytes_total",
        "counter",
        "Cumulative AI node direct-egress traffic bytes by direction.",
    )
    r.sample(
        "xray_panel_ai_node_egress_bytes_total",
        ai_metrics["egress_received"],
        {"direction": "received"},
    )
    r.sample(
        "xray_panel_ai_node_egress_bytes_total",
        ai_metrics["egress_sent"],
        {"direction": "sent"},
    )

    ai_destinations = _ai_destination_metrics_cached()
    r.family(
        "xray_panel_ai_destination_log_available",
        "gauge",
        "AI access log is readable for destination analysis (1/0).",
    )
    r.sample("xray_panel_ai_destination_log_available", ai_destinations["available"])
    r.family(
        "xray_panel_ai_destination_window_seconds",
        "gauge",
        "Recent AI destination request analysis window in seconds.",
    )
    r.sample(
        "xray_panel_ai_destination_window_seconds",
        ai_destinations["window_seconds"],
    )
    r.family(
        "xray_panel_ai_destination_requests",
        "gauge",
        "Accepted AI node requests in the recent analysis window by destination.",
    )
    r.family(
        "xray_panel_ai_destination_requests_per_second",
        "gauge",
        "Average accepted AI node requests per second in the recent analysis window.",
    )
    r.family(
        "xray_panel_ai_destination_last_seen_timestamp_seconds",
        "gauge",
        "Unix time when the AI destination was last accepted in the recent window.",
    )
    for destination in ai_destinations["requests"]:
        labels = {
            "domain": destination["domain"],
            "port": destination["port"],
            "network": destination["network"],
        }
        r.sample(
            "xray_panel_ai_destination_requests",
            destination["requests"],
            labels,
        )
        r.sample(
            "xray_panel_ai_destination_requests_per_second",
            destination["requests_per_second"],
            labels,
        )
        r.sample(
            "xray_panel_ai_destination_last_seen_timestamp_seconds",
            int(destination["last_seen"]),
            labels,
        )
    r.family(
        "xray_panel_ai_destination_other_requests",
        "gauge",
        "Recent AI destination requests omitted after the top-label limit.",
    )
    r.sample(
        "xray_panel_ai_destination_other_requests",
        ai_destinations["other_requests"],
    )

    # --- Backup Xray mode ------------------------------------------------
    backup_mode = state.backup_xray_mode() if hasattr(state, "backup_xray_mode") else "disabled"
    r.family(
        "xray_panel_backup_xray_mode_info",
        "gauge",
        "Control-plane backup Xray mode (value=1 on the active mode label).",
    )
    r.sample(
        "xray_panel_backup_xray_mode_info",
        1,
        {"mode": backup_mode},
    )

    # --- AI routing -------------------------------------------------------
    ai = state.query_ai_domain_aggregate()
    r.family("xray_panel_ai_domains_total", "gauge", "Number of classified AI domains.")
    r.sample("xray_panel_ai_domains_total", int(ai["total_ai_domains"]))
    r.family("xray_panel_ai_domain_hits_total", "counter", "Cumulative AI domain hits.")
    r.sample("xray_panel_ai_domain_hits_total", int(ai["total_hits"]))
    r.family(
        "xray_panel_ai_domains_last_update_timestamp_seconds",
        "gauge",
        "Unix time of the latest AI domain update (use time()-metric for report age).",
    )
    epoch = _iso_to_epoch(ai.get("updated_at"))
    if epoch is not None:
        r.sample("xray_panel_ai_domains_last_update_timestamp_seconds", int(epoch))

    return r.text()


@route("/metrics", methods=["GET"])
def metrics():
    # Token gate before any work. Empty token => endpoint disabled.
    if not METRICS_TOKEN:
        return Response("metrics disabled\n", status=404, mimetype="text/plain")
    header = request.headers.get("Authorization", "")
    expected = f"Bearer {METRICS_TOKEN}"
    if not hmac.compare_digest(header, expected):
        return Response(
            "unauthorized\n",
            status=401,
            mimetype="text/plain",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return Response(_collect(), content_type="text/plain; version=0.0.4; charset=utf-8")
