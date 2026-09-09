"""Application-facing operations for the managed node inventory."""

from ..errors import ValidationError
from ..xray.node.fleet import (
    aggregate_node_status,
    any_node_running,
    node_statuses,
    restart_node_or_raise,
    sync_node_configs,
)


class NodesService:
    """Expose data-plane and AI-node operations as one application domain."""

    def __init__(self, data_plane, ai_nodes, ai_node=None):
        self.data_plane = data_plane
        self.ai_nodes = {} if ai_nodes is None else ai_nodes
        self.ai_node = ai_node if ai_node is not None else next(iter(self.ai_nodes.values()), None)

    def data_plane_status(self):
        return self.data_plane.status_summary()

    def data_plane_configured(self):
        return self.data_plane.is_configured()

    def data_plane_running(self):
        return self.data_plane.is_running()

    def ai_nodes_status(self):
        return node_statuses(self.ai_nodes)

    def ai_node_status(self, nodes=None):
        current_nodes = self.ai_nodes_status() if nodes is None else nodes
        return aggregate_node_status(current_nodes)

    def ai_node_running(self):
        return any_node_running(self.ai_nodes)

    def ai_node_reachable(self):
        return self.ai_node_running()

    def sync_ai_node_config(self):
        return sync_node_configs(self.ai_nodes)

    def restart_ai_node_or_raise(self, node_id=None):
        return restart_node_or_raise(self.ai_nodes, self.ai_node, node_id=node_id)

    def restart_data_plane_or_raise(self):
        if not self.data_plane.is_configured():
            raise ValidationError("数据面未配置。")
        if not self.data_plane.supports_restart():
            raise ValidationError("当前数据面未配置可用的重启方式。")
        restarted = self.data_plane.restart()
        if not restarted:
            raise ValidationError("当前数据面不可重启。")
        return self.data_plane.status_summary()


__all__ = ["NodesService"]
