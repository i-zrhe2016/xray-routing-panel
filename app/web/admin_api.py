import sqlite3

from flask import jsonify, request

from ..errors import ValidationError
from .core import (
    build_dashboard_state,
    json_error_response,
    json_snapshot_success_response,
    json_success_response,
    log_business_event,
    request_payload,
    require_csrf,
    route,
    state,
)
from .sqlite_errors import is_listen_port_conflict


@route("/api/dashboard", methods=["GET"])
def api_dashboard():
    return jsonify({"ok": True, "dashboard": build_dashboard_state()})


@route("/api/plans", methods=["GET"])
def api_plans():
    return jsonify({"ok": True, "plans": state.query_plans(public_only=False)})


@route("/api/plans", methods=["POST"])
def api_create_plan():
    require_csrf()
    try:
        payload = state.validate_plan_payload(request_payload())
        state.create_plan(payload)
        log_business_event("commerce.plan.created", resource_type="plan", metadata={"slug": payload.get("slug", "")})
        return json_success_response("套餐已创建。", status_code=201)
    except sqlite3.IntegrityError:
        log_business_event("commerce.plan.created", result="failure", error_code="conflict", resource_type="plan")
        return json_error_response("套餐 slug 已存在，请修改后重试。", status_code=409)
    except ValidationError as exc:
        log_business_event("commerce.plan.created", result="failure", error_code="validation", message=str(exc), resource_type="plan")
        return json_error_response(str(exc), status_code=400)


@route("/api/plans/<int:plan_id>", methods=["PUT"])
def api_update_plan(plan_id):
    require_csrf()
    try:
        payload = state.validate_plan_payload(request_payload())
        state.update_plan(plan_id, payload)
        log_business_event("commerce.plan.updated", resource_type="plan", resource_id=plan_id)
        return json_success_response("套餐已更新。")
    except sqlite3.IntegrityError:
        log_business_event("commerce.plan.updated", result="failure", error_code="conflict", resource_type="plan", resource_id=plan_id)
        return json_error_response("套餐 slug 已存在，请修改后重试。", status_code=409)
    except ValidationError as exc:
        log_business_event("commerce.plan.updated", result="failure", error_code="validation", message=str(exc), resource_type="plan", resource_id=plan_id)
        return json_error_response(str(exc), status_code=400)


@route("/api/orders", methods=["GET"])
def api_orders():
    return jsonify({"ok": True, "orders": state.query_admin_orders(request.args.get("status", "").strip())})


@route("/api/orders/<int:order_id>/fulfill", methods=["POST"])
def api_fulfill_order(order_id):
    require_csrf()
    try:
        state.fulfill_order(order_id, request_payload().get("review_note", ""))
        log_business_event("order.fulfilled", resource_type="order", resource_id=order_id)
        return json_success_response("订单已审核通过并完成开通。")
    except sqlite3.IntegrityError:
        log_business_event("order.fulfilled", result="failure", error_code="conflict", resource_type="order", resource_id=order_id)
        return json_error_response("自动分配端口时出现冲突，请重试。", status_code=409)
    except (ValidationError, RuntimeError) as exc:
        log_business_event("order.fulfilled", result="failure", error_code="rejected", message=str(exc), resource_type="order", resource_id=order_id)
        return json_error_response(str(exc), status_code=400)


@route("/api/orders/<int:order_id>/reject", methods=["POST"])
def api_reject_order(order_id):
    require_csrf()
    try:
        state.reject_order(order_id, request_payload().get("review_note", ""))
        log_business_event("order.rejected", resource_type="order", resource_id=order_id)
        return json_success_response("订单已驳回，客户可重新提交支付凭证。")
    except ValidationError as exc:
        log_business_event("order.rejected", result="failure", error_code="rejected", message=str(exc), resource_type="order", resource_id=order_id)
        return json_error_response(str(exc), status_code=400)


@route("/api/orders/<int:order_id>/cancel", methods=["POST"])
def api_cancel_order(order_id):
    require_csrf()
    try:
        state.cancel_order(order_id, request_payload().get("review_note", ""))
        log_business_event("order.cancelled", resource_type="order", resource_id=order_id)
        return json_success_response("订单已取消。")
    except ValidationError as exc:
        log_business_event("order.cancelled", result="failure", error_code="rejected", message=str(exc), resource_type="order", resource_id=order_id)
        return json_error_response(str(exc), status_code=400)


@route("/api/commerce-settings", methods=["GET"])
def api_commerce_settings():
    return jsonify({"ok": True, "settings": state.get_commerce_settings()})


@route("/api/commerce-settings", methods=["PUT"])
def api_update_commerce_settings():
    require_csrf()
    try:
        state.update_commerce_settings(request_payload())
        log_business_event("commerce.settings.updated", resource_type="commerce_settings")
        return json_success_response("商业化设置已更新。")
    except ValidationError as exc:
        log_business_event("commerce.settings.updated", result="failure", error_code="validation", message=str(exc), resource_type="commerce_settings")
        return json_error_response(str(exc), status_code=400)


@route("/api/dns-failover", methods=["GET"])
def api_dns_failover_status():
    return jsonify({"ok": True, "status": state.dns_failover_status()})


@route("/api/dns-failover/check", methods=["POST"])
def api_dns_failover_check():
    try:
        state.run_dns_failover_check(force=True)
        return json_success_response("DNS 故障切换已执行一次即时检测。")
    except (ValidationError, RuntimeError) as exc:
        log_business_event("dns_failover.checked", result="failure", error_code="probe_failed", message=str(exc), resource_type="dns")
        return json_error_response(str(exc), status_code=400)


@route("/api/dns-failover/switch", methods=["POST"])
def api_dns_failover_switch():
    try:
        payload = request_payload()
        state.switch_dns_target(payload.get("target"))
        return json_success_response("DNS 记录已更新。")
    except (ValidationError, RuntimeError) as exc:
        log_business_event("dns_failover.switched", result="failure", error_code="switch_failed", message=str(exc), resource_type="dns")
        return json_error_response(str(exc), status_code=400)


@route("/api/ai-routing/switch", methods=["POST"])
def api_ai_routing_switch():
    require_csrf()
    try:
        mode = str(request_payload().get("mode") or "").strip().lower()
        state.set_ai_routing_manual_mode(mode)
        message = {
            "forced_fallback": "AI 路由已强制回退到数据面直出。",
            "primary": "AI 路由已切换到主 AI 节点。",
            "backup": "AI 路由已切换到备用 AI 节点。",
            "auto": "AI 路由已恢复自动探测。",
        }.get(mode, "AI 路由模式已更新。")
        return json_snapshot_success_response(message)
    except (ValidationError, RuntimeError) as exc:
        log_business_event(
            "ai_routing.manual_switched",
            result="failure",
            error_code="switch_failed",
            message=str(exc),
            resource_type="ai_routing",
        )
        return json_error_response(str(exc), status_code=400)


@route("/api/subscriptions/rotate", methods=["POST"])
def api_rotate_subscription():
    state.rotate_subscription_token()
    log_business_event("subscription.rotated", resource_type="subscription")
    return json_success_response("订阅链接已重新生成，旧链接已失效。")


@route("/api/ports", methods=["POST"])
def api_create_port():
    payload = {}
    try:
        payload = state.validate_port_payload(request_payload())
        port_id = state.create_port(payload)
        log_business_event("port.created", resource_type="port", resource_id=port_id, metadata={"listen_port": payload.get("listen_port")})
    except sqlite3.IntegrityError as exc:
        if is_listen_port_conflict(exc):
            log_business_event(
                "port.created",
                resource_type="port",
                metadata={"listen_port": payload.get("listen_port", ""), "already_exists": True},
            )
            return json_snapshot_success_response("监听端口已存在，已选中已有端口。", level="info")
        raise
    except (ValidationError, RuntimeError) as exc:
        log_business_event("port.created", result="failure", error_code="validation", message=str(exc), resource_type="port")
        return json_error_response(str(exc), status_code=400)
    return json_snapshot_success_response("端口已创建并写入 Xray。", status_code=201)


@route("/api/ports/<int:port_id>", methods=["PUT"])
def api_update_port(port_id):
    try:
        payload = state.validate_port_payload(request_payload())
        state.update_port(port_id, payload)
        log_business_event("port.updated", resource_type="port", resource_id=port_id)
    except sqlite3.IntegrityError as exc:
        if is_listen_port_conflict(exc):
            log_business_event("port.updated", result="failure", error_code="conflict", resource_type="port", resource_id=port_id)
            return json_error_response("监听端口已存在，请更换其他端口。", status_code=409)
        raise
    except (ValidationError, RuntimeError) as exc:
        log_business_event("port.updated", result="failure", error_code="validation", message=str(exc), resource_type="port", resource_id=port_id)
        return json_error_response(str(exc), status_code=400)
    return json_snapshot_success_response("端口配置已更新。")


@route("/api/ports/<int:port_id>/toggle", methods=["POST"])
def api_toggle_port(port_id):
    try:
        state.toggle_port(port_id)
        log_business_event("port.toggled", resource_type="port", resource_id=port_id)
        return json_success_response("端口状态已切换。")
    except (ValidationError, RuntimeError) as exc:
        log_business_event("port.toggled", result="failure", error_code="rejected", message=str(exc), resource_type="port", resource_id=port_id)
        return json_error_response(str(exc), status_code=400)


@route("/api/ports/<int:port_id>", methods=["DELETE"])
def api_delete_port(port_id):
    try:
        state.delete_port(port_id)
        log_business_event("port.deleted", resource_type="port", resource_id=port_id)
        return json_success_response("端口已删除。")
    except (ValidationError, RuntimeError) as exc:
        if isinstance(exc, ValidationError) and str(exc) == "端口记录不存在。":
            log_business_event(
                "port.deleted",
                resource_type="port",
                resource_id=port_id,
                metadata={"already_missing": True},
            )
            return json_snapshot_success_response("端口已不存在，列表已刷新。", level="info")
        log_business_event("port.deleted", result="failure", error_code="rejected", message=str(exc), resource_type="port", resource_id=port_id)
        return json_error_response(str(exc), status_code=400)


@route("/api/ports/<int:port_id>/reset-traffic", methods=["POST"])
def api_reset_port_traffic(port_id):
    try:
        restored = state.reset_port_traffic(port_id)
        log_business_event("port.traffic_reset", resource_type="port", resource_id=port_id, metadata={"restored": restored})
        message = "流量已重置，端口已恢复启用。" if restored else "流量已重置。"
        return json_success_response(message)
    except (ValidationError, RuntimeError) as exc:
        log_business_event("port.traffic_reset", result="failure", error_code="reset_failed", message=str(exc), resource_type="port", resource_id=port_id)
        return json_error_response(str(exc), status_code=400)


@route("/api/ports/<int:port_id>/rotate-tenant-token", methods=["POST"])
def api_rotate_port_tenant_token(port_id):
    try:
        state.rotate_port_tenant_token(port_id)
        log_business_event("port.tenant_token_rotated", resource_type="port", resource_id=port_id)
        return json_success_response("租户面板地址已重置，旧链接已失效。")
    except (ValidationError, RuntimeError) as exc:
        log_business_event("port.tenant_token_rotated", result="failure", error_code="rotation_failed", message=str(exc), resource_type="port", resource_id=port_id)
        return json_error_response(str(exc), status_code=400)


@route("/api/ports/<int:port_id>/rotate-tenant-credentials", methods=["POST"])
def api_rotate_port_tenant_credentials(port_id):
    try:
        state.rotate_port_tenant_credentials(port_id)
        log_business_event("port.tenant_credentials_rotated", resource_type="port", resource_id=port_id)
        return json_success_response("租户登录用户名和密码已重置。")
    except (ValidationError, RuntimeError) as exc:
        log_business_event("port.tenant_credentials_rotated", result="failure", error_code="rotation_failed", message=str(exc), resource_type="port", resource_id=port_id)
        return json_error_response(str(exc), status_code=400)


@route("/api/ports/<int:port_id>/rotate-subscription-token", methods=["POST"])
def api_rotate_port_subscription_token(port_id):
    try:
        state.rotate_port_subscription_token(port_id)
        log_business_event("subscription.rotated", resource_type="port", resource_id=port_id)
        return json_success_response("租户订阅地址已重置，旧链接已失效。")
    except (ValidationError, RuntimeError) as exc:
        log_business_event("subscription.rotated", result="failure", error_code="rotation_failed", message=str(exc), resource_type="port", resource_id=port_id)
        return json_error_response(str(exc), status_code=400)


@route("/api/data-plane/restart", methods=["POST"])
def api_restart_data_plane():
    try:
        state.restart_data_plane_or_raise()
        log_business_event("node.data_plane.restarted", resource_type="node", resource_id="data_plane")
        return json_success_response("数据面已执行重启。")
    except (ValidationError, RuntimeError) as exc:
        log_business_event("node.data_plane.restarted", result="failure", error_code="restart_failed", message=str(exc), resource_type="node", resource_id="data_plane")
        return json_error_response(str(exc), status_code=400)


@route("/api/data-plane/diagnose", methods=["POST"])
def api_diagnose_data_plane():
    try:
        diagnosis = state.diagnose_data_plane()
        log_business_event("node.data_plane.diagnosed", resource_type="node", resource_id="data_plane")
        return jsonify({"ok": True, "diagnosis": diagnosis})
    except (ValidationError, RuntimeError) as exc:
        log_business_event("node.data_plane.diagnosed", result="failure", error_code="diagnose_failed", message=str(exc), resource_type="node", resource_id="data_plane")
        return json_error_response(str(exc), status_code=400)


@route("/api/ai-node/status", methods=["GET"])
def api_ai_node_status():
    return jsonify({"ok": True, "status": state.ai_node_status()})


@route("/api/ai-nodes/status", methods=["GET"])
def api_ai_nodes_status():
    nodes = state.ai_nodes_status()
    return jsonify({"ok": True, "status": state.ai_node_status(nodes), "nodes": nodes})


@route("/api/ai-node/restart", methods=["POST"])
def api_restart_ai_node():
    try:
        state.restart_ai_node_or_raise()
        log_business_event("node.ai.restarted", resource_type="node", resource_id="ai_node")
        return json_success_response("AI 节点已执行重启。")
    except (ValidationError, RuntimeError) as exc:
        log_business_event("node.ai.restarted", result="failure", error_code="restart_failed", message=str(exc), resource_type="node", resource_id="ai_node")
        return json_error_response(str(exc), status_code=400)


@route("/api/ai-nodes/<node_id>/restart", methods=["POST"])
def api_restart_ai_node_by_id(node_id):
    try:
        state.restart_ai_node_or_raise(node_id)
        log_business_event("node.ai.restarted", resource_type="node", resource_id=node_id)
        return json_success_response("AI 节点已执行重启。")
    except (ValidationError, RuntimeError) as exc:
        log_business_event("node.ai.restarted", result="failure", error_code="restart_failed", message=str(exc), resource_type="node", resource_id=node_id)
        return json_error_response(str(exc), status_code=400)
