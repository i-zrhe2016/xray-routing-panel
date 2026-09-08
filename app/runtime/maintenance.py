"""Scheduled maintenance worker."""

import time

from ..observability.logging import emit_business_event


class MaintenanceWorker:
    """Run periodic maintenance without owning any domain policy."""

    def __init__(
        self,
        stop_event,
        traffic_service,
        ports_service,
        probes_service,
        backup_service,
        interval,
        probe_enabled,
        probe_interval,
        backup_mode_interval,
    ):
        self.stop_event = stop_event
        self.traffic_service = traffic_service
        self.ports_service = ports_service
        self.probes_service = probes_service
        self.backup_service = backup_service
        self.interval = interval
        self.probe_enabled = probe_enabled
        self.probe_interval = probe_interval
        self.backup_mode_interval = backup_mode_interval

    def run(self):
        last_probe_at = 0.0
        last_backup_mode_at = 0.0
        while not self.stop_event.wait(self.interval):
            try:
                self.traffic_service.sync_traffic_state()
                self.ports_service.disable_expired_ports(reload_xray=True)
                now_monotonic = time.monotonic()
                if self.probe_enabled and now_monotonic - last_probe_at >= self.probe_interval:
                    self.probes_service.run_upstream_probes()
                    last_probe_at = now_monotonic
                if now_monotonic - last_backup_mode_at >= self.backup_mode_interval:
                    self.backup_service.sync_backup_xray_mode()
                    last_backup_mode_at = now_monotonic
            except Exception as exc:  # noqa: BLE001 - worker must survive one failed cycle
                emit_business_event(
                    "maintenance.failed",
                    result="failure",
                    actor_type="system",
                    error_code="maintenance_exception",
                    exc=exc,
                )
                continue
