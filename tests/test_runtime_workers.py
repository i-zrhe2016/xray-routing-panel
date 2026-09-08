from types import SimpleNamespace

from app.runtime import DNSFailoverWorker, MaintenanceWorker


class OneCycleStopEvent:
    def __init__(self, stop_after_first_wait=False):
        self.wait_calls = []
        self._stop = False
        self.stop_after_first_wait = stop_after_first_wait

    def wait(self, interval):
        self.wait_calls.append(interval)
        if len(self.wait_calls) == 1:
            if self.stop_after_first_wait:
                self._stop = True
                return True
            return False
        self._stop = True
        return True

    def is_set(self):
        return self._stop


def test_maintenance_worker_only_schedules_application_services():
    stop_event = OneCycleStopEvent()
    calls = []
    worker = MaintenanceWorker(
        stop_event=stop_event,
        traffic_service=SimpleNamespace(sync_traffic_state=lambda: calls.append("traffic")),
        ports_service=SimpleNamespace(disable_expired_ports=lambda **kwargs: calls.append(("ports", kwargs))),
        probes_service=SimpleNamespace(run_upstream_probes=lambda: calls.append("probes")),
        backup_service=SimpleNamespace(sync_backup_xray_mode=lambda: calls.append("backup")),
        interval=10,
        probe_enabled=True,
        probe_interval=1,
        backup_mode_interval=1,
    )

    worker.run()

    assert calls == ["traffic", ("ports", {"reload_xray": True}), "probes", "backup"]
    assert stop_event.wait_calls == [10, 10]


def test_dns_failover_worker_has_independent_schedule_and_failure_boundary():
    stop_event = OneCycleStopEvent(stop_after_first_wait=True)
    calls = []

    def check():
        calls.append("check")
        raise RuntimeError("transient failure")

    worker = DNSFailoverWorker(
        stop_event=stop_event,
        failover_service=SimpleNamespace(run_dns_failover_check=check),
        interval=0,
    )

    worker.run()

    assert calls == ["check"]
    assert stop_event.wait_calls == [1]
