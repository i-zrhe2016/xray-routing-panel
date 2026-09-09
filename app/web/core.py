import atexit
import importlib
import logging
import secrets
import signal
import sys
import time
from datetime import datetime

from flask import Flask, Response, abort, current_app, g, jsonify, redirect, request, session, url_for
from flask import cli as flask_cli

from ..auth import (
    auth_required_response,
    clear_customer_session,
    clear_tenant_session,
    credentials_match,
    customer_auth_required_response,
    ensure_csrf_token,
    extract_basic_credentials,
    is_customer_session_authenticated,
    is_session_authenticated,
    is_tenant_session_authenticated,
    mark_session_authenticated,
    mark_tenant_session_authenticated,
    tenant_credentials_match,
    validate_csrf_token,
)
from ..config import (
    AUTH_ENABLED,
    AUTH_SESSION_KEY,
    CUSTOMER_SESSION_ID_KEY,
    DEFAULT_UPSTREAM_HOST,
    DEFAULT_UPSTREAM_PORT,
    GRAFANA_OBSERVABILITY_UID,
    GRAFANA_PUBLIC_URL,
    PANEL_HOST,
    PANEL_INTERNAL_HOSTS,
    PANEL_PORT,
    PANEL_PUBLIC_URL,
    PANEL_SECRET_KEY,
    PROBE_ENABLED,
    TENANT_SESSION_TOKEN_KEY,
    XRAY_CLIENT_CONFIG_PATH,
)
from ..helpers import format_optional_display_time, human_bytes
from ..observability.logging import (
    REQUEST_ID_HEADER,
    bind_actor,
    clear_request_context,
    emit_business_event,
    emit_event,
    emit_request_event,
    get_request_context,
    initialize_request_context,
    is_valid_request_id,
    new_request_id,
    set_request_endpoint,
    slow_request_threshold_ms,
)
from ..subscriptions import (
    build_clash_subscription_content,
    build_port_access_payload,
    build_v2ray_subscription_content,
    parse_xray_client_profile,
)

# Routes, before_request hooks and template filters are collected when the
# factory loads the view modules and then applied to each Flask app. Every
# handler stays a plain module-level function, and — crucially — endpoint names
# stay bare (the function name), so url_for(...) in templates and the
# endpoint-name sets in the before_request guards keep working unchanged.
_ROUTES = []
_BEFORE_REQUEST = []
_TEMPLATE_FILTERS = []

_VIEW_MODULES = (
    "admin_api",
    "admin_views",
    "client_errors",
    "customer_api",
    "customer_views",
    "health",
    "metrics",
    "portal_views",
    "subscription_views",
    "tenant_views",
)


def route(rule, **options):
    def decorator(view_func):
        _ROUTES.append((rule, options, view_func))
        return view_func

    return decorator


def before_request(view_func):
    _BEFORE_REQUEST.append(view_func)
    return view_func


def template_filter(name):
    def decorator(filter_func):
        _TEMPLATE_FILTERS.append((name, filter_func))
        return filter_func

    return decorator


# The compatibility reference is stable across factory calls. View modules can
# keep their existing ``state.method(...)`` calls while requests resolve the
# application from the Flask app that is currently serving them. This keeps the
# migration seam narrow until the view call sites move to application services.
app = None


class _ApplicationReference:
    def __init__(self):
        self._fallback = None

    def bind(self, application):
        self._fallback = application

    def _resolve(self):
        try:
            return current_app.extensions["application"]
        except RuntimeError:
            if self._fallback is not None:
                return self._fallback
            raise RuntimeError("No application has been bound to the Web factory") from None

    def __getattr__(self, name):
        return getattr(self._resolve(), name)


state = _ApplicationReference()


def _load_view_modules(application):
    state.bind(application)
    for module_name in _VIEW_MODULES:
        importlib.import_module(f"{__package__}.{module_name}")

    # A view module may have been imported by a helper test before the factory
    # was called. Rebind its legacy module-level name so every handler consumes
    # the application supplied to this factory call.
    for module_name in _VIEW_MODULES:
        module = sys.modules.get(f"{__package__}.{module_name}")
        if module is not None and "state" in vars(module):
            module.state = state


def create_app(application):
    """Create the Flask consumer for a caller-provided application object."""

    if application is None:
        raise TypeError("create_app(application) requires an application object")

    _load_view_modules(application)
    # import_name "app" so Flask resolves root_path to the app/ package dir,
    # making template_folder/static_folder point at app/templates and app/static
    # exactly as the former single-file app.web module did.
    flask_app = Flask(
        "app",
        template_folder="templates",
        static_folder="static",
    )
    flask_app.config.update(
        SECRET_KEY=PANEL_SECRET_KEY or secrets.token_hex(32),
        SESSION_COOKIE_NAME="xray-routing-panel-session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=PANEL_PUBLIC_URL.startswith("https://"),
    )
    flask_app.extensions["application"] = application
    werkzeug_logger = logging.getLogger("werkzeug")
    werkzeug_logger.disabled = True
    werkzeug_logger.propagate = False
    for rule, options, view_func in _ROUTES:
        endpoint = options.get("endpoint", view_func.__name__)
        extra = {key: value for key, value in options.items() if key != "endpoint"}
        flask_app.add_url_rule(rule, endpoint, view_func, **extra)
    for view_func in _BEFORE_REQUEST:
        flask_app.before_request(view_func)
    flask_app.after_request(observability_after_request)
    flask_app.teardown_request(observability_teardown_request)
    for name, filter_func in _TEMPLATE_FILTERS:
        flask_app.add_template_filter(filter_func, name)
    global app
    app = flask_app
    package = sys.modules.get(__package__)
    if package is not None:
        package.app = flask_app
        package.state = state
    return flask_app


_BUSINESS_EVENT_BY_ENDPOINT = {
    "login": "auth.admin.login",
    "customer_login": "auth.customer.login",
    "customer_register": "auth.customer.register",
    "tenant_login": "auth.tenant.login",
    "api_customer_login": "auth.customer.login",
    "api_customer_register": "auth.customer.register",
    "api_tenant_login": "auth.tenant.login",
    "api_create_port": "port.created",
    "create_port": "port.created",
    "api_update_port": "port.updated",
    "update_port": "port.updated",
    "api_toggle_port": "port.toggled",
    "toggle_port": "port.toggled",
    "api_delete_port": "port.deleted",
    "delete_port": "port.deleted",
    "api_reset_port_traffic": "port.traffic_reset",
    "reset_port_traffic": "port.traffic_reset",
    "api_fulfill_order": "order.fulfilled",
    "api_reject_order": "order.rejected",
    "api_cancel_order": "order.cancelled",
    "api_rotate_subscription": "subscription.rotated",
    "rotate_subscription": "subscription.rotated",
    "api_customer_subscription_renew": "subscription.renewed",
    "customer_subscription_renew": "subscription.renewed",
    "api_customer_submit_payment_proof": "order.payment_proof_submitted",
    "customer_submit_order_payment_proof": "order.payment_proof_submitted",
    "api_dns_failover_check": "dns_failover.checked",
    "api_dns_failover_switch": "dns_failover.switched",
    "api_ai_routing_switch": "ai_routing.manual_switched",
    "api_restart_data_plane": "node.data_plane.restarted",
    "api_diagnose_data_plane": "node.data_plane.diagnosed",
    "api_restart_ai_node": "node.ai.restarted",
}


@before_request
def initialize_observability():
    supplied_request_id = request.headers.get(REQUEST_ID_HEADER, "").strip()
    request_id = supplied_request_id if is_valid_request_id(supplied_request_id) else new_request_id()
    endpoint = request.url_rule.rule if request.url_rule is not None else (request.endpoint or "unmatched")
    initialize_request_context(request_id, method=request.method, endpoint=endpoint)
    g.panel_request_started_at = time.monotonic()
    g.panel_request_id = request_id
    return None


def is_internal_panel_request():
    """Return whether the request used a configured private panel host.

    The client source address is not suitable here because a remote Tailscale
    client has its own address. The destination Host header identifies the
    private control-plane entry point; public requests use the Cloudflare
    hostname and do not match this set.
    """
    host = request.host.rsplit(":", 1)[0].strip("[]").lower()
    return host in PANEL_INTERNAL_HOSTS


@before_request
def ensure_basic_auth():
    if request.path in {"/healthz", "/metrics"}:
        return None
    if is_internal_panel_request():
        if AUTH_ENABLED:
            mark_session_authenticated()
        return None
    if not AUTH_ENABLED:
        return None
    access_email = request.environ.get("HTTP_CF_ACCESS_AUTHENTICATED_USER_EMAIL", "").strip()
    if access_email:
        if not is_session_authenticated():
            mark_session_authenticated()
        return None
    if request.endpoint in {
        "login",
        "logout",
        "static",
        "landing_page",
        "robots_txt",
        "sitemap_xml",
        "plans_page",
        "customer_login",
        "customer_register",
        "customer_logout",
        "customer_dashboard",
        "customer_orders",
        "customer_order_detail",
        "customer_subscriptions",
        "customer_subscription_detail",
        "customer_subscription_renew",
        "customer_submit_order_payment_proof",
        "payment_proof_file",
        "tenant_login",
        "tenant_logout",
        "subscription_default",
        "subscription_clash",
        "subscription_v2ray",
        "tenant_panel",
        "tenant_subscription_default",
        "tenant_subscription_clash",
        "tenant_subscription_v2ray",
        # Subscriber portal shell + JSON API (self-gate via the customer session
        # / are public). Allowlisted so admin Basic auth does not intercept them.
        "portal_shell",
        "portal_shell_path",
        "api_customer_me",
        "api_customer_overview",
        "api_customer_subscriptions",
        "api_customer_subscription_detail",
        "api_customer_subscription_renew",
        "api_customer_orders",
        "api_customer_order_detail",
        "api_customer_submit_payment_proof",
        "api_customer_plans",
        "api_customer_login",
        "api_customer_register",
        "api_customer_logout",
        # Tokenized tenant deep-link: public shell + JSON API gated by tenant session.
        "api_tenant_subscription",
        "api_tenant_login",
        "api_client_errors",
    }:
        return None
    if session.get(AUTH_SESSION_KEY) and not is_session_authenticated():
        session.clear()
    if is_session_authenticated():
        return None

    basic_credentials = extract_basic_credentials()
    if basic_credentials and credentials_match(*basic_credentials):
        mark_session_authenticated()
        return None
    return auth_required_response()


@before_request
def ensure_tenant_panel_auth():
    # The /tenant/<token> page is now a public SPA shell; tenant access is gated
    # at the JSON API (/api/tenant/<token>/subscription via _tenant_authed), which
    # preserves the same admin-session-bypass / per-port-credential semantics. This
    # before_request hook is kept as a no-op to avoid disturbing hook ordering.
    return None


@before_request
def ensure_customer_portal_auth():
    customer_endpoints = {
        "customer_dashboard",
        "customer_orders",
        "customer_order_detail",
        "customer_subscriptions",
        "customer_subscription_detail",
        "customer_subscription_renew",
        "customer_submit_order_payment_proof",
    }
    if request.endpoint not in customer_endpoints:
        return None

    customer = get_authenticated_customer()
    if customer is not None:
        return None
    return customer_auth_required_response()


@before_request
def bind_observability_actor():
    if is_session_authenticated():
        bind_actor("admin")
        return None
    customer = get_authenticated_customer()
    if customer is not None:
        bind_actor("customer", customer.get("id"))
        return None
    tenant = get_authenticated_tenant()
    if tenant is not None:
        bind_actor("tenant", tenant.get("id"))
    return None


def observability_after_request(response):
    """Add the request ID and emit only actionable request access logs."""

    try:
        endpoint = request.url_rule.rule if request.url_rule is not None else (request.endpoint or "unmatched")
        set_request_endpoint(endpoint, request.method)
        context = get_request_context()
        request_id = str(context.get("request_id") or getattr(g, "panel_request_id", "") or new_request_id())
        response.headers[REQUEST_ID_HEADER] = request_id
        started_at = getattr(g, "panel_request_started_at", time.monotonic())
        duration_ms = (time.monotonic() - started_at) * 1000
        status_code = int(response.status_code)
        if status_code >= 400 and not context.get("business_event_emitted"):
            failed_event = _BUSINESS_EVENT_BY_ENDPOINT.get(request.endpoint or "")
            if failed_event:
                emit_business_event(
                    failed_event,
                    result="failure",
                    status_code=status_code,
                    error_code="http_rejected",
                )
        is_slow = duration_ms >= slow_request_threshold_ms()
        should_log = request.method.upper() not in {"GET", "HEAD", "OPTIONS"} or is_slow or status_code >= 400
        if should_log:
            if status_code >= 500:
                result = "failure"
            elif status_code >= 400:
                result = "rejected"
            elif is_slow:
                result = "slow"
            else:
                result = "success"
            emit_request_event(
                status_code=status_code,
                duration_ms=duration_ms,
                result=result,
                message="slow request" if is_slow and status_code < 400 else "",
            )
    except Exception:
        pass
    return response


def observability_teardown_request(exc):
    try:
        if exc is not None:
            emit_event(
                "http.unhandled_exception",
                category="request",
                result="failure",
                level="ERROR",
                status_code=500,
                exc=exc,
            )
    finally:
        clear_request_context()


def log_business_event(event, **kwargs):
    emit_business_event(event, **kwargs)


@template_filter("human_bytes")
def human_bytes_filter(value):
    return human_bytes(value)


def build_subscription_snapshot(ports):
    subscription_profile, subscription_error = parse_xray_client_profile()
    subscription = {
        "available": subscription_profile is not None,
        "error": subscription_error,
        "client_config_path": str(XRAY_CLIENT_CONFIG_PATH),
        "server": subscription_profile["server"] if subscription_profile else "",
        "mode": "per-port",
        "tenant_count": len(ports),
        "tenant_panel_path_example": "/login?next=/tenant/<tenant_token>",
        "tenant_subscription_path_example": "/tenant-subscriptions/<subscription_token>/clash",
    }
    for port in ports:
        port["access"] = build_port_access_payload(port, subscription_profile)
    return subscription


def collect_dashboard_state(message="", level="info", ai_sync_error=""):
    ports = state.query_ports()
    summary = state.query_summary(ports)
    subscription = build_subscription_snapshot(ports)
    data_plane_status = state.data_plane_status()
    ai_nodes_status = state.ai_nodes_status()
    ai_node_status = state.ai_node_status(ai_nodes_status)
    ai_routing_status = state.ai_routing_status(sync_error=ai_sync_error)
    dns_failover_status = state.dns_failover_status()
    commerce_summary = state.query_commerce_overview()
    commerce_settings = state.get_commerce_settings()
    commerce_plans = state.query_plans(public_only=False)
    commerce_orders = state.query_admin_orders()
    nodes = [
        {
            "key": "data_plane",
            "label": data_plane_status.get("label") or "数据面",
            "configured": bool(data_plane_status.get("configured")),
            "reachable": bool(data_plane_status.get("reachable")),
            "xray_running": data_plane_status.get("xray_running"),
            "management_target": data_plane_status.get("management_target") or "",
            "supports_restart": bool(data_plane_status.get("supports_restart")),
        },
        {
            "key": "ai_node",
            "label": ai_node_status.get("label") or "AI 节点",
            "configured": bool(ai_node_status.get("configured")),
            "reachable": bool(ai_node_status.get("reachable")),
            "xray_running": ai_node_status.get("xray_running"),
            "management_target": ai_node_status.get("management_target") or "",
            "supports_restart": bool(ai_node_status.get("supports_restart")),
        },
    ]
    dns_failover_running = bool(dns_failover_status.get("enabled") and dns_failover_status.get("configured"))
    nodes.append({
        "key": "control_plane_backup",
        "label": dns_failover_status.get("backup_label") or "控制面备用",
        "configured": dns_failover_running,
        "reachable": dns_failover_running,
        "xray_running": dns_failover_running,
        "management_target": "控制面本机" if dns_failover_running else "",
        "supports_restart": False,
    })
    traffic_routing = build_traffic_routing(data_plane_status, ai_node_status, ai_routing_status, dns_failover_status)
    backup_xray_mode = state.backup_xray_mode() if hasattr(state, "backup_xray_mode") else "disabled"
    return {
        "flash": {
            "message": message,
            "level": level,
        },
        "meta": {
            "panel_address": PANEL_PUBLIC_URL or f"{PANEL_HOST}:{PANEL_PORT}",
            "data_plane_running": bool(data_plane_status.get("xray_running")),
            "ai_node_running": bool(ai_node_status.get("reachable")),
            "timezone_label": datetime.now().astimezone().strftime("%Z"),
            "probe_enabled": PROBE_ENABLED,
            "probe_dashboard_url": url_for("probe_dashboard") if PROBE_ENABLED else "",
            "ai_domain_dashboard_url": url_for("ai_domain_dashboard"),
            "plans_page_url": url_for("plans_page"),
            "customer_login_url": url_for("customer_login"),
            "csrf_token": ensure_csrf_token(),
            "default_upstream_host": DEFAULT_UPSTREAM_HOST,
            "default_upstream_port": DEFAULT_UPSTREAM_PORT,
            "tenant_panel_prefix": "/tenant/",
            "data_plane_status": data_plane_status,
            "ai_node_status": ai_node_status,
            "ai_nodes": ai_nodes_status,
            "ai_routing_status": ai_routing_status,
            "dns_failover_status": dns_failover_status,
            "nodes": nodes,
            "traffic_routing": traffic_routing,
            "backup_xray_mode": backup_xray_mode,
            "ai_domain_stats": state.query_ai_domain_overview(sync_error=ai_sync_error),
            "grafana_url": GRAFANA_PUBLIC_URL,
            "grafana_observability_uid": GRAFANA_OBSERVABILITY_UID,
        },
        "summary": summary,
        "subscription": subscription,
        "ports": ports,
        "commerce": {
            "summary": commerce_summary,
            "settings": commerce_settings,
            "plans": commerce_plans,
            "orders": commerce_orders,
        },
    }


def build_traffic_routing(data_plane_status, ai_node_status, ai_routing_status, dns_failover_status):
    """Describe the current traffic-routing path for the dashboard flow diagram."""
    dp_ok = bool(data_plane_status.get("reachable"))
    ai_candidates = ai_routing_status.get("ai_candidates")
    if not isinstance(ai_candidates, list):
        ai_candidates = []
    selected_ai_candidate = next(
        (
            candidate
            for candidate in ai_candidates
            if isinstance(candidate, dict) and candidate.get("selected") is True
        ),
        None,
    )
    selected_ai_has_probe = selected_ai_candidate is not None and selected_ai_candidate.get("is_reachable") in {
        True,
        False,
    }
    # The AI routing report is the source of truth for the traffic path. The
    # SSH-managed node status only describes the control channel and may be
    # unavailable while the selected REALITY candidate is serving traffic.
    ai_ok = (
        bool(selected_ai_candidate.get("is_reachable"))
        if selected_ai_has_probe
        else bool(ai_node_status.get("reachable"))
    )
    ai_label = str(
        (selected_ai_candidate or {}).get("label")
        or (selected_ai_candidate or {}).get("candidate_label")
        or "AI 节点"
    ).strip() or "AI 节点"
    ai_route_status = str(
        ai_routing_status.get("route_status") or ai_routing_status.get("status") or ""
    ).strip()
    ai_fallback = ai_route_status in {
        "fallback_to_primary",
        "manual_fallback",
        "manual_target_unreachable",
        "probe_error",
    }
    dns_target = str(dns_failover_status.get("current_target") or "primary").strip()
    backup_enabled = bool(dns_failover_status.get("enabled") and dns_failover_status.get("configured"))
    backup_xray_enabled = bool(dns_failover_status.get("control_plane_backup_xray_enabled"))
    backup_mode = "relay" if backup_xray_enabled and ai_ok else "direct"

    def route(path, label, scenario, entry, transit, exit_node, status, degraded=False, waiting=False):
        return {
            "path": path,
            "label": label,
            "scenario": scenario,
            "route_status": status,
            "is_degraded": degraded,
            "waiting_for_switch": waiting,
            "entry_node": entry,
            "transit_nodes": transit,
            "exit_node": exit_node,
        }

    if dns_target == "backup" and backup_enabled:
        if backup_mode == "relay" and ai_ok:
            return route(
                "dns_backup_relay_ai",
                f"DNS→控制面备用→{ai_label}",
                f"数据面故障，控制面备用 relay 到{ai_label}",
                "控制面备用",
                [ai_label],
                f"{ai_label} freedom 直出",
                "备用 relay",
                True,
            )
        return route("dns_backup_direct", "DNS→控制面备用→freedom 直出", "双节点故障，控制面备用 freedom 直出", "控制面备用", [], "控制面备用 freedom 直出", "备用直出", True)

    if not dp_ok and backup_enabled:
        return route("dns_backup_pending", "DNS 待切换到控制面备用", "数据面故障，等待 DNS 切换", "当前 DNS 入口", [], "等待切换", "待切换", True, True)

    if dp_ok and ai_ok and not ai_fallback:
        return route(
            "normal_ai",
            f"数据面→{ai_label}直出",
            f"正常：AI 流量经{ai_label}直出",
            "普通数据面",
            [ai_label],
            f"{ai_label} freedom 直出",
            "正常",
        )

    if dp_ok and (not ai_ok or ai_fallback):
        reason = (
            "AI 路由被人工强制回退，AI 流量改走数据面直出"
            if ai_route_status == "manual_fallback"
            else f"{ai_label}不可达，AI 流量回退到数据面直出"
        )
        status = "人工回退" if ai_route_status == "manual_fallback" else "AI 回退"
        return route("normal_fallback", "数据面→freedom 直出", reason, "普通数据面", [], "普通数据面 freedom 直出", status, True)

    if dp_ok:
        return route("normal_direct", "数据面→freedom 直出", "正常：流量经数据面直出", "普通数据面", [], "普通数据面 freedom 直出", "正常")

    return route("unknown", "状态未知", "节点状态待确认", "入口未确认", [], "出口未确认", "未知", True)


def build_tenant_dashboard_state(tenant_token, message="", level="info"):
    port = state.get_port_by_tenant_token(tenant_token)
    if port is None:
        return None

    subscription_profile, subscription_error = parse_xray_client_profile()
    access = build_port_access_payload(port, subscription_profile)
    return {
        "flash": {
            "message": message,
            "level": level,
        },
        "meta": {
            "panel_address": PANEL_PUBLIC_URL or f"{PANEL_HOST}:{PANEL_PORT}",
            "timezone_label": datetime.now().astimezone().strftime("%Z"),
            "probe_enabled": PROBE_ENABLED,
            "probe_dashboard_url": url_for("probe_dashboard") if PROBE_ENABLED else "",
            "tenant_login_url": tenant_login_target(tenant_token),
            "tenant_logout_url": url_for("tenant_logout", tenant_token=tenant_token),
            "subscription_available": subscription_profile is not None,
            "subscription_error": subscription_error,
            "client_config_path": str(XRAY_CLIENT_CONFIG_PATH),
        },
        "port": port,
        "access": access,
    }


def build_customer_service_access(service, subscription_profile):
    if not service or not service.get("port_id"):
        return None
    port_like = {
        "tenant_token": service.get("tenant_token"),
        "subscription_token": service.get("subscription_token"),
        "tenant_username": service.get("tenant_username"),
        "tenant_password": service.get("tenant_password"),
        "listen_port": service.get("listen_port"),
        "note": service.get("note"),
    }
    return build_port_access_payload(port_like, subscription_profile)


def build_customer_dashboard_state(customer, message="", level="info"):
    services = state.query_customer_service_subscriptions(customer["id"])
    orders = state.query_customer_orders(customer["id"])
    subscription_profile, subscription_error = parse_xray_client_profile()
    for service in services:
        service["access"] = build_customer_service_access(service, subscription_profile)
    open_order_count = len([item for item in orders if item["status"] in {"pending_payment", "payment_submitted", "payment_rejected"}])
    renewable_count = len([item for item in services if item["renewal_allowed"]])
    return {
        "flash": {"message": message, "level": level},
        "meta": {
            "timezone_label": datetime.now().astimezone().strftime("%Z"),
            "plans_page_url": url_for("plans_page"),
            "orders_url": url_for("customer_orders"),
            "subscriptions_url": url_for("customer_subscriptions"),
            "logout_url": url_for("customer_logout"),
            "subscription_available": subscription_profile is not None,
            "subscription_error": subscription_error,
            "csrf_token": ensure_csrf_token(),
        },
        "customer": {
            "id": customer["id"],
            "email": customer["email"],
            "last_login_at_display": format_optional_display_time(
                customer.get("last_login_at"), default="首次登录"
            ),
        },
        "summary": {
            "service_count": len(services),
            "renewable_count": renewable_count,
            "open_order_count": open_order_count,
        },
        "services": services[:5],
        "orders": orders[:10],
        "commerce_settings": state.get_commerce_settings(),
    }


def build_dashboard_state(message="", level="info"):
    state.sync_traffic_state()
    state.disable_auto_stopped_ports(reload_xray=True)
    ai_sync_error = ""
    try:
        state.sync_data_plane_ai_state()
    except RuntimeError as exc:
        ai_sync_error = str(exc)
    return collect_dashboard_state(message=message, level=level, ai_sync_error=ai_sync_error)


def json_success_response(message="", level="success", status_code=200):
    return (
        jsonify(
            {
                "ok": True,
                "message": message,
                "level": level,
                "dashboard": build_dashboard_state(message=message, level=level),
            }
        ),
        status_code,
    )


def json_snapshot_success_response(message="", level="success", status_code=200):
    """Return the current dashboard without running maintenance a second time."""
    return (
        jsonify(
            {
                "ok": True,
                "message": message,
                "level": level,
                "dashboard": collect_dashboard_state(message=message, level=level),
            }
        ),
        status_code,
    )


def json_error_response(message, status_code=400):
    return jsonify({"ok": False, "message": message}), status_code


def json_customer_success(data=None, message="", level="success", status_code=200):
    # Subscriber-portal success envelope. Unlike json_success_response it does NOT
    # rebuild the admin dashboard; it returns just the affected resource so the
    # portal SPA updates in place.
    return (
        jsonify({"ok": True, "message": message, "level": level, "data": data if data is not None else {}}),
        status_code,
    )


def json_customer_auth_required():
    return (
        jsonify(
            {
                "ok": False,
                "code": "auth_required",
                "message": "请先登录。",
                "login_url": url_for("customer_login"),
            }
        ),
        401,
    )


def json_tenant_auth_required():
    # Token-mode subscriber view: the SPA shows an inline per-port login card on
    # 401 rather than redirecting, so this stays JSON.
    return (
        jsonify({"ok": False, "code": "auth_required", "message": "请输入该端口的租户用户名和密码。"}),
        401,
    )


def json_validate_csrf():
    # Returns a JSON 400 tuple when the CSRF token is missing/invalid, else None,
    # so JSON endpoints stay JSON instead of aborting to an HTML error page.
    if is_internal_panel_request():
        return None
    token = request.headers.get("X-CSRF-Token", "") or request.form.get("csrf_token", "")
    if not validate_csrf_token(token):
        return json_error_response("CSRF token 无效。", 400)
    return None


def request_payload():
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        return payload
    return {}


def tenant_panel_target(tenant_token):
    return url_for("tenant_panel", tenant_token=tenant_token)


def tenant_login_target(tenant_token, **values):
    query = {"next": tenant_panel_target(tenant_token)}
    query.update(values)
    return url_for("login", **query)


def customer_dashboard_target():
    return url_for("customer_dashboard")


def customer_login_target(next_target="", **values):
    query = {"next": next_target or customer_dashboard_target()}
    query.update(values)
    return url_for("customer_login", **query)


def get_authenticated_tenant():
    tenant_token = str(session.get(TENANT_SESSION_TOKEN_KEY) or "").strip()
    if not tenant_token:
        return None

    port = state.get_port_by_tenant_token(tenant_token)
    if port is None or not is_tenant_session_authenticated(port):
        clear_tenant_session()
        return None
    return port


def get_authenticated_customer():
    customer_id = session.get(CUSTOMER_SESSION_ID_KEY)
    if not customer_id:
        return None
    customer = state.get_customer_by_id(customer_id)
    if customer is None or customer.get("status") != "active" or not is_customer_session_authenticated(customer):
        clear_customer_session()
        return None
    return customer


def require_csrf():
    if is_internal_panel_request():
        return None
    token = request.headers.get("X-CSRF-Token", "")
    if not token:
        token = request.form.get("csrf_token", "")
    if not validate_csrf_token(token):
        abort(400, description="CSRF token 无效。")


def build_subscription_response(token, listen_port, output_format):
    expected_token = state.get_subscription_token()
    if token != expected_token:
        abort(404)

    profile, _ = parse_xray_client_profile()
    if profile is None:
        abort(404)

    port = state.get_port_subscription_record(listen_port)
    if port is None:
        abort(404)
    if profile.get("unified_port") and str(listen_port) not in profile["user_uuids"]:
        abort(404)

    if output_format == "v2ray":
        content = build_v2ray_subscription_content(profile, listen_port, port["note"])
        content_type = "text/plain; charset=utf-8"
    else:
        content = build_clash_subscription_content(profile, listen_port, port["note"])
        content_type = "text/yaml; charset=utf-8"

    return Response(
        content,
        content_type=content_type,
        headers={
            "Cache-Control": "no-store, no-cache, max-age=0, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


def build_port_token_subscription_response(subscription_token, output_format):
    profile, _ = parse_xray_client_profile()
    if profile is None:
        abort(404)

    port = state.get_port_subscription_record_by_token(subscription_token)
    if port is None or port.get("status") != "active":
        abort(404)
    if profile.get("unified_port") and str(port["listen_port"]) not in profile["user_uuids"]:
        abort(404)

    if output_format == "v2ray":
        content = build_v2ray_subscription_content(profile, port["listen_port"], port["note"])
        content_type = "text/plain; charset=utf-8"
    else:
        content = build_clash_subscription_content(profile, port["listen_port"], port["note"])
        content_type = "text/yaml; charset=utf-8"

    return Response(
        content,
        content_type=content_type,
        headers={
            "Cache-Control": "no-store, no-cache, max-age=0, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


def message_redirect(message, level):
    return redirect(url_for("index", message=message, level=level), code=303)


def handle_shutdown(signum, _frame):
    raise KeyboardInterrupt(f"received signal {signum}")


def main():
    state.lifecycle.start()
    atexit.register(state.lifecycle.stop)
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    show_server_banner = flask_cli.show_server_banner
    flask_cli.show_server_banner = lambda *args, **kwargs: None
    try:
        app.run(host=PANEL_HOST, port=PANEL_PORT, threaded=True, use_reloader=False)
    finally:
        flask_cli.show_server_banner = show_server_banner
        state.lifecycle.stop()
