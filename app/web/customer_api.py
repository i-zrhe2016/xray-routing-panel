"""JSON API for the subscriber portal SPA.

The customer-session endpoints self-gate (returning a JSON 401 instead of an
HTML redirect), mutating endpoints validate CSRF via the X-CSRF-Token header,
and responses use the { ok, message, data } envelope. Package plans are
read-only here; customer mutations are limited to subscription renewal and
payment-proof submission for an existing order. Endpoint names are allowlisted
in core.ensure_basic_auth so admin Basic auth does not intercept them.
"""

import sqlite3

from flask import request

from ..auth import (
    clear_customer_session,
    customer_credentials_match,
    is_session_authenticated,
    is_tenant_session_authenticated,
    mark_customer_session_authenticated,
    mark_tenant_session_authenticated,
    tenant_credentials_match,
)
from ..config import AUTH_ENABLED
from ..errors import ValidationError
from ..helpers import format_optional_display_time
from ..subscriptions import parse_xray_client_profile
from .core import (
    build_customer_dashboard_state,
    build_customer_service_access,
    bind_actor,
    get_authenticated_customer,
    json_customer_auth_required,
    json_customer_success,
    json_error_response,
    json_tenant_auth_required,
    log_business_event,
    json_validate_csrf,
    request_payload,
    route,
    state,
)


def _serialize_customer(customer):
    return {
        "id": customer["id"],
        "email": customer["email"],
        "last_login_at_display": format_optional_display_time(
            customer.get("last_login_at"), default="首次登录"
        ),
    }


def _refresh_service_state():
    # Same freshening the server-rendered customer pages do before reading.
    state.sync_traffic_state()
    state.disable_auto_stopped_ports(reload_xray=True)


# --- session / identity ----------------------------------------------------


@route("/api/customer/me", methods=["GET"])
def api_customer_me():
    customer = get_authenticated_customer()
    if customer is None:
        return json_customer_auth_required()
    return json_customer_success(
        {"customer": _serialize_customer(customer), "commerce_settings": state.get_commerce_settings()}
    )


@route("/api/customer/auth/login", methods=["POST"])
def api_customer_login():
    csrf_error = json_validate_csrf()
    if csrf_error is not None:
        return csrf_error
    payload = request_payload()
    email = str(payload.get("email", "") or "")
    password = str(payload.get("password", "") or "")
    try:
        matched = state.get_customer_by_email(email)
    except ValidationError:
        matched = None
    if matched is not None and matched.get("status") == "active" and customer_credentials_match(matched, password):
        mark_customer_session_authenticated(matched)
        state.touch_customer_login(matched["id"])
        bind_actor("customer", matched["id"])
        log_business_event("auth.customer.login", actor_type="customer", actor_id=matched["id"])
        return json_customer_success({"customer": _serialize_customer(matched)}, message="登录成功。")
    log_business_event("auth.customer.login", result="failure", actor_type="customer", error_code="invalid_credentials")
    return json_error_response("邮箱或密码错误。", 401)


@route("/api/customer/auth/register", methods=["POST"])
def api_customer_register():
    csrf_error = json_validate_csrf()
    if csrf_error is not None:
        return csrf_error
    payload = request_payload()
    email = str(payload.get("email", "") or "")
    password = str(payload.get("password", "") or "")
    confirm_password = str(payload.get("confirm_password", password) or "")
    try:
        state.validate_customer_password(password, confirm_password)
        state.create_customer(email, password)
        created = state.get_customer_by_email(email)
        if created is None:
            raise ValidationError("客户账号创建失败。")
        mark_customer_session_authenticated(created)
        state.touch_customer_login(created["id"])
        bind_actor("customer", created["id"])
        log_business_event("auth.customer.register", actor_type="customer", actor_id=created["id"])
        return json_customer_success({"customer": _serialize_customer(created)}, message="注册成功。")
    except sqlite3.IntegrityError:
        log_business_event("auth.customer.register", result="failure", actor_type="customer", error_code="conflict")
        return json_error_response("该邮箱已注册，请直接登录。", 409)
    except ValidationError as exc:
        log_business_event("auth.customer.register", result="failure", actor_type="customer", error_code="validation", message=str(exc))
        return json_error_response(str(exc), 400)


@route("/api/customer/auth/logout", methods=["POST"])
def api_customer_logout():
    log_business_event("auth.customer.logout", actor_type="customer")
    clear_customer_session()
    return json_customer_success(message="已退出登录。")


# --- overview / subscriptions ---------------------------------------------


@route("/api/customer/overview", methods=["GET"])
def api_customer_overview():
    customer = get_authenticated_customer()
    if customer is None:
        return json_customer_auth_required()
    _refresh_service_state()
    return json_customer_success(build_customer_dashboard_state(customer))


@route("/api/customer/subscriptions", methods=["GET"])
def api_customer_subscriptions():
    customer = get_authenticated_customer()
    if customer is None:
        return json_customer_auth_required()
    _refresh_service_state()
    services = state.query_customer_service_subscriptions(customer["id"])
    profile, _ = parse_xray_client_profile()
    for service in services:
        service["access"] = build_customer_service_access(service, profile)
    return json_customer_success({"subscriptions": services})


@route("/api/customer/subscriptions/<int:service_subscription_id>", methods=["GET"])
def api_customer_subscription_detail(service_subscription_id):
    customer = get_authenticated_customer()
    if customer is None:
        return json_customer_auth_required()
    _refresh_service_state()
    service = state.get_customer_service_subscription(customer["id"], service_subscription_id)
    if service is None:
        return json_error_response("服务实例不存在。", 404)
    profile, profile_error = parse_xray_client_profile()
    service["access"] = build_customer_service_access(service, profile)
    return json_customer_success(
        {"subscription": service, "subscription_available": profile is not None, "subscription_error": profile_error}
    )


@route("/api/customer/subscriptions/<int:service_subscription_id>/renew", methods=["POST"])
def api_customer_subscription_renew(service_subscription_id):
    customer = get_authenticated_customer()
    if customer is None:
        return json_customer_auth_required()
    csrf_error = json_validate_csrf()
    if csrf_error is not None:
        return csrf_error
    service = state.get_customer_service_subscription(customer["id"], service_subscription_id)
    if service is None:
        return json_error_response("服务实例不存在。", 404)
    try:
        order_no = state.create_order(
            customer["id"], service["plan_id"], kind="renewal", service_subscription_id=service_subscription_id
        )
        log_business_event("subscription.renewed", actor_type="customer", actor_id=customer["id"], resource_type="subscription", resource_id=service_subscription_id, metadata={"order_no": order_no})
        return json_customer_success({"order_no": order_no}, message="续费订单已创建。")
    except ValidationError as exc:
        log_business_event("subscription.renewed", result="failure", actor_type="customer", actor_id=customer["id"], resource_type="subscription", resource_id=service_subscription_id, error_code="rejected", message=str(exc))
        return json_error_response(str(exc), 400)


# --- orders ----------------------------------------------------------------


@route("/api/customer/orders", methods=["GET"])
def api_customer_orders():
    customer = get_authenticated_customer()
    if customer is None:
        return json_customer_auth_required()
    return json_customer_success(
        {"orders": state.query_customer_orders(customer["id"]), "commerce_settings": state.get_commerce_settings()}
    )


@route("/api/customer/orders/<order_no>", methods=["GET"])
def api_customer_order_detail(order_no):
    customer = get_authenticated_customer()
    if customer is None:
        return json_customer_auth_required()
    order = state.get_customer_order(customer["id"], order_no)
    if order is None:
        return json_error_response("订单不存在。", 404)
    service = None
    if order.get("service_subscription_id"):
        service = state.get_customer_service_subscription(customer["id"], order["service_subscription_id"])
        if service is not None:
            profile, _ = parse_xray_client_profile()
            service["access"] = build_customer_service_access(service, profile)
    return json_customer_success(
        {"order": order, "service": service, "commerce_settings": state.get_commerce_settings()}
    )


@route("/api/customer/orders/<order_no>/payment-proof", methods=["POST"])
def api_customer_submit_payment_proof(order_no):
    customer = get_authenticated_customer()
    if customer is None:
        return json_customer_auth_required()
    csrf_error = json_validate_csrf()
    if csrf_error is not None:
        return csrf_error
    file_storage = request.files.get("proof_image")
    if file_storage is None:
        log_business_event(
            "order.payment_proof_submitted",
            result="failure",
            actor_type="customer",
            actor_id=customer["id"],
            resource_type="order",
            resource_id=order_no,
            error_code="missing_proof",
        )
        return json_error_response("请先选择支付截图。", 400)
    try:
        state.submit_order_payment_submission(
            customer["id"], order_no, file_storage, request.form.get("payer_note", "")
        )
        log_business_event("order.payment_proof_submitted", actor_type="customer", actor_id=customer["id"], resource_type="order", resource_id=order_no)
        return json_customer_success(message="支付凭证已提交，等待人工审核。")
    except ValidationError as exc:
        log_business_event("order.payment_proof_submitted", result="failure", actor_type="customer", actor_id=customer["id"], resource_type="order", resource_id=order_no, error_code="rejected", message=str(exc))
        return json_error_response(str(exc), 400)


# --- plans (public) --------------------------------------------------------


@route("/api/customer/plans", methods=["GET"])
def api_customer_plans():
    return json_customer_success({"plans": state.query_plans(public_only=True)})


# --- tenant token deep-link (account-less single subscription) --------------


def _tenant_authed(port):
    # Same rule the old tenant page used: an admin session sees any port; otherwise
    # a valid tenant session for THIS port is required.
    if AUTH_ENABLED and is_session_authenticated():
        return True
    return is_tenant_session_authenticated(port)


@route("/api/tenant/<tenant_token>/subscription", methods=["GET"])
def api_tenant_subscription(tenant_token):
    port = state.get_port_by_tenant_token(tenant_token)
    if port is None:
        return json_error_response("订阅不存在。", 404)
    if not _tenant_authed(port):
        return json_tenant_auth_required()
    _refresh_service_state()
    port = state.get_port_by_tenant_token(tenant_token) or port
    profile, profile_error = parse_xray_client_profile()
    sub = dict(port)
    sub["access"] = build_customer_service_access({**port, "port_id": port.get("id")}, profile)
    sub.setdefault("status_label", sub.get("status"))
    sub["plan_name"] = sub.get("note") or ""
    sub["renewal_allowed"] = False  # token mode is read-only (no account)
    return json_customer_success(
        {"subscription": sub, "subscription_available": profile is not None, "subscription_error": profile_error}
    )


@route("/api/tenant/<tenant_token>/login", methods=["POST"])
def api_tenant_login(tenant_token):
    port = state.get_port_by_tenant_token(tenant_token)
    if port is None:
        return json_error_response("订阅不存在。", 404)
    csrf_error = json_validate_csrf()
    if csrf_error is not None:
        return csrf_error
    payload = request_payload()
    username = str(payload.get("username", "") or "")
    password = str(payload.get("password", "") or "")
    if tenant_credentials_match(port, username, password):
        mark_tenant_session_authenticated(port)
        bind_actor("tenant", port.get("id"))
        log_business_event("auth.tenant.login", actor_type="tenant", resource_type="port", resource_id=port.get("id"))
        return json_customer_success(message="登录成功。")
    log_business_event("auth.tenant.login", result="failure", actor_type="tenant", resource_type="port", resource_id=port.get("id"), error_code="invalid_credentials")
    return json_error_response("用户名或密码错误。", 401)
