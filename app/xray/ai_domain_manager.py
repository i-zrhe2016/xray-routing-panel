"""Backward-compatible import facade for the split AI routing package.

The implementation lives under :mod:`app.xray.ai_routing`.  The facade keeps
older integrations importable while the canonical CLI is
``app.xray.ai_routing.runner``.
"""

import subprocess
import time

from app.xray.ai_routing import artifact as _artifact
from app.xray.ai_routing import classifier as _classifier
from app.xray.ai_routing import manager as _manager
from app.xray.ai_routing import repository as _repository
from app.xray.ai_routing import selector as _selector
from app.xray.ai_routing.artifact import (
    apply_default_proxy_sockopt,
    build_default_proxy_payload,
    build_domain_report,
    build_proxy_sockopt_payload,
    extract_probe_server_name,
    render_proxy_template,
    rerender_config,
    resolve_probe_server_name,
    restart_xray_command,
    restart_xray_container,
    write_domain_report,
    write_routing_fragment,
)
from app.xray.ai_routing.candidates import (
    build_ai_upstream_candidates,
    build_template_upstream_candidate,
    dedupe_upstream_candidates,
    join_host_port,
    parse_upstream_endpoint,
    parse_upstream_list,
    parse_vless_fallback_url,
    probe_ai_upstream_candidate,
    summarize_ai_target_candidate,
)
from app.xray.ai_routing.classifier import (
    FORCED_AI_ROUTE_DOMAIN_SUFFIXES,
    KNOWN_AI_DOMAIN_SUFFIXES,
    build_codex_command,
    build_openai_classification_payload,
    classify_domains_via_codex,
    classify_domains_via_openai,
    classify_pending_domains,
    extract_chat_completions_text,
    extract_openai_response_text,
    extract_output_text,
    infer_openai_api_style,
    is_local_openai_base_url,
    load_decisions,
    matches_domain_suffixes,
    matches_forced_ai_route_domain,
    matches_known_ai_domain,
    normalize_classification,
    normalize_openai_base_url,
    resolve_openai_endpoint,
    sync_builtin_domain_decisions,
    sync_codex_home,
    validate_classification_results,
)
from app.xray.ai_routing.common import (
    DOMAIN_RE,
    env_bool,
    env_int,
    format_timestamp,
    load_env_file_values,
    load_json,
    parse_positive_float,
    read_env_or_file,
    save_json,
    utc_now,
)
from app.xray.ai_routing.manager import LOCK_BUSY_EXIT_CODE, build_data_plane_controller
from app.xray.ai_routing.observations import (
    load_log_state,
    normalize_log_state,
    parse_log_line,
    purge_old_events,
    save_log_state,
    split_target_host,
    sync_log,
)
from app.xray.ai_routing.repository import (
    connect_panel_db,
    ensure_ai_domain_schema,
    read_ai_routing_manual_mode,
    read_panel_target,
    run_with_sqlite_lock_retry,
    save_ai_domains_to_panel_db,
)
from app.xray.ai_routing.runner import build_args, main, seconds_until_next_boundary
from app.xray.ai_routing.selector import (
    select_ai_target,
    should_fallback_to_primary_route,
    summarize_ai_target_for_report,
)
from app.xray.operation_lock import LockBusyError, exclusive_file_lock


def classify_pending_domains(decisions, decisions_path, observed_domains, args):
    _classifier.classify_domains_via_openai = globals()["classify_domains_via_openai"]
    _classifier.classify_domains_via_codex = globals()["classify_domains_via_codex"]
    return _classifier.classify_pending_domains(decisions, decisions_path, observed_domains, args)


def select_ai_target(candidates, timeout_seconds, probe_controller=None, preferred_index=None):
    _selector.probe_ai_upstream_candidate = globals()["probe_ai_upstream_candidate"]
    return _selector.select_ai_target(
        candidates,
        timeout_seconds,
        probe_controller=probe_controller,
        preferred_index=preferred_index,
    )


def save_ai_domains_to_panel_db(panel_db_path, report, decisions):
    _repository._save_ai_domains_to_panel_db_once = globals()["_save_ai_domains_to_panel_db_once"]
    return _repository.save_ai_domains_to_panel_db(panel_db_path, report, decisions)


_save_ai_domains_to_panel_db_once = _repository._save_ai_domains_to_panel_db_once


def run_once(args):
    for name in (
        "build_data_plane_controller",
        "sync_log",
        "sync_builtin_domain_decisions",
        "read_panel_target",
        "read_ai_routing_manual_mode",
        "select_ai_target",
        "classify_pending_domains",
        "should_fallback_to_primary_route",
        "render_proxy_template",
        "write_routing_fragment",
        "rerender_config",
        "restart_xray_command",
        "restart_xray_container",
        "build_domain_report",
        "save_ai_domains_to_panel_db",
        "write_domain_report",
        "save_log_state",
        "save_json",
    ):
        setattr(_manager, name, globals()[name])
    _selector.probe_ai_upstream_candidate = globals()["probe_ai_upstream_candidate"]
    return _manager.run_once(args)


__all__ = [name for name in globals() if not name.startswith("_")]


if __name__ == "__main__":
    raise SystemExit(main())
