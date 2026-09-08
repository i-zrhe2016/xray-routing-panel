"""Orchestrate one AI routing analysis and apply cycle."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from app.xray.node_control import DataPlaneConfig, DataPlaneController
from app.xray.operation_lock import LockBusyError, exclusive_file_lock

from .artifact import (
    build_domain_report,
    render_proxy_template,
    rerender_config,
    resolve_probe_server_name,
    restart_xray_command,
    restart_xray_container,
    write_domain_report,
    write_routing_fragment,
)
from .classifier import (
    classify_pending_domains,
    load_decisions,
    sync_builtin_domain_decisions,
)
from .common import save_json, utc_now
from .observations import load_log_state, purge_old_events, save_log_state, sync_log
from .repository import (
    read_ai_routing_manual_mode,
    read_panel_target,
    save_ai_domains_to_panel_db,
)
from .selector import select_ai_target, should_fallback_to_primary_route

LOCK_BUSY_EXIT_CODE = 75


def build_data_plane_controller(args):
    upstream_host = ""
    upstream_port = None
    candidates = getattr(args, "ai_upstream_candidates", None)
    first_candidate = candidates[0] if isinstance(candidates, (list, tuple)) and candidates else {}
    if isinstance(first_candidate, dict):
        upstream_host = str(first_candidate.get("upstream_host", "")).strip()
        try:
            upstream_port = int(first_candidate.get("upstream_port"))
        except (TypeError, ValueError):
            upstream_port = None
    access_log_path = str(getattr(args, "data_plane_access_log_path", "") or "").strip()
    if not access_log_path and args.data_plane_ssh_target and args.data_plane_config_path:
        config_path = Path(args.data_plane_config_path)
        if config_path.name == "config.json" and config_path.parent.name == "runtime":
            access_log_path = str(config_path.parent.parent / "logs" / "access.log")
    data_plane_config_path = str(getattr(args, "data_plane_config_path", "") or "").strip()
    panel_ports_path = ""
    if data_plane_config_path:
        panel_ports_path = str(Path(data_plane_config_path).with_name("panel-ports.json"))
    source_panel_ports_path = Path(args.config_out).with_name("panel-ports.json")
    return DataPlaneController(
        DataPlaneConfig(
            role="data_plane",
            label="数据面",
            api_server=args.data_plane_api_server,
            xray_bin=args.data_plane_xray_bin,
            local_bin=args.data_plane_local_bin,
            docker_bin=args.data_plane_docker_bin,
            container_name=args.data_plane_container_name,
            restart_command=args.data_plane_restart_command,
            ssh_target=args.data_plane_ssh_target,
            ssh_bin=args.data_plane_ssh_bin,
            ssh_options=tuple(args.data_plane_ssh_options),
            ssh_known_hosts_file=str(getattr(args, "data_plane_ssh_known_hosts_file", "") or "").strip(),
            remote_command_timeout=float(getattr(args, "data_plane_remote_command_timeout", 8.0) or 8.0),
            config_path=args.data_plane_config_path,
            dynamic_routing_path=args.data_plane_dynamic_routing_path,
            access_log_path=access_log_path,
            panel_ports_path=panel_ports_path,
            source_config_path=args.config_out,
            source_dynamic_routing_path=args.dynamic_routing_path,
            source_panel_ports_path=source_panel_ports_path,
            upstream_host=upstream_host,
            upstream_port=upstream_port,
        )
    )


def _resolve_lock_path(args, attribute, default_name):
    configured = getattr(args, attribute, None)
    if isinstance(configured, (str, os.PathLike)) and str(configured).strip():
        return Path(configured)
    return Path(args.config_out).with_name(default_name)


def run_once(args):
    """Run one apply cycle while excluding every other manager process."""

    lock_path = _resolve_lock_path(args, "apply_lock_path", ".ai-domain-manager.lock")
    manual_mode_override = getattr(args, "manual_mode", None)
    has_manual_override = isinstance(manual_mode_override, str) and bool(manual_mode_override.strip())
    manual_lock_held = getattr(args, "manual_lock_held", False)
    if not isinstance(manual_lock_held, bool):
        manual_lock_held = False
    if has_manual_override:
        if manual_lock_held:
            with exclusive_file_lock(lock_path):
                return _run_once_locked(args)
        manual_lock_path = _resolve_lock_path(args, "manual_lock_path", ".ai-domain-manager-manual.lock")
        with exclusive_file_lock(manual_lock_path), exclusive_file_lock(lock_path):
            return _run_once_locked(args)

    manual_lock_path = _resolve_lock_path(args, "manual_lock_path", ".ai-domain-manager-manual.lock")
    with exclusive_file_lock(manual_lock_path), exclusive_file_lock(lock_path):
        return _run_once_locked(args)


def _run_once_locked(args):
    now = utc_now()
    data_plane_controller = build_data_plane_controller(args)
    log_state = load_log_state(args.log_state_path)
    sync_log(
        args.log_path,
        log_state,
        data_plane_controller=data_plane_controller,
        lookback_seconds=args.lookback_seconds,
        now=now,
    )
    cutoff = purge_old_events(log_state, args.lookback_seconds, now)

    decisions = load_decisions(args.classification_state_path)
    observed_domains = {item["domain"] for item in log_state["events"]}
    sync_builtin_domain_decisions(decisions, args.classification_state_path, observed_domains)
    panel_target = read_panel_target(args.panel_db_path, args.panel_route_listen_port)
    manual_mode_override = getattr(args, "manual_mode", None)
    if not isinstance(manual_mode_override, str):
        manual_mode_override = ""
    manual_mode = manual_mode_override.strip().lower() or read_ai_routing_manual_mode(args.panel_db_path)
    if manual_mode == "forced_fallback":
        ai_target = {
            "probe_status": "manual_fallback",
            "failure_reason": "manual_override",
            "is_reachable": False,
            "candidates": [],
        }
    else:
        for candidate in args.ai_upstream_candidates:
            candidate["probe_server_name"] = resolve_probe_server_name(
                args.proxy_template_path,
                candidate,
                panel_target,
                getattr(args, "ai_upstream_probe_server_name", ""),
            )
        preferred_index = {"primary": 0, "backup": 1}.get(manual_mode)
        ai_target = select_ai_target(
            args.ai_upstream_candidates,
            args.ai_upstream_probe_timeout_seconds,
            probe_controller=data_plane_controller,
            preferred_index=preferred_index,
        )
    route_status = {"status": "disabled", "reason": ""}

    if manual_mode == "forced_fallback":
        pending_without_classifier = []
    else:
        pending_without_classifier = classify_pending_domains(
            decisions,
            args.classification_state_path,
            observed_domains,
            args,
        )

    ai_domains = sorted(domain for domain, item in decisions["domains"].items() if item.get("classification") == "ai")

    proxy_payload = None
    if ai_domains:
        if manual_mode == "forced_fallback":
            args.dynamic_routing_path.unlink(missing_ok=True)
            route_status = {"status": "manual_fallback", "reason": "manual_override"}
        elif (
            should_fallback_to_primary_route(ai_target)
            or str(ai_target.get("probe_status", "")).strip().lower() == "manual_unreachable"
        ):
            args.dynamic_routing_path.unlink(missing_ok=True)
            route_status = {
                "status": (
                    "manual_target_unreachable" if manual_mode in {"primary", "backup"} else "fallback_to_primary"
                ),
                "reason": (
                    "manual_ai_target_unreachable"
                    if manual_mode in {"primary", "backup"}
                    else "ai_upstream_unreachable"
                ),
            }
        elif str(ai_target.get("probe_status", "")).strip().lower() == "probe_error":
            args.dynamic_routing_path.unlink(missing_ok=True)
            route_status = {
                "status": "probe_error",
                "reason": ai_target.get("failure_reason", "ai_probe_management_failed"),
            }
        else:
            proxy_payload, proxy_error = render_proxy_template(args.proxy_template_path, ai_target, panel_target)
            if proxy_payload is None:
                args.dynamic_routing_path.unlink(missing_ok=True)
                route_status = {"status": "pending_proxy_template", "reason": proxy_error}
            else:
                applied = write_routing_fragment(args.dynamic_routing_path, ai_domains, proxy_payload)
                route_status = {
                    "status": "applied" if applied else "disabled",
                    "reason": proxy_error if applied else "no_ai_domains",
                }
    elif manual_mode == "forced_fallback":
        args.dynamic_routing_path.unlink(missing_ok=True)
        route_status = {"status": "manual_fallback", "reason": "manual_override"}
    else:
        args.dynamic_routing_path.unlink(missing_ok=True)
        route_status = {"status": "idle", "reason": "no_ai_domains"}

    previous_config = args.config_out.read_text(encoding="utf-8") if args.config_out.is_file() else ""
    pending_apply_path = args.config_out.with_name(args.config_out.name + ".pending-apply")
    pending_apply = pending_apply_path.exists()
    pending_apply_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        rerender_config(
            args.render_script,
            args.env_file,
            args.config_out,
            args.client_out,
            args.share_out,
            args.dynamic_routing_path,
        )
    except Exception:
        current_config = args.config_out.read_text(encoding="utf-8") if args.config_out.is_file() else ""
        if not pending_apply and current_config != previous_config:
            pending_apply_path.touch()
        raise
    current_config = args.config_out.read_text(encoding="utf-8") if args.config_out.is_file() else ""
    config_changed = current_config != previous_config
    config_retried = pending_apply
    if config_changed or pending_apply:
        pending_apply_path.touch()
    remote_config_changed = False
    config_apply_status = "not_needed"
    apply_needed = config_changed or pending_apply
    external_reloader_enabled = getattr(args, "data_plane_external_reloader_enabled", False)
    if not isinstance(external_reloader_enabled, bool):
        external_reloader_enabled = False
    managed_data_plane = data_plane_controller.is_configured() and (
        getattr(data_plane_controller, "mode", "") != "unmanaged" or external_reloader_enabled
    )
    if managed_data_plane:
        synced_paths = []
        if data_plane_controller.supports_sync():
            synced_paths = data_plane_controller.sync_generated_files(validate_config=True)
            remote_config_path = str(getattr(args, "data_plane_config_path", "") or "").strip()
            remote_config_changed = bool(
                remote_config_path and remote_config_path in {str(path) for path in synced_paths}
            )
        apply_needed = config_changed or remote_config_changed or pending_apply
        if apply_needed:
            pending_apply_path.touch()
        if apply_needed and external_reloader_enabled:
            config_apply_status = "delegated"
        elif apply_needed and (not data_plane_controller.supports_restart() or not data_plane_controller.restart()):
            raise RuntimeError("AI 路由配置重载失败，保留待应用状态以便重试。")
        elif apply_needed:
            config_apply_status = "direct"
        else:
            config_apply_status = "unchanged"
        if config_apply_status != "delegated":
            pending_apply_path.unlink(missing_ok=True)
    elif (config_changed or pending_apply) and args.restart_command and not external_reloader_enabled:
        restart_xray_command(args.restart_command, args.docker_timeout_seconds)
        config_apply_status = "direct"
        pending_apply_path.unlink(missing_ok=True)
    elif (config_changed or pending_apply) and args.restart_container_name and not external_reloader_enabled:
        restart_xray_container(args.restart_container_name, args.docker_timeout_seconds)
        config_apply_status = "direct"
        pending_apply_path.unlink(missing_ok=True)
    elif apply_needed:
        config_apply_status = "delegated" if external_reloader_enabled else "unmanaged"
        if config_apply_status == "unmanaged" and manual_mode_override.strip():
            raise RuntimeError("AI 路由配置重载未配置，保留待应用状态以便重试。")
        if config_apply_status != "delegated":
            pending_apply_path.unlink(missing_ok=True)

    try:
        report = build_domain_report(log_state, cutoff, now, decisions, ai_target, panel_target, route_status)
        if pending_without_classifier:
            report["route_status"]["pending_domains_without_classifier"] = pending_without_classifier
        report["route_status"]["config_changed"] = config_changed or remote_config_changed
        report["route_status"]["config_retried"] = config_retried
        report["route_status"]["config_apply_status"] = config_apply_status
        report["panel_db_status"] = save_ai_domains_to_panel_db(args.panel_db_path, report, decisions)
        write_domain_report(args.report_output_dir, report)
        save_log_state(args.log_state_path, log_state)
        save_json(args.classification_state_path, decisions)
        print(
            "[ai_domain_manager] "
            f"domains={report['unique_domains']} ai_domains={len(report['ai_domains'])} "
            f"route_status={report['route_status']['status']}",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001 - reporting must not undo a successful config apply
        print(
            "[ai_domain_manager] config applied but report persistence failed: " f"{exc}",
            file=sys.stderr,
            flush=True,
        )
        return {
            "status": "applied_with_reporting_error",
            "config_apply_status": config_apply_status,
            "config_changed": config_changed or remote_config_changed,
            "config_retried": config_retried,
        }
    return {
        "status": "applied",
        "config_apply_status": config_apply_status,
        "config_changed": config_changed or remote_config_changed,
        "config_retried": config_retried,
    }


__all__ = [
    "LOCK_BUSY_EXIT_CODE",
    "LockBusyError",
    "build_data_plane_controller",
    "run_once",
]
