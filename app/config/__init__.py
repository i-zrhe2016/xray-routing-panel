"""Application configuration.

Values are read from the environment at import time and exposed as module-level
constants. The test suite relies on this: it sets env vars, drops ``app.*`` from
``sys.modules`` and re-imports, so the constants pick up the new environment.
Keep every value import-time evaluated — do not defer behind a function or lazy
object, or that reload-re-reads-env contract breaks.

Pure parsing/validation helpers live in :mod:`app.config.parsers` and are
re-exported here so existing ``from app.config import parse_bool_env`` style
imports keep working.
"""

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from .parsers import (
    parse_bool_env,
    parse_nonnegative_env_int,
    parse_optional_env_port,
    parse_positive_env_float,
    parse_positive_env_int,
    parse_shell_words_env,
    parse_time_of_day_env,
    parse_timezone_env,
)


def _parse_csv_env(name):
    """Read a small comma/newline-separated node inventory from the environment."""
    raw = os.environ.get(name, "")
    return tuple(item.strip() for item in raw.replace("\r", "\n").replace("\n", ",").split(",") if item.strip())


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "panel.db"))
UPLOADS_DIR = DATA_DIR / "uploads"
PAYMENT_PROOFS_DIR = UPLOADS_DIR / "payment-proofs"
XRAY_ENV_FILE_PATH = Path(os.environ.get("XRAY_ENV_FILE_PATH", BASE_DIR / "xray" / ".env"))
XRAY_CONFIG_PATH = Path(os.environ.get("XRAY_CONFIG_PATH", BASE_DIR / "xray" / "runtime" / "config.json"))
XRAY_DYNAMIC_ROUTING_PATH = Path(
    os.environ.get("XRAY_DYNAMIC_ROUTING_PATH", XRAY_CONFIG_PATH.parent / "dynamic-routing.json")
)
XRAY_PANEL_PORTS_PATH = Path(
    os.environ.get("XRAY_PANEL_PORTS_PATH", BASE_DIR / "xray" / "runtime" / "panel-ports.json")
)
XRAY_ACCESS_LOG_PATH = Path(os.environ.get("XRAY_ACCESS_LOG_PATH", BASE_DIR / "xray" / "logs" / "access.log"))
XRAY_API_SERVER = os.environ.get("XRAY_API_SERVER", "127.0.0.1:10085").strip() or "127.0.0.1:10085"
XRAY_STATS_QUERY_TIMEOUT = parse_nonnegative_env_int(
    os.environ.get("XRAY_STATS_QUERY_TIMEOUT", "5"),
    "XRAY_STATS_QUERY_TIMEOUT",
)
XRAY_CLIENT_CONFIG_PATH = Path(
    os.environ.get("XRAY_CLIENT_CONFIG_PATH", BASE_DIR / "xray" / "runtime" / "client-test.json")
)
SUBSCRIPTION_NAME_PREFIX = os.environ.get("SUBSCRIPTION_NAME_PREFIX", "reality").strip() or "reality"

DATAPLANE_SSH_TARGET = os.environ.get("DATAPLANE_SSH_TARGET", "").strip()
DATAPLANE_SSH_BIN = os.environ.get("DATAPLANE_SSH_BIN", "ssh").strip() or "ssh"
DATAPLANE_SSH_OPTIONS = parse_shell_words_env(
    os.environ.get("DATAPLANE_SSH_OPTIONS", ""),
    "DATAPLANE_SSH_OPTIONS",
)
DATAPLANE_SSH_KNOWN_HOSTS = os.environ.get(
    "DATAPLANE_SSH_KNOWN_HOSTS", "/root/.ssh/known_hosts"
).strip()
DATAPLANE_REMOTE_COMMAND_TIMEOUT = parse_positive_env_float(
    os.environ.get("DATAPLANE_REMOTE_COMMAND_TIMEOUT", "8"),
    "DATAPLANE_REMOTE_COMMAND_TIMEOUT",
)
DATAPLANE_API_SERVER = os.environ.get("DATAPLANE_API_SERVER", XRAY_API_SERVER).strip() or XRAY_API_SERVER
DATAPLANE_XRAY_BIN = os.environ.get("DATAPLANE_XRAY_BIN", "/usr/local/bin/xray").strip() or "/usr/local/bin/xray"
DATAPLANE_LOCAL_BIN = os.environ.get("DATAPLANE_LOCAL_BIN", "").strip()
DATAPLANE_CONTAINER_NAME = os.environ.get("DATAPLANE_CONTAINER_NAME", "xray-reality-local").strip()
DATAPLANE_DOCKER_BIN = os.environ.get("DATAPLANE_DOCKER_BIN", "docker").strip() or "docker"
DATAPLANE_RESTART_COMMAND = os.environ.get("DATAPLANE_RESTART_COMMAND", "").strip()
DATAPLANE_CONFIG_PATH = os.environ.get("DATAPLANE_CONFIG_PATH", "").strip()
DATAPLANE_DYNAMIC_ROUTING_PATH = os.environ.get("DATAPLANE_DYNAMIC_ROUTING_PATH", "").strip()
DATAPLANE_AI_REPORT_PATH = os.environ.get("DATAPLANE_AI_REPORT_PATH", "").strip()
DATAPLANE_PANEL_DB_PATH = os.environ.get("DATAPLANE_PANEL_DB_PATH", "").strip()
DATAPLANE_PANEL_PORTS_PATH = os.environ.get("DATAPLANE_PANEL_PORTS_PATH", "").strip()
DATAPLANE_ACCESS_LOG_PATH = os.environ.get("DATAPLANE_ACCESS_LOG_PATH", "").strip()
DATAPLANE_PROBE_HOST = os.environ.get("DATAPLANE_PROBE_HOST", "127.0.0.1").strip() or "127.0.0.1"

AI_NODE_SSH_TARGET = os.environ.get(
    "AI_NODE_SSH_TARGET", ""
).strip()
AI_NODE_SSH_TARGETS = _parse_csv_env("AI_NODE_SSH_TARGETS")
AI_NODE_IDS = _parse_csv_env("AI_NODE_IDS")
AI_NODE_LABELS = _parse_csv_env("AI_NODE_LABELS")
AI_NODE_CONTAINER_NAMES = _parse_csv_env("AI_NODE_CONTAINER_NAMES")
AI_NODE_RESTART_COMMANDS = _parse_csv_env("AI_NODE_RESTART_COMMANDS")
AI_NODE_CONFIG_PATHS = _parse_csv_env("AI_NODE_CONFIG_PATHS")
AI_NODE_API_SERVERS = _parse_csv_env("AI_NODE_API_SERVERS")
AI_NODE_PROBE_HOSTS = _parse_csv_env("AI_NODE_PROBE_HOSTS")
AI_NODE_LIST_CONFIGURED = any(
    os.environ.get(name, "").strip()
    for name in (
        "AI_NODE_SSH_TARGETS",
        "AI_NODE_IDS",
        "AI_NODE_LABELS",
        "AI_NODE_CONTAINER_NAMES",
        "AI_NODE_RESTART_COMMANDS",
        "AI_NODE_CONFIG_PATHS",
        "AI_NODE_API_SERVERS",
        "AI_NODE_PROBE_HOSTS",
    )
)
AI_NODE_SSH_BIN = os.environ.get("AI_NODE_SSH_BIN", "ssh").strip() or "ssh"
AI_NODE_SSH_OPTIONS = parse_shell_words_env(
    os.environ.get("AI_NODE_SSH_OPTIONS", ""),
    "AI_NODE_SSH_OPTIONS",
)
AI_NODE_SSH_KNOWN_HOSTS = os.environ.get(
    "AI_NODE_SSH_KNOWN_HOSTS", "/root/.ssh/known_hosts_ai"
).strip()
AI_NODE_XRAY_BIN = os.environ.get("AI_NODE_XRAY_BIN", "/usr/local/bin/xray").strip() or "/usr/local/bin/xray"
AI_NODE_CONTAINER_NAME = os.environ.get("AI_NODE_CONTAINER_NAME", "xray-ai-node").strip()
AI_NODE_DOCKER_BIN = os.environ.get("AI_NODE_DOCKER_BIN", "docker").strip() or "docker"
AI_NODE_RESTART_COMMAND = os.environ.get("AI_NODE_RESTART_COMMAND", "").strip()
AI_NODE_CONFIG_PATH = os.environ.get("AI_NODE_CONFIG_PATH", "/etc/xray/config.json").strip()
AI_NODE_API_SERVER = os.environ.get("AI_NODE_API_SERVER", "127.0.0.1:10085").strip()
AI_NODE_METRICS_URL = os.environ.get(
    "AI_NODE_METRICS_URL", "http://127.0.0.1:31097/debug/vars"
).strip()
AI_NODE_ACCESS_LOG_PATH = os.environ.get(
    "AI_NODE_ACCESS_LOG_PATH", str(BASE_DIR / "xray" / "logs" / "ai-access.log")
).strip()
AI_NODE_DESTINATION_WINDOW_SECONDS = parse_positive_env_int(
    os.environ.get("AI_NODE_DESTINATION_WINDOW_SECONDS", "600"),
    "AI_NODE_DESTINATION_WINDOW_SECONDS",
)
AI_NODE_DESTINATION_MAX_LABELS = parse_positive_env_int(
    os.environ.get("AI_NODE_DESTINATION_MAX_LABELS", "50"),
    "AI_NODE_DESTINATION_MAX_LABELS",
)
AI_NODE_PROBE_HOST = os.environ.get("AI_NODE_PROBE_HOST", "").strip()
AI_NODE_CONFIG_OUT = Path(
    os.environ.get("AI_NODE_CONFIG_OUT", XRAY_CONFIG_PATH.parent / "config-ai-node.json")
)
AI_DOMAIN_MANAGER_CONTAINER_NAME = os.environ.get(
    "AI_DOMAIN_MANAGER_CONTAINER_NAME", "xray-routing-panel-xray-ai-domain-manager-1"
).strip()
AI_DOMAIN_MANAGER_DOCKER_BIN = os.environ.get("AI_DOMAIN_MANAGER_DOCKER_BIN", "docker").strip() or "docker"
AI_DOMAIN_MANAGER_EXECUTION_MODE = os.environ.get(
    "AI_DOMAIN_MANAGER_EXECUTION_MODE", "docker"
).strip().lower() or "docker"

PANEL_HOST = os.environ.get("PANEL_HOST", "0.0.0.0")
PANEL_PORT = int(os.environ.get("PANEL_PORT", "18080"))
PANEL_INTERNAL_HOSTS = frozenset(
    item.strip().lower().strip("[]")
    for item in (
        _parse_csv_env("PANEL_INTERNAL_HOSTS")
        or ("100.112.13.103",)
    )
    if item.strip()
)
PANEL_PUBLIC_URL = os.environ.get("PANEL_PUBLIC_URL", "").strip().rstrip("/")
PANEL_SUBSCRIPTION_PUBLIC_URL = (
    os.environ.get("PANEL_SUBSCRIPTION_PUBLIC_URL", "").strip().rstrip("/")
    or PANEL_PUBLIC_URL
)
PANEL_USERNAME = os.environ.get("PANEL_USERNAME", "")
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "")
PANEL_SECRET_KEY = os.environ.get("PANEL_SECRET_KEY", "").strip()
PANEL_LOG_LEVEL = os.environ.get("PANEL_LOG_LEVEL", "INFO").strip().upper() or "INFO"
PANEL_SLOW_REQUEST_MS = parse_nonnegative_env_int(
    os.environ.get("PANEL_SLOW_REQUEST_MS", "1000"),
    "PANEL_SLOW_REQUEST_MS",
)
# Prometheus /metrics scrape token. Empty disables the endpoint (returns 404) so
# the internet-facing panel never exposes metrics unauthenticated.
METRICS_TOKEN = os.environ.get("METRICS_TOKEN", "").strip()
# TTL (seconds) for caching the data-plane running check — the one SSH call on the
# /metrics path — so frequent/concurrent scrapes never stack SSH round trips.
METRICS_DP_TTL = float(os.environ.get("METRICS_DP_TTL", "30"))
# Browser-reachable base URL of the Grafana behind the monitoring stack (e.g.
# "http://panel-host:3000"). When set, the admin "监控" tab embeds Grafana panels
# (d-solo iframes) from it; empty disables the tab's charts (shows a hint instead).
GRAFANA_PUBLIC_URL = os.environ.get("GRAFANA_PUBLIC_URL", "").strip().rstrip("/")
# UID of the embed-focused Grafana dashboard the 监控 tab pulls single panels from.
GRAFANA_OBSERVABILITY_UID = os.environ.get("GRAFANA_OBSERVABILITY_UID", "xray-observability").strip() or "xray-observability"

DEFAULT_UPSTREAM_HOST = os.environ.get("DEFAULT_UPSTREAM_HOST", "127.0.0.1")
DEFAULT_UPSTREAM_PORT = int(os.environ.get("DEFAULT_UPSTREAM_PORT", "443"))
SEED_LISTEN_PORT = os.environ.get("SEED_LISTEN_PORT", "31098").strip()
MAINTENANCE_INTERVAL = int(os.environ.get("MAINTENANCE_INTERVAL", "10"))
PROBE_ENABLED = os.environ.get("PROBE_ENABLED", "0").strip().lower() not in {"0", "false", "no", "off"}
PROBE_INTERVAL = int(os.environ.get("PROBE_INTERVAL", "60"))
PROBE_TIMEOUT = float(os.environ.get("PROBE_TIMEOUT", "3"))
PROBE_TEST_LISTEN_PORT = parse_optional_env_port(
    os.environ.get("PROBE_TEST_LISTEN_PORT", ""),
    "PROBE_TEST_LISTEN_PORT",
)
DNS_FAILOVER_ENABLED = parse_bool_env(os.environ.get("DNS_FAILOVER_ENABLED"), default=False)
DNS_FAILOVER_INTERVAL = parse_positive_env_int(
    os.environ.get("DNS_FAILOVER_INTERVAL", "15"),
    "DNS_FAILOVER_INTERVAL",
)
DNS_FAILOVER_TIMEOUT = parse_positive_env_float(
    os.environ.get("DNS_FAILOVER_TIMEOUT", "3"),
    "DNS_FAILOVER_TIMEOUT",
)
DNS_FAILOVER_FAILURE_THRESHOLD = parse_positive_env_int(
    os.environ.get("DNS_FAILOVER_FAILURE_THRESHOLD", "3"),
    "DNS_FAILOVER_FAILURE_THRESHOLD",
)
DNS_FAILOVER_RECOVERY_THRESHOLD = parse_positive_env_int(
    os.environ.get("DNS_FAILOVER_RECOVERY_THRESHOLD", "2"),
    "DNS_FAILOVER_RECOVERY_THRESHOLD",
)
DNS_FAILOVER_PROBE_HOST = os.environ.get("DNS_FAILOVER_PROBE_HOST", "").strip()
DNS_FAILOVER_PROBE_PORT = parse_optional_env_port(
    os.environ.get("DNS_FAILOVER_PROBE_PORT", ""),
    "DNS_FAILOVER_PROBE_PORT",
)
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "").strip()
CF_ZONE_ID = os.environ.get("CF_ZONE_ID", "").strip()
CF_DNS_RECORD_ID = os.environ.get("CF_DNS_RECORD_ID", "").strip()
CF_DNS_RECORD_TYPE = (os.environ.get("CF_DNS_RECORD_TYPE", "A").strip() or "A").upper()
CF_DNS_RECORD_NAME = os.environ.get("CF_DNS_RECORD_NAME", "").strip()
CF_DNS_RECORD_PROXIED = parse_bool_env(os.environ.get("CF_DNS_RECORD_PROXIED"), default=False)
CF_DNS_RECORD_TTL = parse_positive_env_int(
    os.environ.get("CF_DNS_RECORD_TTL", "60"),
    "CF_DNS_RECORD_TTL",
)
DNS_FAILOVER_PRIMARY_CONTENT = os.environ.get("DNS_FAILOVER_PRIMARY_CONTENT", "").strip()
DNS_FAILOVER_BACKUP_CONTENT = os.environ.get("DNS_FAILOVER_BACKUP_CONTENT", "").strip()
DNS_FAILOVER_BACKUP_LABEL = os.environ.get("DNS_FAILOVER_BACKUP_LABEL", "控制面备用节点").strip() or "控制面备用节点"
DNS_FAILOVER_PEAK_ENABLED = parse_bool_env(os.environ.get("DNS_FAILOVER_PEAK_ENABLED"), default=False)
DNS_FAILOVER_PEAK_START = parse_time_of_day_env(
    os.environ.get("DNS_FAILOVER_PEAK_START", ""),
    "DNS_FAILOVER_PEAK_START",
)
DNS_FAILOVER_PEAK_END = parse_time_of_day_env(
    os.environ.get("DNS_FAILOVER_PEAK_END", ""),
    "DNS_FAILOVER_PEAK_END",
)
DNS_FAILOVER_PEAK_TIMEZONE = parse_timezone_env(
    os.environ.get("DNS_FAILOVER_PEAK_TIMEZONE", ""),
    "DNS_FAILOVER_PEAK_TIMEZONE",
)
CONTROL_PLANE_BACKUP_XRAY_ENABLED = parse_bool_env(
    os.environ.get("CONTROL_PLANE_BACKUP_XRAY_ENABLED"),
    default=False,
)
# Full vless:// share URL for the upstream the control-plane backup Xray relays
# every client connection to (e.g. a NAT-forwarded exit at nat.qq.pw). When set,
# the backup node config replaces the direct "freedom" exit with a VLESS outbound
# to this upstream, so failover traffic is forwarded instead of leaving directly.
CONTROL_PLANE_BACKUP_UPSTREAM_URL = os.environ.get(
    "CONTROL_PLANE_BACKUP_UPSTREAM_URL", ""
).strip()
AI_ROUTING_ENABLED = parse_bool_env(os.environ.get("AI_ROUTING_ENABLED"), default=True)
PANEL_HEALTH_REQUIRES_XRAY = parse_bool_env(os.environ.get("PANEL_HEALTH_REQUIRES_XRAY"), default=True)
AUTH_ENABLED = bool(PANEL_USERNAME or PANEL_PASSWORD)
AUTH_SESSION_KEY = "panel_auth_marker"
AUTH_SESSION_MARKER = hashlib.sha256(f"{PANEL_USERNAME}\0{PANEL_PASSWORD}".encode("utf-8")).hexdigest()
TENANT_SESSION_TOKEN_KEY = "tenant_auth_token"
TENANT_SESSION_MARKER_KEY = "tenant_auth_marker"
PROBE_DASHBOARD_RANGES = {
    "1h": {"hours": 1, "label": "1小时"},
    "24h": {"hours": 24, "label": "24小时"},
    "7d": {"hours": 24 * 7, "label": "7天"},
}

COMMERCE_AUTO_PORT_START = parse_optional_env_port(
    os.environ.get("COMMERCE_AUTO_PORT_START", "31000"),
    "COMMERCE_AUTO_PORT_START",
)
COMMERCE_AUTO_PORT_END = parse_optional_env_port(
    os.environ.get("COMMERCE_AUTO_PORT_END", "39999"),
    "COMMERCE_AUTO_PORT_END",
)
if COMMERCE_AUTO_PORT_START is not None and COMMERCE_AUTO_PORT_END is not None:
    if COMMERCE_AUTO_PORT_START > COMMERCE_AUTO_PORT_END:
        raise ValueError("COMMERCE_AUTO_PORT_START 不能大于 COMMERCE_AUTO_PORT_END。")
COMMERCE_ORDER_EXPIRY_HOURS_DEFAULT = parse_positive_env_int(
    os.environ.get("COMMERCE_ORDER_EXPIRY_HOURS_DEFAULT", "24"),
    "COMMERCE_ORDER_EXPIRY_HOURS_DEFAULT",
)
PAYMENT_PROOF_MAX_BYTES = parse_positive_env_int(
    os.environ.get("PAYMENT_PROOF_MAX_BYTES", str(5 * 1024 * 1024)),
    "PAYMENT_PROOF_MAX_BYTES",
)

LOCAL_TZ = datetime.now().astimezone().tzinfo or timezone.utc
CUSTOMER_SESSION_ID_KEY = "customer_session_id"
CUSTOMER_SESSION_MARKER_KEY = "customer_session_marker"
CSRF_SESSION_KEY = "csrf_token"
