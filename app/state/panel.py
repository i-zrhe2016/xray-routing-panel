from ..config import (
    DATAPLANE_CONFIG_PATH,
    DATAPLANE_LOCAL_BIN,
    DATAPLANE_SSH_TARGET,
    XRAY_CONFIG_PATH,
    XRAY_ENV_FILE_PATH,
)
from ..dns_failover import resolve_public_ip
from ..helpers import format_optional_display_time
from ..xray.apply import XrayApplyService
from .ai_routing import AiRoutingService
from .commerce import CommerceService
from .diagnostics import DiagnosticsService
from .dns_failover import DnsFailoverService
from .lifecycle import ApplicationLifecycle
from .nodes import NodesService
from .ports import PortsService
from .probes import ProbesService
from .traffic import TrafficService


def _expand_ai_node_values(values, count, field_name, fallback=""):
    values = tuple(values or ())
    if not values:
        return tuple(fallback for _ in range(count))
    if len(values) == 1:
        return tuple(values[0] for _ in range(count))
    if len(values) != count:
        raise ValueError(f"{field_name} 必须与 AI 节点数量一致。")
    return values


def _data_plane_config_path():
    explicit = DATAPLANE_CONFIG_PATH.strip()
    if explicit:
        return explicit
    if DATAPLANE_SSH_TARGET or DATAPLANE_LOCAL_BIN:
        return str(XRAY_CONFIG_PATH)
    return "/etc/xray/config.json"


def _data_plane_ai_report_source_path():
    return XRAY_ENV_FILE_PATH.parent / "reports" / "hourly-domains" / "latest.json"


def _resolve_public_ip(*args, **kwargs):
    # Keep the legacy test/integration seam on the facade while the normal
    # construction path remains owned by app.bootstrap.
    return resolve_public_ip(*args, **kwargs)


class PanelState:
    """Compatibility facade for the panel domains.

    The application composition root owns construction of the shared runtime
    resources and domain service graph. This class only stores that graph and
    exposes the legacy flat delegate methods used during migration.
    Services receive their repositories, node controllers, renderers, locks and
    sibling services directly; they never retain a reference to this object.
    Legacy flat method calls remain available through concrete delegate methods,
    rather than a catch-all attribute hook.
    """

    _COMPONENT_NAMES = (
        "write_lock",
        "stop_event",
        "database",
        "schema_bootstrap",
        "data_plane",
        "ai_nodes",
        "ai_node",
        "nodes",
        "dns_failover_manager",
        "xray_stats",
        "xray_apply",
        "ports",
        "traffic",
        "probes",
        "dns_failover",
        "ai_routing",
        "commerce",
        "diagnostics",
        "maintenance_worker",
        "dns_failover_worker",
        "lifecycle",
    )

    def __init__(self, components=None):
        if components is None:
            # Preserve the legacy direct-construction API without retaining
            # dependency construction in the facade module. The import is
            # intentionally lazy so app.bootstrap can import this class.
            from ..bootstrap import build_application_components

            components = build_application_components()
        missing = [name for name in self._COMPONENT_NAMES if name not in components]
        if missing:
            raise ValueError(f"应用组件缺失: {', '.join(missing)}")
        for name in self._COMPONENT_NAMES:
            setattr(self, name, components[name])
        # Preserve the legacy override seam used by tests and integrations:
        # traffic stats should follow the facade's data-plane status method.
        if hasattr(self.xray_stats, "running_check"):
            self.xray_stats.running_check = lambda: self.data_plane_running()
        if hasattr(self.dns_failover, "public_ip_resolver"):
            self.dns_failover.public_ip_resolver = _resolve_public_ip
        self._services = (
            self.nodes,
            self.xray_apply,
            self.ports,
            self.traffic,
            self.probes,
            self.dns_failover,
            self.ai_routing,
            self.commerce,
            self.diagnostics,
            self.lifecycle,
        )

    def connect(self):
        return self.database.connect()

    def get_state(self, conn, key, default=None):
        return self.database.get_state(conn, key, default)

    def set_state(self, conn, key, value):
        return self.database.set_state(conn, key, value)

    def apply_state_update(self, operation):
        return self.database.apply_state_update(operation)

    def data_plane_config_path(self):
        return _data_plane_config_path()

    def data_plane_ai_report_source_path(self):
        return _data_plane_ai_report_source_path()

    def data_plane_status(self):
        return self.nodes.data_plane_status()

    def data_plane_configured(self):
        return self.nodes.data_plane_configured()

    def data_plane_running(self):
        return self.nodes.data_plane_running()

    def ai_nodes_status(self):
        return self.nodes.ai_nodes_status()

    def ai_node_status(self, nodes=None):
        return self.nodes.ai_node_status(nodes)

    def ai_node_running(self):
        return self.nodes.ai_node_running()

    def sync_ai_node_config(self):
        return self.nodes.sync_ai_node_config()

    def restart_ai_node_or_raise(self, node_id=None):
        return self.nodes.restart_ai_node_or_raise(node_id)

    def ai_node_reachable(self):
        return self.nodes.ai_node_reachable()

    def read_xray_traffic_stats(self):
        return self.xray_stats.read_xray_traffic_stats()

    def restart_data_plane_or_raise(self):
        return self.nodes.restart_data_plane_or_raise()

    def format_optional_display_time(self, value, default="暂无"):
        return format_optional_display_time(value, default=default)

    def maintenance_loop(self):
        return self.maintenance_worker.run()

    def dns_failover_loop(self):
        return self.dns_failover_worker.run()

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)
        service_attr = getattr(type(self), "_delegate_owners", {}).get(name)
        if service_attr is None:
            return
        service = getattr(self, service_attr)
        delegate = getattr(type(self), name, None)
        is_delegate_restore = getattr(value, "__self__", None) is self and getattr(value, "__func__", None) is delegate
        if is_delegate_restore:
            service.__dict__.pop(name, None)
            return
        setattr(service, name, value)


def _make_delegate(service_attr, method_name):
    def delegate(self, *args, **kwargs):
        service = getattr(self, service_attr)
        return getattr(service, method_name)(*args, **kwargs)

    delegate.__name__ = method_name
    delegate.__qualname__ = f"PanelState.{method_name}"
    return delegate


_DELEGATE_BINDINGS = {}
for _service_attr, _service_type in (
    ("nodes", NodesService),
    ("lifecycle", ApplicationLifecycle),
    ("xray_apply", XrayApplyService),
    ("ports", PortsService),
    ("traffic", TrafficService),
    ("probes", ProbesService),
    ("dns_failover", DnsFailoverService),
    ("ai_routing", AiRoutingService),
    ("commerce", CommerceService),
    ("diagnostics", DiagnosticsService),
):
    for _method_name in vars(_service_type):
        if _method_name in {"__init__", "bind_services"} or _method_name.startswith("__"):
            continue
        if callable(getattr(_service_type, _method_name, None)) and _method_name not in _DELEGATE_BINDINGS:
            _DELEGATE_BINDINGS[_method_name] = (_service_attr, _method_name)
            setattr(PanelState, _method_name, _make_delegate(_service_attr, _method_name))

PanelState._delegate_owners = {name: service_attr for name, (service_attr, _method_name) in _DELEGATE_BINDINGS.items()}
