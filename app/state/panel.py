import threading

from ..config import (
    AI_DOMAIN_MANAGER_CONTAINER_NAME,
    AI_DOMAIN_MANAGER_DOCKER_BIN,
    AI_DOMAIN_MANAGER_EXECUTION_MODE,
    AI_NODE_API_SERVER,
    AI_NODE_API_SERVERS,
    AI_NODE_CONFIG_OUT,
    AI_NODE_CONFIG_PATH,
    AI_NODE_CONFIG_PATHS,
    AI_NODE_CONTAINER_NAME,
    AI_NODE_CONTAINER_NAMES,
    AI_NODE_DOCKER_BIN,
    AI_NODE_IDS,
    AI_NODE_LABELS,
    AI_NODE_LIST_CONFIGURED,
    AI_NODE_PROBE_HOST,
    AI_NODE_PROBE_HOSTS,
    AI_NODE_RESTART_COMMAND,
    AI_NODE_RESTART_COMMANDS,
    AI_NODE_SSH_BIN,
    AI_NODE_SSH_KNOWN_HOSTS,
    AI_NODE_SSH_OPTIONS,
    AI_NODE_SSH_TARGET,
    AI_NODE_SSH_TARGETS,
    AI_NODE_XRAY_BIN,
    CF_API_TOKEN,
    CF_DNS_RECORD_ID,
    CF_DNS_RECORD_NAME,
    CF_DNS_RECORD_PROXIED,
    CF_DNS_RECORD_TTL,
    CF_DNS_RECORD_TYPE,
    CF_ZONE_ID,
    DATA_DIR,
    DATAPLANE_ACCESS_LOG_PATH,
    DATAPLANE_AI_REPORT_PATH,
    DATAPLANE_API_SERVER,
    DATAPLANE_CONFIG_PATH,
    DATAPLANE_CONTAINER_NAME,
    DATAPLANE_DOCKER_BIN,
    DATAPLANE_DYNAMIC_ROUTING_PATH,
    DATAPLANE_LOCAL_BIN,
    DATAPLANE_PANEL_DB_PATH,
    DATAPLANE_PANEL_PORTS_PATH,
    DATAPLANE_REMOTE_COMMAND_TIMEOUT,
    DATAPLANE_RESTART_COMMAND,
    DATAPLANE_SSH_BIN,
    DATAPLANE_SSH_KNOWN_HOSTS,
    DATAPLANE_SSH_OPTIONS,
    DATAPLANE_SSH_TARGET,
    DATAPLANE_XRAY_BIN,
    DEFAULT_UPSTREAM_HOST,
    DEFAULT_UPSTREAM_PORT,
    DNS_FAILOVER_BACKUP_CONTENT,
    DNS_FAILOVER_BACKUP_LABEL,
    DNS_FAILOVER_ENABLED,
    DNS_FAILOVER_FAILURE_THRESHOLD,
    DNS_FAILOVER_INTERVAL,
    DNS_FAILOVER_PEAK_ENABLED,
    DNS_FAILOVER_PEAK_END,
    DNS_FAILOVER_PEAK_START,
    DNS_FAILOVER_PEAK_TIMEZONE,
    DNS_FAILOVER_PRIMARY_CONTENT,
    DNS_FAILOVER_PROBE_HOST,
    DNS_FAILOVER_PROBE_PORT,
    DNS_FAILOVER_RECOVERY_THRESHOLD,
    DNS_FAILOVER_TIMEOUT,
    MAINTENANCE_INTERVAL,
    PAYMENT_PROOFS_DIR,
    PROBE_ENABLED,
    PROBE_INTERVAL,
    XRAY_ACCESS_LOG_PATH,
    XRAY_CONFIG_PATH,
    XRAY_DYNAMIC_ROUTING_PATH,
    XRAY_ENV_FILE_PATH,
    XRAY_PANEL_PORTS_PATH,
)
from ..dns_failover import DnsFailoverConfig, DnsFailoverManager, resolve_public_ip
from ..errors import ValidationError
from ..helpers import format_optional_display_time
from ..runtime import DNSFailoverWorker, MaintenanceWorker
from ..storage import SchemaBootstrap, SQLiteDatabase
from ..xray.ai_routing.launcher import AiDomainManagerRunner
from ..xray.apply import XrayApplyService
from ..xray.node import DataPlaneConfig, NodeController
from ..xray.node.fleet import (
    aggregate_node_status,
    any_node_running,
    node_statuses,
    restart_node_or_raise,
    sync_node_configs,
)
from ..xray.stats import XrayStatsReader
from .ai_routing import AiRoutingService
from .commerce import CommerceService
from .diagnostics import DiagnosticsService
from .dns_failover import DnsFailoverService
from .lifecycle import ApplicationLifecycle
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
    # Keep the resolver injectable for the failover service.  The indirection
    # also preserves the old test/integration seam that patches this module's
    # ``resolve_public_ip`` global.
    return resolve_public_ip(*args, **kwargs)


class PanelState:
    """Composition root and compatibility facade for the panel domains.

    The facade owns only shared runtime resources and the domain service graph.
    Services receive their repositories, node controllers, renderers, locks and
    sibling services directly; they never retain a reference to this object.
    Legacy flat method calls remain available through concrete delegate methods
    installed below the class, rather than a catch-all attribute hook.
    """

    def __init__(self):
        # Shared infrastructure is built before domain services so every
        # dependency can be passed explicitly at construction time.
        self.write_lock = threading.Lock()
        self.stop_event = threading.Event()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PAYMENT_PROOFS_DIR.mkdir(parents=True, exist_ok=True)
        XRAY_PANEL_PORTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        XRAY_ACCESS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.database = SQLiteDatabase(write_lock=self.write_lock)
        self.schema_bootstrap = SchemaBootstrap()
        self.data_plane = NodeController(
            DataPlaneConfig(
                role="data_plane",
                label="数据面",
                api_server=DATAPLANE_API_SERVER,
                xray_bin=DATAPLANE_XRAY_BIN,
                local_bin=DATAPLANE_LOCAL_BIN,
                docker_bin=DATAPLANE_DOCKER_BIN,
                container_name=DATAPLANE_CONTAINER_NAME,
                restart_command=DATAPLANE_RESTART_COMMAND,
                ssh_target=DATAPLANE_SSH_TARGET,
                ssh_bin=DATAPLANE_SSH_BIN,
                ssh_options=DATAPLANE_SSH_OPTIONS,
                ssh_known_hosts_file=DATAPLANE_SSH_KNOWN_HOSTS,
                remote_command_timeout=DATAPLANE_REMOTE_COMMAND_TIMEOUT,
                config_path=_data_plane_config_path(),
                dynamic_routing_path=DATAPLANE_DYNAMIC_ROUTING_PATH.strip(),
                ai_report_path=DATAPLANE_AI_REPORT_PATH.strip(),
                panel_db_path=DATAPLANE_PANEL_DB_PATH.strip(),
                access_log_path=DATAPLANE_ACCESS_LOG_PATH.strip() or str(XRAY_ACCESS_LOG_PATH),
                panel_ports_path=DATAPLANE_PANEL_PORTS_PATH.strip(),
                source_config_path=XRAY_CONFIG_PATH,
                source_dynamic_routing_path=XRAY_DYNAMIC_ROUTING_PATH,
                source_ai_report_path=_data_plane_ai_report_source_path(),
                source_panel_ports_path=XRAY_PANEL_PORTS_PATH,
                upstream_host=DEFAULT_UPSTREAM_HOST,
                upstream_port=DEFAULT_UPSTREAM_PORT,
            )
        )
        configured_targets = AI_NODE_SSH_TARGETS or ((AI_NODE_SSH_TARGET,) if AI_NODE_SSH_TARGET else ())
        configured_containers = AI_NODE_CONTAINER_NAMES or ((AI_NODE_CONTAINER_NAME,) if AI_NODE_CONTAINER_NAME else ())
        configured_restart_commands = AI_NODE_RESTART_COMMANDS or (
            (AI_NODE_RESTART_COMMAND,) if AI_NODE_RESTART_COMMAND else ()
        )
        node_count = max(
            len(configured_targets),
            len(configured_containers),
            len(configured_restart_commands),
            len(AI_NODE_IDS),
            len(AI_NODE_LABELS),
            0,
        )
        if node_count:
            targets = _expand_ai_node_values(configured_targets, node_count, "AI_NODE_SSH_TARGETS")
            containers = _expand_ai_node_values(configured_containers, node_count, "AI_NODE_CONTAINER_NAMES")
            restart_commands = _expand_ai_node_values(
                configured_restart_commands,
                node_count,
                "AI_NODE_RESTART_COMMANDS",
            )
            labels = (
                _expand_ai_node_values(AI_NODE_LABELS, node_count, "AI_NODE_LABELS")
                if AI_NODE_LABELS
                else tuple(f"AI 节点 {index + 1}" for index in range(node_count))
            )
            node_ids = (
                _expand_ai_node_values(AI_NODE_IDS, node_count, "AI_NODE_IDS")
                if AI_NODE_IDS
                else tuple(f"node-{index + 1}" for index in range(node_count))
            )
            if len(set(node_ids)) != node_count:
                raise ValueError("AI_NODE_IDS 不能包含重复值。")
            api_servers = _expand_ai_node_values(
                AI_NODE_API_SERVERS,
                node_count,
                "AI_NODE_API_SERVERS",
                fallback=AI_NODE_API_SERVER,
            )
            config_paths = _expand_ai_node_values(
                AI_NODE_CONFIG_PATHS,
                node_count,
                "AI_NODE_CONFIG_PATHS",
                fallback=AI_NODE_CONFIG_PATH,
            )
            probe_hosts = _expand_ai_node_values(
                AI_NODE_PROBE_HOSTS,
                node_count,
                "AI_NODE_PROBE_HOSTS",
                fallback=AI_NODE_PROBE_HOST,
            )
            self.ai_nodes = {}
            for index in range(node_count):
                node_id = node_ids[index] if node_count > 1 or AI_NODE_LIST_CONFIGURED else "ai_node"
                label = labels[index] if node_count > 1 or AI_NODE_LIST_CONFIGURED else "AI 节点"
                self.ai_nodes[node_id] = NodeController(
                    DataPlaneConfig(
                        role="ai_node",
                        label=label,
                        api_server=api_servers[index],
                        ssh_target=targets[index],
                        ssh_bin=AI_NODE_SSH_BIN,
                        ssh_options=AI_NODE_SSH_OPTIONS,
                        ssh_known_hosts_file=AI_NODE_SSH_KNOWN_HOSTS,
                        xray_bin=AI_NODE_XRAY_BIN,
                        docker_bin=AI_NODE_DOCKER_BIN,
                        container_name=containers[index],
                        restart_command=restart_commands[index],
                        config_path=config_paths[index],
                        source_config_path=None if AI_NODE_LIST_CONFIGURED else AI_NODE_CONFIG_OUT,
                        upstream_host=probe_hosts[index],
                    )
                )
        else:
            self.ai_nodes = {}
        self.ai_node = next(iter(self.ai_nodes.values()), None)
        self.dns_failover_manager = DnsFailoverManager(
            DnsFailoverConfig(
                enabled=DNS_FAILOVER_ENABLED,
                interval=DNS_FAILOVER_INTERVAL,
                timeout=DNS_FAILOVER_TIMEOUT,
                failure_threshold=DNS_FAILOVER_FAILURE_THRESHOLD,
                recovery_threshold=DNS_FAILOVER_RECOVERY_THRESHOLD,
                probe_host=DNS_FAILOVER_PROBE_HOST,
                probe_port=DNS_FAILOVER_PROBE_PORT,
                api_token=CF_API_TOKEN,
                zone_id=CF_ZONE_ID,
                record_id=CF_DNS_RECORD_ID,
                record_type=CF_DNS_RECORD_TYPE,
                record_name=CF_DNS_RECORD_NAME,
                record_proxied=CF_DNS_RECORD_PROXIED,
                record_ttl=CF_DNS_RECORD_TTL,
                primary_content=DNS_FAILOVER_PRIMARY_CONTENT,
                backup_content=DNS_FAILOVER_BACKUP_CONTENT,
                backup_label=DNS_FAILOVER_BACKUP_LABEL,
                peak_enabled=DNS_FAILOVER_PEAK_ENABLED,
                peak_start=DNS_FAILOVER_PEAK_START,
                peak_end=DNS_FAILOVER_PEAK_END,
                peak_timezone=DNS_FAILOVER_PEAK_TIMEZONE,
            )
        )

        self.xray_stats = XrayStatsReader(
            self.data_plane,
            running_check=lambda: self.data_plane_running(),
        )
        self.xray_apply = XrayApplyService(
            repository=self.database,
            node_controller=self.data_plane,
            ai_nodes=self.ai_nodes,
            ai_node=self.ai_node,
            write_lock=self.write_lock,
        )
        self.ports = PortsService(
            repository=self.database,
            renderer=self.xray_apply,
            write_lock=self.write_lock,
        )
        self.traffic = TrafficService(
            repository=self.database,
            node_controller=self.data_plane,
            stats_reader=self.xray_stats,
            renderer=self.xray_apply,
            write_lock=self.write_lock,
        )
        self.probes = ProbesService(
            repository=self.database,
            write_lock=self.write_lock,
        )
        self.dns_failover = DnsFailoverService(
            repository=self.database,
            node_controller=self.data_plane,
            failover_manager=self.dns_failover_manager,
            backup_xray=self.xray_apply,
            write_lock=self.write_lock,
            public_ip_resolver=_resolve_public_ip,
        )
        self.ai_routing = AiRoutingService(
            repository=self.database,
            node_controller=self.data_plane,
            manager_runner=AiDomainManagerRunner(
                execution_mode=AI_DOMAIN_MANAGER_EXECUTION_MODE,
                container_name=AI_DOMAIN_MANAGER_CONTAINER_NAME,
                docker_bin=AI_DOMAIN_MANAGER_DOCKER_BIN,
            ),
        )
        self.commerce = CommerceService(
            repository=self.database,
            renderer=self.xray_apply,
            ports=self.ports,
            traffic=self.traffic,
            write_lock=self.write_lock,
        )
        self.diagnostics = DiagnosticsService(
            ports=self.ports,
            node_controller=self.data_plane,
        )
        self.xray_apply.bind_services(
            ports_service=self.ports,
            traffic_service=self.traffic,
        )
        self.maintenance_worker = MaintenanceWorker(
            stop_event=self.stop_event,
            traffic_service=self.traffic,
            ports_service=self.ports,
            probes_service=self.probes,
            backup_service=self.xray_apply,
            interval=MAINTENANCE_INTERVAL,
            probe_enabled=PROBE_ENABLED,
            probe_interval=PROBE_INTERVAL,
            backup_mode_interval=max(PROBE_INTERVAL, 60),
        )
        self.dns_failover_worker = DNSFailoverWorker(
            stop_event=self.stop_event,
            failover_service=self.dns_failover,
            interval=DNS_FAILOVER_INTERVAL,
        )
        self.lifecycle = ApplicationLifecycle(
            database=self.database,
            schema_bootstrap=self.schema_bootstrap,
            ports=self.ports,
            traffic=self.traffic,
            probes=self.probes,
            dns_failover=self.dns_failover,
            ai_routing=self.ai_routing,
            commerce=self.commerce,
            xray_apply=self.xray_apply,
            stop_event=self.stop_event,
            maintenance_worker=self.maintenance_worker,
            dns_failover_worker=self.dns_failover_worker,
        )
        self._services = (
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
        return self.data_plane.status_summary()

    def data_plane_configured(self):
        return self.data_plane.is_configured()

    def data_plane_running(self):
        return self.data_plane.is_running()

    def ai_nodes_status(self):
        return node_statuses(self.ai_nodes)

    def ai_node_status(self, nodes=None):
        nodes = self.ai_nodes_status() if nodes is None else nodes
        return aggregate_node_status(nodes)

    def ai_node_running(self):
        return any_node_running(self.ai_nodes)

    @staticmethod
    def _node_running(controller):
        return controller.is_running()

    def sync_ai_node_config(self):
        return sync_node_configs(self.ai_nodes)

    def restart_ai_node_or_raise(self, node_id=None):
        return restart_node_or_raise(self.ai_nodes, self.ai_node, node_id=node_id)

    def ai_node_reachable(self):
        return self.ai_node_running()

    def read_xray_traffic_stats(self):
        return self.xray_stats.read_xray_traffic_stats()

    def restart_data_plane_or_raise(self):
        if not self.data_plane.is_configured():
            raise ValidationError("数据面未配置。")
        if not self.data_plane.supports_restart():
            raise ValidationError("当前数据面未配置可用的重启方式。")
        restarted = self.data_plane.restart()
        if not restarted:
            raise ValidationError("当前数据面不可重启。")
        return self.data_plane.status_summary()

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
