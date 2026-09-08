"""CLI and scheduler entry point for the AI routing manager."""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import time
from pathlib import Path

from app.xray.config import BASE_DIR, DEFAULT_RENDER_MODULE
from app.xray.operation_lock import LockBusyError

from .candidates import build_ai_upstream_candidates
from .classifier import is_local_openai_base_url, normalize_openai_base_url
from .common import (
    env_bool,
    env_int,
    load_env_file_values,
    parse_positive_float,
    read_env_or_file,
)
from .manager import LOCK_BUSY_EXIT_CODE, run_once


def seconds_until_next_boundary(interval_seconds):
    now = time.time()
    next_boundary = ((int(now) // interval_seconds) + 1) * interval_seconds
    return max(1, next_boundary - now)


def build_args():
    parser = argparse.ArgumentParser(description="Classify Xray destination domains and maintain dynamic AI routing.")
    parser.add_argument("--workspace-dir", default=os.environ.get("XRAY_WORKSPACE_DIR", str(BASE_DIR)))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=env_int("AI_DOMAIN_INTERVAL_SECONDS", 3600))
    parser.add_argument("--lookback-seconds", type=int, default=env_int("AI_DOMAIN_LOOKBACK_SECONDS", 3600))
    parser.add_argument("--batch-size", type=int, default=env_int("AI_DOMAIN_BATCH_SIZE", 50))
    parser.add_argument("--openai-timeout-seconds", type=int, default=env_int("OPENAI_TIMEOUT_SECONDS", 45))
    parser.add_argument("--codex-timeout-seconds", type=int, default=env_int("CODEX_TIMEOUT_SECONDS", 180))
    parser.add_argument("--docker-timeout-seconds", type=int, default=env_int("DOCKER_TIMEOUT_SECONDS", 30))
    parser.add_argument("--panel-route-listen-port", type=int, default=env_int("PANEL_ROUTE_LISTEN_PORT", 0))
    parser.add_argument(
        "--manual-mode",
        choices=("auto", "primary", "backup", "forced_fallback"),
        default="",
        help="Apply this mode for a one-shot request without reading the panel state.",
    )
    parser.add_argument("--manual-lock-held", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    workspace = Path(args.workspace_dir)
    args.log_path = Path(os.environ.get("XRAY_ACCESS_LOG_PATH", str(workspace / "logs" / "access.log")))
    args.report_output_dir = Path(
        os.environ.get("AI_DOMAIN_REPORT_OUTPUT_DIR", str(workspace / "reports" / "hourly-domains"))
    )
    args.log_state_path = Path(os.environ.get("AI_DOMAIN_LOG_STATE_PATH", str(args.report_output_dir / ".state.json")))
    args.classification_state_path = Path(
        os.environ.get("AI_DOMAIN_CLASSIFICATION_STATE_PATH", str(workspace / "runtime" / "ai-domain-decisions.json"))
    )
    args.dynamic_routing_path = Path(
        os.environ.get("AI_DOMAIN_DYNAMIC_ROUTING_PATH", str(workspace / "runtime" / "dynamic-routing.json"))
    )
    args.env_file = Path(os.environ.get("XRAY_ENV_FILE", str(workspace / ".env")))
    args.render_script = (
        os.environ.get("XRAY_RENDER_MODULE", "").strip()
        or os.environ.get("XRAY_RENDER_SCRIPT", "").strip()
        or DEFAULT_RENDER_MODULE
    )
    args.config_out = Path(os.environ.get("XRAY_CONFIG_OUT", str(workspace / "runtime" / "config.json")))
    args.apply_lock_path = Path(
        os.environ.get("AI_DOMAIN_MANAGER_LOCK_PATH", str(args.config_out.with_name(".ai-domain-manager.lock")))
    )
    args.manual_lock_path = Path(
        os.environ.get(
            "AI_DOMAIN_MANAGER_MANUAL_LOCK_PATH", str(args.config_out.with_name(".ai-domain-manager-manual.lock"))
        )
    )
    args.client_out = Path(os.environ.get("XRAY_CLIENT_OUT", str(workspace / "runtime" / "client-test.json")))
    args.share_out = Path(os.environ.get("XRAY_SHARE_OUT", str(workspace / "runtime" / "client-share.txt")))
    args.panel_db_path = Path(os.environ.get("PANEL_DB_PATH", "/panel-data/panel.db"))
    args.proxy_template_path = Path(
        os.environ.get("AI_PROXY_OUTBOUND_TEMPLATE_PATH", str(workspace / "ai-proxy-outbound.json"))
    )
    env_file_values = load_env_file_values(args.env_file)
    args.restart_container_name = os.environ.get("DATAPLANE_RESTART_CONTAINER", "").strip()
    args.restart_command = os.environ.get("DATAPLANE_RESTART_COMMAND", "").strip()
    args.data_plane_ssh_target = os.environ.get("DATAPLANE_SSH_TARGET", "").strip()
    args.data_plane_ssh_bin = os.environ.get("DATAPLANE_SSH_BIN", "ssh").strip() or "ssh"
    args.data_plane_ssh_options = (
        tuple(shlex.split(os.environ.get("DATAPLANE_SSH_OPTIONS", "").strip()))
        if os.environ.get("DATAPLANE_SSH_OPTIONS", "").strip()
        else ()
    )
    args.data_plane_ssh_known_hosts_file = os.environ.get("DATAPLANE_SSH_KNOWN_HOSTS", "/root/.ssh/known_hosts").strip()
    args.data_plane_api_server = os.environ.get("DATAPLANE_API_SERVER", "127.0.0.1:10085").strip() or "127.0.0.1:10085"
    args.data_plane_xray_bin = (
        os.environ.get("DATAPLANE_XRAY_BIN", "/usr/local/bin/xray").strip() or "/usr/local/bin/xray"
    )
    args.data_plane_local_bin = os.environ.get("DATAPLANE_LOCAL_BIN", "").strip()
    args.data_plane_container_name = os.environ.get("DATAPLANE_CONTAINER_NAME", "").strip()
    args.data_plane_docker_bin = os.environ.get("DATAPLANE_DOCKER_BIN", "docker").strip() or "docker"
    args.data_plane_restart_command = os.environ.get("DATAPLANE_RESTART_COMMAND", "").strip()
    args.data_plane_config_path = os.environ.get("DATAPLANE_CONFIG_PATH", "").strip()
    args.data_plane_dynamic_routing_path = os.environ.get("DATAPLANE_DYNAMIC_ROUTING_PATH", "").strip()
    args.data_plane_access_log_path = os.environ.get("DATAPLANE_ACCESS_LOG_PATH", "").strip()
    args.data_plane_external_reloader_enabled = env_bool("DATAPLANE_EXTERNAL_RELOADER_ENABLED", "0")
    args.codex_classifier_enabled = env_bool("CODEX_CLASSIFIER_ENABLED", "1")
    args.codex_source_home = Path(os.environ.get("CODEX_SOURCE_HOME", "/host-codex-home"))
    args.codex_runtime_home = Path(os.environ.get("CODEX_RUNTIME_HOME", str(workspace / "runtime" / "codex-home")))
    args.codex_workdir = Path(os.environ.get("CODEX_WORKDIR", "/tmp/codex-domain-classifier"))
    args.codex_bin = os.environ.get("CODEX_BIN", "codex").strip() or "codex"
    args.codex_model = os.environ.get("CODEX_MODEL", "").strip()
    args.openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    args.openai_model = os.environ.get("OPENAI_MODEL", "gpt-5.5").strip() or "gpt-5.5"
    args.openai_base_url = normalize_openai_base_url(
        os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1/responses")
    )
    args.openai_allow_no_key = env_bool(
        "OPENAI_ALLOW_NO_KEY", "1" if is_local_openai_base_url(args.openai_base_url) else "0"
    )
    args.openai_classifier_enabled = bool(args.openai_api_key) or args.openai_allow_no_key
    args.ai_upstream_host = read_env_or_file("AI_UPSTREAM_HOST", "upstream.example.com", env_file_values)
    args.ai_upstream_port = int(read_env_or_file("AI_UPSTREAM_PORT", "27166", env_file_values))
    args.ai_upstreams = read_env_or_file("AI_UPSTREAMS", "", env_file_values)
    args.ai_upstream_fallbacks = read_env_or_file("AI_UPSTREAM_FALLBACKS", "", env_file_values)
    args.ai_upstream_fallback_url = read_env_or_file("AI_UPSTREAM_FALLBACK_URL", "", env_file_values)
    args.data_plane_remote_command_timeout = parse_positive_float(
        os.environ.get("DATAPLANE_REMOTE_COMMAND_TIMEOUT", "8"), "DATAPLANE_REMOTE_COMMAND_TIMEOUT"
    )
    args.ai_upstream_probe_timeout_seconds = parse_positive_float(
        read_env_or_file("AI_UPSTREAM_PROBE_TIMEOUT_SECONDS", "3", env_file_values),
        "AI_UPSTREAM_PROBE_TIMEOUT_SECONDS",
    )
    args.ai_upstream_probe_server_name = read_env_or_file("AI_UPSTREAM_PROBE_SERVER_NAME", "", env_file_values)
    args.ai_upstream_candidates = build_ai_upstream_candidates(
        args.ai_upstream_host,
        args.ai_upstream_port,
        upstreams_raw=args.ai_upstreams,
        fallbacks_raw=args.ai_upstream_fallbacks,
        fallback_share_url=args.ai_upstream_fallback_url,
    )
    return args


def main():
    args = build_args()

    if args.interval_seconds <= 0:
        print("AI_DOMAIN_INTERVAL_SECONDS must be > 0", file=sys.stderr)
        return 1
    if args.lookback_seconds <= 0:
        print("AI_DOMAIN_LOOKBACK_SECONDS must be > 0", file=sys.stderr)
        return 1
    if args.batch_size <= 0:
        print("AI_DOMAIN_BATCH_SIZE must be > 0", file=sys.stderr)
        return 1
    if not args.ai_upstream_candidates:
        print("at least one AI upstream must be configured", file=sys.stderr)
        return 1

    while True:
        try:
            run_once(args)
        except LockBusyError as exc:
            print(f"[ai_domain_manager] busy: {exc}", file=sys.stderr, flush=True)
            if args.once:
                return LOCK_BUSY_EXIT_CODE
        except Exception as exc:  # noqa: BLE001 - scheduler reports one-cycle failures and continues
            print(f"[ai_domain_manager] error: {exc}", file=sys.stderr, flush=True)
            if args.once:
                return 1
        else:
            if args.once:
                return 0
        time.sleep(seconds_until_next_boundary(args.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
