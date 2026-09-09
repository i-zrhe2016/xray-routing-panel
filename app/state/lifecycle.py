"""Application startup and shutdown orchestration."""

import threading

WORKER_JOIN_TIMEOUT_SECONDS = 5.0


class ApplicationLifecycle:
    """Coordinate application startup and shutdown dependencies.

    This class owns ordering only.  Domain policy remains in the application
    services and the periodic scheduling remains in the runtime workers.
    """

    def __init__(
        self,
        *,
        database,
        schema_bootstrap,
        ports,
        traffic,
        probes,
        dns_failover,
        ai_routing,
        commerce,
        xray_apply,
        stop_event,
        maintenance_worker=None,
        dns_failover_worker=None,
    ):
        self.database = database
        self.schema_bootstrap = schema_bootstrap
        self.ports = ports
        self.traffic = traffic
        self.probes = probes
        self.dns_failover = dns_failover
        self.ai_routing = ai_routing
        self.commerce = commerce
        self.xray_apply = xray_apply
        self.stop_event = stop_event
        self.maintenance_worker = maintenance_worker
        self.dns_failover_worker = dns_failover_worker
        self._worker_threads = []
        self._started = False
        self._stopped = False

    def init_db(self):
        self.schema_bootstrap.initialize(
            self.database,
            schema_initializers=(
                # Ports cleanup removes related traffic/probe rows, so those
                # tables must exist before the port migration hook runs.
                self.traffic.ensure_traffic_schema,
                self.probes.ensure_probe_schema,
                self.ports.ensure_port_schema,
                self.ai_routing.ensure_ai_schema,
                self.dns_failover.ensure_dns_failover_schema,
                self.commerce.ensure_commerce_schema,
            ),
        )

    def bootstrap(self):
        self.init_db()
        self.ports.seed_defaults()
        self.ports.normalize_upstream_targets()
        with self.database.transaction() as conn:
            self.ports.cleanup_expired_ports_in_tx(conn)
            self.ports.ensure_subscription_token_in_tx(conn)
            self.ports.ensure_port_tokens_in_tx(conn)
            self.ports.ensure_port_credentials_in_tx(conn)
        self.traffic.sync_traffic_state()
        self.xray_apply.disable_auto_stopped_ports(reload_xray=False)
        self.xray_apply.write_current_config()
        try:
            self.dns_failover.refresh_dns_failover_record_snapshot()
        except Exception:  # noqa: BLE001, S110 - startup must remain available without DNS
            pass

    def start(self):
        if self._stopped:
            raise RuntimeError("Application lifecycle cannot be restarted after stop")
        if self._started:
            return
        self.bootstrap()
        worker_threads = []
        for worker, name in (
            (self.maintenance_worker, "panel-maintenance"),
            (self.dns_failover_worker, "dns-failover"),
        ):
            if worker is None:
                continue
            thread = threading.Thread(target=worker.run, name=name, daemon=True)
            thread.start()
            worker_threads.append(thread)
        self._worker_threads = worker_threads
        self._started = True

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self.stop_event.set()
        try:
            self.traffic.sync_traffic_state()
        finally:
            current_thread = threading.current_thread()
            for thread in self._worker_threads:
                if thread is current_thread:
                    continue
                thread.join(timeout=WORKER_JOIN_TIMEOUT_SECONDS)
