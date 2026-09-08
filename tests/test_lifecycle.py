from threading import Event
from unittest.mock import Mock, patch


class FakeConnection:
    def __init__(self, events):
        self.events = events

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.events.append("commit")
        return False

    def execute(self, statement):
        self.events.append(statement.strip())

    def commit(self):
        self.events.append("commit")


class FakeDatabase:
    def __init__(self, events):
        self.events = events

    def connect(self):
        return FakeConnection(self.events)

    def transaction(self):
        self.events.append("BEGIN IMMEDIATE")
        return FakeConnection(self.events)


class FakeSchemaBootstrap:
    def __init__(self, events):
        self.events = events

    def initialize(self, database, schema_initializers):
        assert database is not None
        self.events.append("schema.initialize")
        for initializer in schema_initializers:
            initializer(object())


class FakePorts:
    def __init__(self, events):
        self.events = events

    def ensure_port_schema(self, conn):
        self.events.append("ports.schema")

    def seed_defaults(self):
        self.events.append("ports.seed")

    def normalize_upstream_targets(self):
        self.events.append("ports.normalize")

    def cleanup_expired_ports_in_tx(self, conn):
        self.events.append("ports.cleanup")

    def ensure_subscription_token_in_tx(self, conn):
        self.events.append("ports.subscription")

    def ensure_port_tokens_in_tx(self, conn):
        self.events.append("ports.tokens")

    def ensure_port_credentials_in_tx(self, conn):
        self.events.append("ports.credentials")


class FakeTraffic:
    def __init__(self, events):
        self.events = events

    def ensure_traffic_schema(self, conn):
        self.events.append("traffic.schema")

    def sync_traffic_state(self):
        self.events.append("traffic.sync")


class FakeProbes:
    def __init__(self, events):
        self.events = events

    def ensure_probe_schema(self, conn):
        self.events.append("probes.schema")


class FakeAiRouting:
    def __init__(self, events):
        self.events = events

    def ensure_ai_schema(self, conn):
        self.events.append("ai.schema")


class FakeDnsFailover:
    def __init__(self, events):
        self.events = events

    def ensure_dns_failover_schema(self, conn):
        self.events.append("dns.schema")

    def refresh_dns_failover_record_snapshot(self):
        self.events.append("dns.refresh")


class FakeCommerce:
    def __init__(self, events):
        self.events = events

    def ensure_commerce_schema(self, conn):
        self.events.append("commerce.schema")


class FakeXrayApply:
    def __init__(self, events):
        self.events = events

    def disable_auto_stopped_ports(self, reload_xray):
        self.events.append(("xray.disable", reload_xray))

    def write_current_config(self):
        self.events.append("xray.write")


class FakeWorker:
    def __init__(self, events, label):
        self.events = events
        self.label = label

    def run(self):
        self.events.append(("worker.run", self.label))


def test_application_lifecycle_owns_bootstrap_order():
    from app.state.lifecycle import ApplicationLifecycle

    events = []
    lifecycle = ApplicationLifecycle(
        database=FakeDatabase(events),
        schema_bootstrap=FakeSchemaBootstrap(events),
        ports=FakePorts(events),
        traffic=FakeTraffic(events),
        probes=FakeProbes(events),
        dns_failover=FakeDnsFailover(events),
        ai_routing=FakeAiRouting(events),
        commerce=FakeCommerce(events),
        xray_apply=FakeXrayApply(events),
        stop_event=Event(),
    )

    lifecycle.bootstrap()

    assert events == [
        "schema.initialize",
        "traffic.schema",
        "probes.schema",
        "ports.schema",
        "ai.schema",
        "dns.schema",
        "commerce.schema",
        "ports.seed",
        "ports.normalize",
        "BEGIN IMMEDIATE",
        "ports.cleanup",
        "ports.subscription",
        "ports.tokens",
        "ports.credentials",
        "commit",
        "traffic.sync",
        ("xray.disable", False),
        "xray.write",
        "dns.refresh",
    ]


def test_application_lifecycle_stop_is_idempotent():
    from app.state.lifecycle import ApplicationLifecycle

    events = []
    stop_event = Event()
    lifecycle = ApplicationLifecycle(
        database=FakeDatabase(events),
        schema_bootstrap=FakeSchemaBootstrap(events),
        ports=FakePorts(events),
        traffic=FakeTraffic(events),
        probes=FakeProbes(events),
        dns_failover=FakeDnsFailover(events),
        ai_routing=FakeAiRouting(events),
        commerce=FakeCommerce(events),
        xray_apply=FakeXrayApply(events),
        stop_event=stop_event,
    )

    lifecycle.stop()
    lifecycle.stop()

    assert stop_event.is_set()
    assert events == ["traffic.sync"]


def test_application_lifecycle_starts_runtime_workers_after_bootstrap():
    from app.state.lifecycle import ApplicationLifecycle

    events = []
    lifecycle = ApplicationLifecycle(
        database=FakeDatabase(events),
        schema_bootstrap=FakeSchemaBootstrap(events),
        ports=FakePorts(events),
        traffic=FakeTraffic(events),
        probes=FakeProbes(events),
        dns_failover=FakeDnsFailover(events),
        ai_routing=FakeAiRouting(events),
        commerce=FakeCommerce(events),
        xray_apply=FakeXrayApply(events),
        stop_event=Event(),
        maintenance_worker=FakeWorker(events, "maintenance"),
        dns_failover_worker=FakeWorker(events, "dns"),
    )

    threads = []

    def make_thread(*args, **kwargs):
        thread = Mock()
        thread.start.side_effect = lambda: events.append(("thread.start", kwargs["name"]))
        threads.append(thread)
        return thread

    with patch("app.state.lifecycle.threading.Thread", side_effect=make_thread) as thread_factory:
        lifecycle.start()

    assert [call.kwargs["name"] for call in thread_factory.call_args_list] == [
        "panel-maintenance",
        "dns-failover",
    ]
    assert [thread.start.call_count for thread in threads] == [1, 1]
    assert events[-2:] == [("thread.start", "panel-maintenance"), ("thread.start", "dns-failover")]
