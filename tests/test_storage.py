import pytest

from app.storage import SchemaBootstrap, SQLiteDatabase


class RecordingLock:
    def __init__(self):
        self.entered = 0
        self.exited = 0

    def __enter__(self):
        self.entered += 1

    def __exit__(self, exc_type, exc_value, traceback):
        self.exited += 1
        return False


def test_sqlite_database_transaction_commits_and_rolls_back(tmp_path):
    database = SQLiteDatabase(tmp_path / "panel.db")

    with database.transaction() as conn:
        conn.execute("CREATE TABLE items (value TEXT NOT NULL)")
        conn.execute("INSERT INTO items (value) VALUES (?)", ("kept",))

    with pytest.raises(RuntimeError, match="rollback"), database.transaction() as conn:
        conn.execute("INSERT INTO items (value) VALUES (?)", ("discarded",))
        raise RuntimeError("rollback")

    with database.connect() as conn:
        values = [row["value"] for row in conn.execute("SELECT value FROM items ORDER BY rowid")]

    assert values == ["kept"]


def test_sqlite_database_transaction_uses_the_shared_write_lock(tmp_path):
    write_lock = RecordingLock()
    database = SQLiteDatabase(tmp_path / "panel.db", write_lock=write_lock)

    with database.transaction() as conn:
        conn.execute("CREATE TABLE items (value TEXT NOT NULL)")

    assert write_lock.entered == 1
    assert write_lock.exited == 1


def test_schema_bootstrap_owns_base_schema_and_runs_extensions(tmp_path):
    database = SQLiteDatabase(tmp_path / "panel.db")
    initialized = []

    def initialize_extension(conn):
        initialized.append(conn)
        conn.execute("CREATE TABLE extension_marker (value TEXT NOT NULL)")

    SchemaBootstrap().initialize(database, schema_initializers=(initialize_extension,))

    with database.connect() as conn:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    assert initialized
    assert tables == {"app_state", "extension_marker"}
    assert "extension_marker" in tables


def test_domain_schema_hooks_install_domain_tables_on_a_clean_database(tmp_path):
    from app.state.ai_routing import AiRoutingService
    from app.state.commerce import CommerceService
    from app.state.dns_failover import DnsFailoverService
    from app.state.ports import PortsService
    from app.state.probes import ProbesService
    from app.state.traffic import TrafficService

    database = SQLiteDatabase(tmp_path / "panel.db")
    ports = PortsService(repository=database)
    traffic = TrafficService(repository=database)
    probes = ProbesService(repository=database)
    ai_routing = AiRoutingService(repository=database, node_controller=object())
    dns_failover = DnsFailoverService(repository=database)
    commerce = CommerceService(repository=database)

    SchemaBootstrap().initialize(
        database,
        schema_initializers=(
            traffic.ensure_traffic_schema,
            probes.ensure_probe_schema,
            ports.ensure_port_schema,
            ai_routing.ensure_ai_schema,
            dns_failover.ensure_dns_failover_schema,
            commerce.ensure_commerce_schema,
        ),
    )

    with database.connect() as conn:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    assert {
        "app_state",
        "ports",
        "traffic_totals",
        "traffic_daily",
        "upstream_probes",
        "upstream_probe_history",
        "ai_domains",
        "ai_domain_observations",
        "dns_failover_state",
        "dns_failover_history",
        "customers",
        "plans",
        "orders",
        "service_subscriptions",
        "order_payment_submissions",
    } <= tables


def test_sqlite_database_owns_connection_and_state_operations(tmp_path):
    database = SQLiteDatabase(tmp_path / "panel.db")

    with database.connect() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        conn.execute("CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        database.set_state(conn, "mode", "active")
        assert database.get_state(conn, "mode") == "active"
        assert database.get_state(conn, "missing", "fallback") == "fallback"
