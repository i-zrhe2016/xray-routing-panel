from flask import jsonify

from ..config import CONTROL_PLANE_BACKUP_XRAY_ENABLED, PANEL_HEALTH_REQUIRES_XRAY
from .core import application, route

# Keep the old module-level name available for integrations that imported it
# directly; new handlers use the grouped application facade below.
state = application


@route("/healthz", methods=["GET"])
def healthz():
    dns_status = application.dns_failover.dns_failover_status()
    backup_active = bool(
        CONTROL_PLANE_BACKUP_XRAY_ENABLED
        and dns_status.get("enabled")
        and dns_status.get("current_target") == "backup"
    )
    data_plane_running = False if backup_active else application.nodes.data_plane_running()
    ai_node_running = None if backup_active else application.nodes.ai_node_running()
    healthy = (data_plane_running or backup_active) if PANEL_HEALTH_REQUIRES_XRAY else True
    status_code = 200 if healthy else 500
    return jsonify({
        "ok": healthy,
        "data_plane_running": data_plane_running,
        "ai_node_running": ai_node_running,
        "dns_failover_active": backup_active,
    }), status_code
