import inspect


def test_panel_state_wires_domains_with_explicit_dependencies():
    from app.state import PanelState
    from app.storage import SchemaBootstrap, SQLiteDatabase
    from app.xray.ai_routing.launcher import AiDomainManagerRunner

    state = PanelState()

    assert not hasattr(PanelState, "__getattr__")
    assert isinstance(state.database, SQLiteDatabase)
    assert isinstance(state.schema_bootstrap, SchemaBootstrap)
    assert state.xray_apply.repository is state.database
    assert state.xray_apply.node_controller is state.data_plane
    assert state.xray_apply.ports_service is state.ports
    assert state.xray_apply.traffic_service is state.traffic
    assert state.xray_stats.node_controller is state.data_plane
    assert state.traffic.stats_reader is state.xray_stats
    assert state.ports.repository is state.database
    assert state.ports.renderer is state.xray_apply
    assert state.traffic.repository is state.database
    assert state.traffic.node_controller is state.data_plane
    assert state.traffic.renderer is state.xray_apply
    assert state.probes.repository is state.database
    assert state.dns_failover.repository is state.database
    assert state.dns_failover.node_controller is state.data_plane
    assert state.dns_failover.failover_manager is state.dns_failover_manager
    assert state.dns_failover.backup_xray is state.xray_apply
    assert state.ai_routing.repository is state.database
    assert state.ai_routing.node_controller is state.data_plane
    assert isinstance(state.ai_routing.manager_runner, AiDomainManagerRunner)
    assert state.commerce.repository is state.database
    assert state.commerce.ports is state.ports
    assert state.commerce.traffic is state.traffic
    assert state.diagnostics.ports is state.ports
    assert state.diagnostics.node_controller is state.data_plane
    assert state.lifecycle.database is state.database
    assert state.lifecycle.ports is state.ports
    assert state.lifecycle.xray_apply is state.xray_apply
    assert state.lifecycle.maintenance_worker is state.maintenance_worker
    assert state.lifecycle.dns_failover_worker is state.dns_failover_worker


def test_domain_services_do_not_retain_panel_backreferences():
    from app.state import PanelState
    from app.state.ai_routing import AiRoutingService
    from app.state.commerce import CommerceService
    from app.state.diagnostics import DiagnosticsService
    from app.state.dns_failover import DnsFailoverService
    from app.state.ports import PortsService
    from app.state.probes import ProbesService
    from app.state.traffic import TrafficService

    state = PanelState()
    service_types = (
        type(state.xray_apply),
        PortsService,
        TrafficService,
        ProbesService,
        DnsFailoverService,
        AiRoutingService,
        CommerceService,
        DiagnosticsService,
        type(state.lifecycle),
    )

    for service_type in service_types:
        assert "self._panel" not in inspect.getsource(service_type)

    from app.state.commerce import CommerceService

    assert "renderer._" not in inspect.getsource(CommerceService)

    for service in state._services:
        assert "_panel" not in vars(service)
        assert state not in vars(service).values()



def test_ai_routing_service_accepts_named_route_dependencies():
    from app.state.ai_routing import AiRoutingService

    repository = object()
    node_controller = object()
    service = AiRoutingService(
        repository=repository,
        node_controller=node_controller,
    )

    assert service.repository is repository
    assert service.node_controller is node_controller
