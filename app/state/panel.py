import threading


from ..config import (
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
    PAYMENT_PROOFS_DIR,
    XRAY_ACCESS_LOG_PATH,
    XRAY_CONFIG_PATH,
    XRAY_DYNAMIC_ROUTING_PATH,
    XRAY_PANEL_PORTS_PATH,
)
from ..dns_failover import DnsFailoverConfig, DnsFailoverManager
from ..xray.node_control import DataPlaneConfig, DataPlaneController

from .ai_routing import AiRoutingService
from .base import CoreService
from .commerce import CommerceService
from .diagnostics import DiagnosticsService
from .dns_failover import DnsFailoverService
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


class PanelState:
    """Facade composing the per-domain services.

    Shared infrastructure (the write lock, stop event, data-plane controller and
    DNS-failover manager) lives on the facade; each service holds a back-reference
    to it as ``self._panel`` and reaches siblings/infra through the facade. Every
    public method of the original god-class resolves via __getattr__, so existing
    call sites — and the tests' instance-attribute monkeypatches — are unchanged.
    """

    def __init__(self):
        self.core = CoreService(self)
        self.ports = PortsService(self)
        self.traffic = TrafficService(self)
        self.probes = ProbesService(self)
        self.dns_failover = DnsFailoverService(self)
        self.ai_routing = AiRoutingService(self)
        self.commerce = CommerceService(self)
        self.diagnostics = DiagnosticsService(self)
        self._services = [
            self.core, self.ports, self.traffic, self.probes,
            self.dns_failover, self.ai_routing, self.commerce, self.diagnostics,
        ]
        self._method_owner = {}
        for service in self._services:
            for attr_name in vars(type(service)):
                if attr_name.startswith("__"):
                    continue
                if callable(getattr(type(service), attr_name)):
                    self._method_owner[attr_name] = service

        # Shared infrastructure (verbatim from the original PanelState.__init__).
        self.write_lock = threading.Lock()
        self.stop_event = threading.Event()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        PAYMENT_PROOFS_DIR.mkdir(parents=True, exist_ok=True)
        XRAY_PANEL_PORTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        XRAY_ACCESS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.data_plane = DataPlaneController(
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
                config_path=self.data_plane_config_path(),
                dynamic_routing_path=DATAPLANE_DYNAMIC_ROUTING_PATH.strip(),
                ai_report_path=DATAPLANE_AI_REPORT_PATH.strip(),
                panel_db_path=DATAPLANE_PANEL_DB_PATH.strip(),
                access_log_path=DATAPLANE_ACCESS_LOG_PATH.strip() or str(XRAY_ACCESS_LOG_PATH),
                panel_ports_path=DATAPLANE_PANEL_PORTS_PATH.strip(),
                source_config_path=XRAY_CONFIG_PATH,
                source_dynamic_routing_path=XRAY_DYNAMIC_ROUTING_PATH,
                source_ai_report_path=self.data_plane_ai_report_source_path(),
                source_panel_ports_path=XRAY_PANEL_PORTS_PATH,
                upstream_host=DEFAULT_UPSTREAM_HOST,
                upstream_port=DEFAULT_UPSTREAM_PORT,
            )
        )
        configured_targets = AI_NODE_SSH_TARGETS or ((AI_NODE_SSH_TARGET,) if AI_NODE_SSH_TARGET else ())
        configured_containers = AI_NODE_CONTAINER_NAMES or ((AI_NODE_CONTAINER_NAME,) if AI_NODE_CONTAINER_NAME else ())
        configured_restart_commands = AI_NODE_RESTART_COMMANDS or ((AI_NODE_RESTART_COMMAND,) if AI_NODE_RESTART_COMMAND else ())
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
                self.ai_nodes[node_id] = DataPlaneController(
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

    def __getattr__(self, name):
        owner_map = self.__dict__.get("_method_owner")
        if owner_map is not None and name in owner_map:
            return getattr(owner_map[name], name)
        raise AttributeError(name)
