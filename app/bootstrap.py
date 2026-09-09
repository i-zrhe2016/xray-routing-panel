"""Application composition root.

This module is the only control-plane construction path for the shared
database, node controllers, application services, workers and lifecycle. The
legacy :class:`app.state.PanelState` API remains available as a facade for
callers that have not migrated yet.
"""

import threading

from . import config
from .state.panel import PanelState


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
    explicit = config.DATAPLANE_CONFIG_PATH.strip()
    if explicit:
        return explicit
    if config.DATAPLANE_SSH_TARGET or config.DATAPLANE_LOCAL_BIN:
        return str(config.XRAY_CONFIG_PATH)
    return "/etc/xray/config.json"


def _data_plane_ai_report_source_path():
    return config.XRAY_ENV_FILE_PATH.parent / "reports" / "hourly-domains" / "latest.json"


def _resolve_public_ip(*args, **kwargs):
    # Keep the resolver injectable for the failover service while keeping the
    # construction seam in the composition root.
    from .dns_failover import resolve_public_ip

    return resolve_public_ip(*args, **kwargs)


def build_application_components():
    """Build the shared application object graph without constructing a facade.

    Returning a mapping keeps the wiring explicit and lets the legacy
    ``PanelState()`` constructor adopt the same graph during migration without
    moving construction back into the facade module.
    """

    from .dns_failover import DnsFailoverConfig, DnsFailoverManager
    from .runtime import DNSFailoverWorker, MaintenanceWorker
    from .state.ai_routing import AiRoutingService
    from .state.commerce import CommerceService
    from .state.diagnostics import DiagnosticsService
    from .state.dns_failover import DnsFailoverService
    from .state.lifecycle import ApplicationLifecycle
    from .state.nodes import NodesService
    from .state.ports import PortsService
    from .state.probes import ProbesService
    from .state.traffic import TrafficService
    from .storage import SchemaBootstrap, SQLiteDatabase
    from .xray.ai_routing.launcher import AiDomainManagerRunner
    from .xray.apply import XrayApplyService
    from .xray.node import DataPlaneConfig, NodeController
    from .xray.stats import XrayStatsReader

    write_lock = threading.Lock()
    stop_event = threading.Event()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.PAYMENT_PROOFS_DIR.mkdir(parents=True, exist_ok=True)
    config.XRAY_PANEL_PORTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.XRAY_ACCESS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    database = SQLiteDatabase(write_lock=write_lock)
    schema_bootstrap = SchemaBootstrap()
    data_plane = NodeController(
        DataPlaneConfig(
            role="data_plane",
            label="数据面",
            api_server=config.DATAPLANE_API_SERVER,
            xray_bin=config.DATAPLANE_XRAY_BIN,
            local_bin=config.DATAPLANE_LOCAL_BIN,
            docker_bin=config.DATAPLANE_DOCKER_BIN,
            container_name=config.DATAPLANE_CONTAINER_NAME,
            restart_command=config.DATAPLANE_RESTART_COMMAND,
            ssh_target=config.DATAPLANE_SSH_TARGET,
            ssh_bin=config.DATAPLANE_SSH_BIN,
            ssh_options=config.DATAPLANE_SSH_OPTIONS,
            ssh_known_hosts_file=config.DATAPLANE_SSH_KNOWN_HOSTS,
            remote_command_timeout=config.DATAPLANE_REMOTE_COMMAND_TIMEOUT,
            config_path=_data_plane_config_path(),
            dynamic_routing_path=config.DATAPLANE_DYNAMIC_ROUTING_PATH.strip(),
            ai_report_path=config.DATAPLANE_AI_REPORT_PATH.strip(),
            panel_db_path=config.DATAPLANE_PANEL_DB_PATH.strip(),
            access_log_path=config.DATAPLANE_ACCESS_LOG_PATH.strip() or str(config.XRAY_ACCESS_LOG_PATH),
            panel_ports_path=config.DATAPLANE_PANEL_PORTS_PATH.strip(),
            source_config_path=config.XRAY_CONFIG_PATH,
            source_dynamic_routing_path=config.XRAY_DYNAMIC_ROUTING_PATH,
            source_ai_report_path=_data_plane_ai_report_source_path(),
            source_panel_ports_path=config.XRAY_PANEL_PORTS_PATH,
            upstream_host=config.DEFAULT_UPSTREAM_HOST,
            upstream_port=config.DEFAULT_UPSTREAM_PORT,
        )
    )

    configured_targets = config.AI_NODE_SSH_TARGETS or (
        (config.AI_NODE_SSH_TARGET,) if config.AI_NODE_SSH_TARGET else ()
    )
    configured_containers = config.AI_NODE_CONTAINER_NAMES or (
        (config.AI_NODE_CONTAINER_NAME,) if config.AI_NODE_CONTAINER_NAME else ()
    )
    configured_restart_commands = config.AI_NODE_RESTART_COMMANDS or (
        (config.AI_NODE_RESTART_COMMAND,) if config.AI_NODE_RESTART_COMMAND else ()
    )
    node_count = max(
        len(configured_targets),
        len(configured_containers),
        len(configured_restart_commands),
        len(config.AI_NODE_IDS),
        len(config.AI_NODE_LABELS),
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
            _expand_ai_node_values(config.AI_NODE_LABELS, node_count, "AI_NODE_LABELS")
            if config.AI_NODE_LABELS
            else tuple(f"AI 节点 {index + 1}" for index in range(node_count))
        )
        node_ids = (
            _expand_ai_node_values(config.AI_NODE_IDS, node_count, "AI_NODE_IDS")
            if config.AI_NODE_IDS
            else tuple(f"node-{index + 1}" for index in range(node_count))
        )
        if len(set(node_ids)) != node_count:
            raise ValueError("AI_NODE_IDS 不能包含重复值。")
        api_servers = _expand_ai_node_values(
            config.AI_NODE_API_SERVERS,
            node_count,
            "AI_NODE_API_SERVERS",
            fallback=config.AI_NODE_API_SERVER,
        )
        config_paths = _expand_ai_node_values(
            config.AI_NODE_CONFIG_PATHS,
            node_count,
            "AI_NODE_CONFIG_PATHS",
            fallback=config.AI_NODE_CONFIG_PATH,
        )
        probe_hosts = _expand_ai_node_values(
            config.AI_NODE_PROBE_HOSTS,
            node_count,
            "AI_NODE_PROBE_HOSTS",
            fallback=config.AI_NODE_PROBE_HOST,
        )
        ai_nodes = {}
        for index in range(node_count):
            node_id = node_ids[index] if node_count > 1 or config.AI_NODE_LIST_CONFIGURED else "ai_node"
            label = labels[index] if node_count > 1 or config.AI_NODE_LIST_CONFIGURED else "AI 节点"
            ai_nodes[node_id] = NodeController(
                DataPlaneConfig(
                    role="ai_node",
                    label=label,
                    api_server=api_servers[index],
                    ssh_target=targets[index],
                    ssh_bin=config.AI_NODE_SSH_BIN,
                    ssh_options=config.AI_NODE_SSH_OPTIONS,
                    ssh_known_hosts_file=config.AI_NODE_SSH_KNOWN_HOSTS,
                    xray_bin=config.AI_NODE_XRAY_BIN,
                    docker_bin=config.AI_NODE_DOCKER_BIN,
                    container_name=containers[index],
                    restart_command=restart_commands[index],
                    config_path=config_paths[index],
                    source_config_path=None if config.AI_NODE_LIST_CONFIGURED else config.AI_NODE_CONFIG_OUT,
                    upstream_host=probe_hosts[index],
                )
            )
    else:
        ai_nodes = {}
    ai_node = next(iter(ai_nodes.values()), None)
    nodes = NodesService(
        data_plane=data_plane,
        ai_nodes=ai_nodes,
        ai_node=ai_node,
    )

    dns_failover_manager = DnsFailoverManager(
        DnsFailoverConfig(
            enabled=config.DNS_FAILOVER_ENABLED,
            interval=config.DNS_FAILOVER_INTERVAL,
            timeout=config.DNS_FAILOVER_TIMEOUT,
            failure_threshold=config.DNS_FAILOVER_FAILURE_THRESHOLD,
            recovery_threshold=config.DNS_FAILOVER_RECOVERY_THRESHOLD,
            probe_host=config.DNS_FAILOVER_PROBE_HOST,
            probe_port=config.DNS_FAILOVER_PROBE_PORT,
            api_token=config.CF_API_TOKEN,
            zone_id=config.CF_ZONE_ID,
            record_id=config.CF_DNS_RECORD_ID,
            record_type=config.CF_DNS_RECORD_TYPE,
            record_name=config.CF_DNS_RECORD_NAME,
            record_proxied=config.CF_DNS_RECORD_PROXIED,
            record_ttl=config.CF_DNS_RECORD_TTL,
            primary_content=config.DNS_FAILOVER_PRIMARY_CONTENT,
            backup_content=config.DNS_FAILOVER_BACKUP_CONTENT,
            backup_label=config.DNS_FAILOVER_BACKUP_LABEL,
            peak_enabled=config.DNS_FAILOVER_PEAK_ENABLED,
            peak_start=config.DNS_FAILOVER_PEAK_START,
            peak_end=config.DNS_FAILOVER_PEAK_END,
            peak_timezone=config.DNS_FAILOVER_PEAK_TIMEZONE,
        )
    )

    xray_stats = XrayStatsReader(data_plane)
    xray_apply = XrayApplyService(
        repository=database,
        node_controller=data_plane,
        ai_nodes=ai_nodes,
        ai_node=ai_node,
        write_lock=write_lock,
    )
    ports = PortsService(
        repository=database,
        renderer=xray_apply,
        write_lock=write_lock,
    )
    traffic = TrafficService(
        repository=database,
        node_controller=data_plane,
        stats_reader=xray_stats,
        renderer=xray_apply,
        write_lock=write_lock,
    )
    probes = ProbesService(
        repository=database,
        write_lock=write_lock,
    )
    dns_failover = DnsFailoverService(
        repository=database,
        node_controller=data_plane,
        failover_manager=dns_failover_manager,
        backup_xray=xray_apply,
        write_lock=write_lock,
        public_ip_resolver=_resolve_public_ip,
    )
    ai_routing = AiRoutingService(
        repository=database,
        node_controller=data_plane,
        manager_runner=AiDomainManagerRunner(
            execution_mode=config.AI_DOMAIN_MANAGER_EXECUTION_MODE,
            container_name=config.AI_DOMAIN_MANAGER_CONTAINER_NAME,
            docker_bin=config.AI_DOMAIN_MANAGER_DOCKER_BIN,
        ),
    )
    commerce = CommerceService(
        repository=database,
        renderer=xray_apply,
        ports=ports,
        traffic=traffic,
        write_lock=write_lock,
    )
    diagnostics = DiagnosticsService(
        ports=ports,
        node_controller=data_plane,
    )
    xray_apply.bind_services(
        ports_service=ports,
        traffic_service=traffic,
    )
    maintenance_worker = MaintenanceWorker(
        stop_event=stop_event,
        traffic_service=traffic,
        ports_service=ports,
        probes_service=probes,
        backup_service=xray_apply,
        interval=config.MAINTENANCE_INTERVAL,
        probe_enabled=config.PROBE_ENABLED,
        probe_interval=config.PROBE_INTERVAL,
        backup_mode_interval=max(config.PROBE_INTERVAL, 60),
    )
    dns_failover_worker = DNSFailoverWorker(
        stop_event=stop_event,
        failover_service=dns_failover,
        interval=config.DNS_FAILOVER_INTERVAL,
    )
    lifecycle = ApplicationLifecycle(
        database=database,
        schema_bootstrap=schema_bootstrap,
        ports=ports,
        traffic=traffic,
        probes=probes,
        dns_failover=dns_failover,
        ai_routing=ai_routing,
        commerce=commerce,
        xray_apply=xray_apply,
        stop_event=stop_event,
        maintenance_worker=maintenance_worker,
        dns_failover_worker=dns_failover_worker,
    )

    return {
        "write_lock": write_lock,
        "stop_event": stop_event,
        "database": database,
        "schema_bootstrap": schema_bootstrap,
        "data_plane": data_plane,
        "ai_nodes": ai_nodes,
        "ai_node": ai_node,
        "nodes": nodes,
        "dns_failover_manager": dns_failover_manager,
        "xray_stats": xray_stats,
        "xray_apply": xray_apply,
        "ports": ports,
        "traffic": traffic,
        "probes": probes,
        "dns_failover": dns_failover,
        "ai_routing": ai_routing,
        "commerce": commerce,
        "diagnostics": diagnostics,
        "maintenance_worker": maintenance_worker,
        "dns_failover_worker": dns_failover_worker,
        "lifecycle": lifecycle,
    }


class Application(PanelState):
    """Canonical application facade returned by :func:`build_application`.

    These domain services are the supported entry points for new callers.
    Lower-level components remain exposed on the inherited facade only for
    compatibility with existing workers, scripts and view migrations.
    """

    PUBLIC_COMPONENT_NAMES = (
        "ports",
        "traffic",
        "probes",
        "nodes",
        "ai_routing",
        "dns_failover",
        "commerce",
        "diagnostics",
        "lifecycle",
    )


def build_application():
    """Build and return the complete control-plane application facade."""

    return Application(build_application_components())


__all__ = ["Application", "build_application", "build_application_components"]
