"""Small helpers for composing status across managed node controllers."""

from ...errors import ValidationError


def node_statuses(nodes):
    """Return one controller status summary per configured node."""
    statuses = []
    for node_id, controller in nodes.items():
        status = controller.status_summary()
        status["node_id"] = node_id
        statuses.append(status)
    return statuses


def aggregate_node_status(nodes):
    """Aggregate a list of node status summaries for the panel API."""
    if not nodes:
        return {
            "role": "ai_node",
            "label": "AI 节点",
            "configured": False,
            "reachable": False,
            "xray_running": None,
            "management_target": "",
            "supports_restart": False,
            "supports_sync": False,
            "last_error": "",
            "nodes": [],
            "node_count": 0,
            "all_reachable": False,
            "any_reachable": False,
        }
    if len(nodes) == 1:
        status = dict(nodes[0])
        status.update(
            {
                "nodes": nodes,
                "node_count": 1,
                "all_reachable": bool(status.get("reachable")),
                "any_reachable": bool(status.get("reachable")),
            }
        )
        return status

    reachable = [bool(node.get("reachable")) for node in nodes]
    running = [node.get("xray_running") for node in nodes]
    if all(value is True for value in running):
        aggregate_running = True
    elif any(value is True for value in running):
        aggregate_running = None
    else:
        aggregate_running = False
    errors = [f"{node.get('label')}: {node.get('last_error')}" for node in nodes if node.get("last_error")]
    return {
        "role": "ai_node",
        "label": f"AI 节点（{len(nodes)}）",
        "configured": all(bool(node.get("configured")) for node in nodes),
        "reachable": any(reachable),
        "xray_running": aggregate_running,
        "management_target": "、".join(str(node.get("label") or node.get("node_id")) for node in nodes),
        "api_server": "",
        "config_path": "",
        "access_log_path": "",
        "supports_sync": all(bool(node.get("supports_sync")) for node in nodes),
        "supports_restart": any(bool(node.get("supports_restart")) for node in nodes),
        "last_error": "；".join(errors),
        "nodes": nodes,
        "node_count": len(nodes),
        "all_reachable": all(reachable),
        "any_reachable": any(reachable),
    }


def any_node_running(nodes):
    return any(node_running(controller) for controller in nodes.values())


def node_running(controller):
    try:
        return bool(controller.is_running())
    except (OSError, RuntimeError, ValueError):
        return False


def sync_node_configs(nodes):
    uploaded = []
    for controller in nodes.values():
        if controller.supports_sync():
            uploaded.extend(controller.sync_generated_files(validate_config=True))
    return uploaded


def restart_node_or_raise(nodes, default_controller, node_id=None):
    if not nodes:
        raise ValidationError("AI 节点未配置（AI_NODE_SSH_TARGETS 为空）。")
    controller = nodes.get(node_id) if node_id else default_controller
    if controller is None:
        raise ValidationError(f"AI 节点不存在：{node_id}。")
    if not controller.supports_restart():
        raise ValidationError("AI 节点未配置可用的重启方式。")
    restarted = controller.restart()
    if not restarted:
        raise ValidationError("AI 节点不可重启。")
    status = controller.status_summary()
    status["node_id"] = node_id or next(iter(nodes))
    return status
