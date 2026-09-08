"""Independent DNS failover worker."""

from ..observability.logging import emit_business_event


class DNSFailoverWorker:
    """Schedule DNS failover checks behind their own failure boundary."""

    def __init__(self, stop_event, failover_service, interval):
        self.stop_event = stop_event
        self.failover_service = failover_service
        self.interval = max(1, interval)

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.failover_service.run_dns_failover_check()
            except Exception as exc:  # noqa: BLE001 - worker must survive one failed check
                emit_business_event(
                    "dns_failover.checked",
                    result="failure",
                    actor_type="system",
                    error_code="check_failed",
                    exc=exc,
                )
            if self.stop_event.wait(self.interval):
                return
