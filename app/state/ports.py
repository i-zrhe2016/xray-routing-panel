from datetime import datetime

from ..config import (
    DEFAULT_UPSTREAM_HOST,
    DEFAULT_UPSTREAM_PORT,
    LOCAL_TZ,
    SEED_LISTEN_PORT,
)
from ..errors import ValidationError
from ..helpers import (
    format_display_time,
    format_input_time,
    generate_access_token,
    generate_subscription_token,
    generate_tenant_password,
    generate_tenant_username,
    human_bytes,
    parse_data_size,
    parse_expiry,
    parse_note,
    parse_port,
    status_payload,
    utc_iso_now,
    utc_now,
)


class PortsService:
    """Port queries and mutations backed by explicit persistence capabilities."""

    def __init__(self, repository=None, renderer=None, write_lock=None):
        self.repository = repository
        self.renderer = renderer or repository
        self.write_lock = write_lock

    def ensure_port_schema(self, conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listen_port INTEGER NOT NULL UNIQUE,
                upstream_host TEXT NOT NULL,
                upstream_port INTEGER NOT NULL,
                tenant_token TEXT NOT NULL DEFAULT '',
                subscription_token TEXT NOT NULL DEFAULT '',
                tenant_username TEXT NOT NULL DEFAULT '',
                tenant_password TEXT NOT NULL DEFAULT '',
                expires_at TEXT,
                traffic_limit_bytes INTEGER,
                enabled INTEGER NOT NULL DEFAULT 1,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(ports)").fetchall()}
        if "tenant_token" not in columns:
            conn.execute("ALTER TABLE ports ADD COLUMN tenant_token TEXT NOT NULL DEFAULT ''")
        if "subscription_token" not in columns:
            conn.execute("ALTER TABLE ports ADD COLUMN subscription_token TEXT NOT NULL DEFAULT ''")
        if "tenant_username" not in columns:
            conn.execute("ALTER TABLE ports ADD COLUMN tenant_username TEXT NOT NULL DEFAULT ''")
        if "tenant_password" not in columns:
            conn.execute("ALTER TABLE ports ADD COLUMN tenant_password TEXT NOT NULL DEFAULT ''")
        if "traffic_limit_bytes" not in columns:
            conn.execute("ALTER TABLE ports ADD COLUMN traffic_limit_bytes INTEGER")
        if "customer_id" not in columns:
            conn.execute("ALTER TABLE ports ADD COLUMN customer_id INTEGER")
        if "service_subscription_id" not in columns:
            conn.execute("ALTER TABLE ports ADD COLUMN service_subscription_id INTEGER")
        if "source_order_id" not in columns:
            conn.execute("ALTER TABLE ports ADD COLUMN source_order_id INTEGER")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ports_tenant_token
            ON ports(tenant_token)
            WHERE tenant_token != ''
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ports_subscription_token
            ON ports(subscription_token)
            WHERE subscription_token != ''
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ports_tenant_username
            ON ports(tenant_username)
            WHERE tenant_username != ''
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ports_customer_id ON ports(customer_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ports_service_subscription_id ON ports(service_subscription_id)")
        self.cleanup_expired_ports_in_tx(conn)
        self.ensure_port_tokens_in_tx(conn)
        self.ensure_port_credentials_in_tx(conn)

    def generate_unique_port_token(self, conn, column_name):
        if column_name not in {"tenant_token", "subscription_token"}:
            raise ValueError("unsupported port token column")
        for _ in range(16):
            token = generate_access_token()
            row = conn.execute(
                f"SELECT 1 FROM ports WHERE {column_name} = ? LIMIT 1",
                (token,),
            ).fetchone()
            if row is None:
                return token
        raise RuntimeError(f"无法为 {column_name} 生成唯一 token。")

    def generate_unique_tenant_username(self, conn):
        for _ in range(16):
            username = generate_tenant_username()
            row = conn.execute(
                "SELECT 1 FROM ports WHERE tenant_username = ? LIMIT 1",
                (username,),
            ).fetchone()
            if row is None:
                return username
        raise RuntimeError("无法生成唯一租户用户名。")

    def ensure_port_tokens_in_tx(self, conn):
        rows = conn.execute(
            """
            SELECT id, tenant_token, subscription_token
            FROM ports
            """
        ).fetchall()
        for row in rows:
            updates = {}
            if not str(row["tenant_token"] or "").strip():
                updates["tenant_token"] = self.generate_unique_port_token(conn, "tenant_token")
            if not str(row["subscription_token"] or "").strip():
                updates["subscription_token"] = self.generate_unique_port_token(conn, "subscription_token")
            if not updates:
                continue
            assignments = ", ".join(f"{column} = ?" for column in updates)
            values = list(updates.values()) + [row["id"]]
            conn.execute(f"UPDATE ports SET {assignments} WHERE id = ?", values)

    def ensure_port_credentials_in_tx(self, conn):
        rows = conn.execute(
            """
            SELECT id, tenant_username, tenant_password
            FROM ports
            """
        ).fetchall()
        for row in rows:
            updates = {}
            if not str(row["tenant_username"] or "").strip():
                updates["tenant_username"] = self.generate_unique_tenant_username(conn)
            if not str(row["tenant_password"] or "").strip():
                updates["tenant_password"] = generate_tenant_password()
            if not updates:
                continue
            assignments = ", ".join(f"{column} = ?" for column in updates)
            values = list(updates.values()) + [row["id"]]
            conn.execute(f"UPDATE ports SET {assignments} WHERE id = ?", values)

    def ensure_subscription_token_in_tx(self, conn):
        token = str(self.repository.get_state(conn, "subscription_token", "") or "").strip()
        if token:
            return token
        token = generate_subscription_token()
        self.repository.set_state(conn, "subscription_token", token)
        return token

    def normalize_upstream_targets(self):
        with self.repository.connect() as conn:
            conn.execute(
                """
                UPDATE ports
                SET upstream_host = ?, upstream_port = ?
                WHERE upstream_host != ? OR upstream_port != ?
                """,
                (
                    DEFAULT_UPSTREAM_HOST,
                    DEFAULT_UPSTREAM_PORT,
                    DEFAULT_UPSTREAM_HOST,
                    DEFAULT_UPSTREAM_PORT,
                ),
            )
            conn.commit()

    def seed_defaults(self):
        if not SEED_LISTEN_PORT:
            return
        listen_port = parse_port(SEED_LISTEN_PORT, "默认监听端口")
        with self.repository.connect() as conn:
            exists = conn.execute("SELECT COUNT(*) FROM ports").fetchone()[0]
            if exists:
                return
            now = utc_iso_now()
            conn.execute(
                """
                INSERT INTO ports (
                    listen_port, upstream_host, upstream_port, tenant_token, subscription_token,
                    tenant_username, tenant_password,
                    expires_at, enabled, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 1, ?, ?, ?)
                """,
                (
                    listen_port,
                    DEFAULT_UPSTREAM_HOST,
                    DEFAULT_UPSTREAM_PORT,
                    self.generate_unique_port_token(conn, "tenant_token"),
                    self.generate_unique_port_token(conn, "subscription_token"),
                    self.generate_unique_tenant_username(conn),
                    generate_tenant_password(),
                    "默认初始化端口",
                    now,
                    now,
                ),
            )

    def query_ports(self):
        today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
        with self.repository.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    p.*,
                    COALESCE(t.total_connections, 0) AS total_connections,
                    COALESCE(t.total_bytes_sent, 0) AS total_bytes_sent,
                    COALESCE(t.total_bytes_received, 0) AS total_bytes_received,
                    t.last_seen AS last_seen,
                    pr.is_reachable AS probe_is_reachable,
                    pr.checked_at AS probe_checked_at,
                    pr.failure_reason AS probe_failure_reason,
                    COALESCE(d.total_connections, 0) AS today_connections,
                    COALESCE(d.total_bytes_sent, 0) AS today_bytes_sent,
                    COALESCE(d.total_bytes_received, 0) AS today_bytes_received
                FROM ports p
                LEFT JOIN traffic_totals t ON t.listen_port = p.listen_port
                LEFT JOIN upstream_probes pr ON pr.listen_port = p.listen_port
                LEFT JOIN traffic_daily d ON d.listen_port = p.listen_port AND d.stat_date = ?
                ORDER BY p.listen_port ASC
                """,
                (today,),
            ).fetchall()

        return [self.serialize_port_row(row) for row in rows]

    def serialize_port_row(self, row):
        item = dict(row)
        item["expires_at_display"] = format_display_time(item["expires_at"])
        item["expires_at_input"] = format_input_time(item["expires_at"])
        item["last_seen_display"] = format_display_time(item["last_seen"]) if item["last_seen"] else "暂无"
        item["probe_checked_at_display"] = (
            format_display_time(item["probe_checked_at"]) if item["probe_checked_at"] else "暂无"
        )
        item["probe_status"] = "unknown"
        item["probe_status_label"] = "未检测"
        item["probe_failure_reason"] = item["probe_failure_reason"] or ""
        if item["probe_is_reachable"] is not None:
            if int(item["probe_is_reachable"]):
                item["probe_status"] = "healthy"
                item["probe_status_label"] = "端口可达"
            else:
                item["probe_status"] = "unhealthy"
                item["probe_status_label"] = "端口不可达"
        item["traffic_usage_bytes"] = int(item["total_bytes_sent"]) + int(item["total_bytes_received"])
        item["traffic_limit_display"] = (
            human_bytes(item["traffic_limit_bytes"]) if item["traffic_limit_bytes"] is not None else "无限制"
        )
        item["traffic_limit_input"] = (
            human_bytes(item["traffic_limit_bytes"]) if item["traffic_limit_bytes"] is not None else ""
        )
        item["traffic_used_display"] = human_bytes(item["traffic_usage_bytes"])
        if item["traffic_limit_bytes"] is None:
            item["traffic_remaining_display"] = "无限制"
        else:
            item["traffic_remaining_display"] = human_bytes(
                max(int(item["traffic_limit_bytes"]) - item["traffic_usage_bytes"], 0)
            )
        status = status_payload(
            bool(item["enabled"]),
            item["expires_at"],
            item["traffic_limit_bytes"],
            item["traffic_usage_bytes"],
        )
        item["status"] = status["code"]
        item["status_label"] = status["label"]
        return item

    def get_subscription_token(self):
        with self.write_lock:
            with self.repository.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                token = self.ensure_subscription_token_in_tx(conn)
                conn.commit()
                return token

    def rotate_subscription_token(self):
        with self.write_lock:
            with self.repository.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                token = generate_subscription_token()
                self.repository.set_state(conn, "subscription_token", token)
                conn.commit()
                return token

    def get_port_subscription_record(self, listen_port):
        with self.repository.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    p.id,
                    p.listen_port,
                    p.note,
                    p.enabled,
                    p.expires_at,
                    p.traffic_limit_bytes,
                    COALESCE(t.total_bytes_sent, 0) AS total_bytes_sent,
                    COALESCE(t.total_bytes_received, 0) AS total_bytes_received
                FROM ports p
                LEFT JOIN traffic_totals t ON t.listen_port = p.listen_port
                WHERE p.listen_port = ?
                LIMIT 1
                """,
                (listen_port,),
            ).fetchone()

        if row is None:
            return None

        item = dict(row)
        item["traffic_usage_bytes"] = int(item["total_bytes_sent"]) + int(item["total_bytes_received"])
        status = status_payload(
            bool(item["enabled"]),
            item["expires_at"],
            item["traffic_limit_bytes"],
            item["traffic_usage_bytes"],
        )
        item["status"] = status["code"]
        item["status_label"] = status["label"]
        return item

    def query_summary(self, ports):
        summary = {
            "total_ports": len(ports),
            "active_ports": 0,
            "expired_ports": 0,
            "quota_ports": 0,
            "disabled_ports": 0,
            "total_connections": 0,
            "total_bytes_sent": 0,
            "total_bytes_received": 0,
        }
        for port in ports:
            summary["total_connections"] += port["total_connections"]
            summary["total_bytes_sent"] += port["total_bytes_sent"]
            summary["total_bytes_received"] += port["total_bytes_received"]
            if port["status"] == "active":
                summary["active_ports"] += 1
            elif port["status"] == "expired":
                summary["expired_ports"] += 1
            elif port["status"] == "quota":
                summary["quota_ports"] += 1
            else:
                summary["disabled_ports"] += 1
        return summary

    def validate_port_payload(self, form):
        return {
            "listen_port": parse_port(form.get("listen_port"), "监听端口"),
            "upstream_host": DEFAULT_UPSTREAM_HOST,
            "upstream_port": DEFAULT_UPSTREAM_PORT,
            "expires_at": parse_expiry(form.get("expires_at")),
            "traffic_limit_bytes": parse_data_size(form.get("traffic_limit"), "流量上限"),
            "note": parse_note(form.get("note")),
        }

    def create_port(self, payload):
        def operation(conn):
            now = utc_iso_now()
            conn.execute(
                """
                INSERT INTO ports (
                    listen_port, upstream_host, upstream_port, tenant_token, subscription_token,
                    tenant_username, tenant_password,
                    expires_at, traffic_limit_bytes, enabled, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    payload["listen_port"],
                    payload["upstream_host"],
                    payload["upstream_port"],
                    self.generate_unique_port_token(conn, "tenant_token"),
                    self.generate_unique_port_token(conn, "subscription_token"),
                    self.generate_unique_tenant_username(conn),
                    generate_tenant_password(),
                    payload["expires_at"],
                    payload["traffic_limit_bytes"],
                    payload["note"],
                    now,
                    now,
                ),
            )
            return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

        return self.renderer.apply_mutation(operation)

    def update_port(self, port_id, payload):
        def operation(conn):
            now = utc_iso_now()
            existing = conn.execute("SELECT id FROM ports WHERE id = ?", (port_id,)).fetchone()
            if existing is None:
                raise ValidationError("端口记录不存在。")
            conn.execute(
                """
                UPDATE ports
                SET listen_port = ?, upstream_host = ?, upstream_port = ?, expires_at = ?, traffic_limit_bytes = ?, note = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    payload["listen_port"],
                    payload["upstream_host"],
                    payload["upstream_port"],
                    payload["expires_at"],
                    payload["traffic_limit_bytes"],
                    payload["note"],
                    now,
                    port_id,
                ),
            )

        self.renderer.apply_mutation(operation)

    def toggle_port(self, port_id):
        def operation(conn):
            row = conn.execute(
                "SELECT id, listen_port, enabled, expires_at, traffic_limit_bytes FROM ports WHERE id = ?",
                (port_id,),
            ).fetchone()
            if row is None:
                raise ValidationError("端口记录不存在。")
            next_enabled = 0 if row["enabled"] else 1
            if next_enabled and row["expires_at"]:
                expires_at = datetime.fromisoformat(row["expires_at"])
                if expires_at <= utc_now():
                    raise ValidationError("端口已过期，请先修改到期时间再启用。")
            if next_enabled and row["traffic_limit_bytes"] is not None:
                usage_bytes = self.get_port_usage_bytes(conn, row["listen_port"])
                if usage_bytes >= int(row["traffic_limit_bytes"]):
                    raise ValidationError("端口已达到流量上限，请先提高上限再启用。")
            conn.execute(
                "UPDATE ports SET enabled = ?, updated_at = ? WHERE id = ?",
                (next_enabled, utc_iso_now(), port_id),
            )

        self.renderer.apply_mutation(operation)

    def delete_port(self, port_id):
        def operation(conn):
            row = conn.execute(
                "SELECT listen_port, customer_id, service_subscription_id FROM ports WHERE id = ?",
                (port_id,),
            ).fetchone()
            if row is None:
                raise ValidationError("端口记录不存在。")
            if row["customer_id"] is not None or row["service_subscription_id"] is not None:
                raise ValidationError("商业化服务端口不能直接删除，请通过业务流程处理。")
            self.delete_port_in_tx(conn, port_id, row["listen_port"])

        self.renderer.apply_mutation(operation)

    def disable_expired_ports(self, reload_xray=True):
        return self.renderer.disable_auto_stopped_ports(reload_xray=reload_xray)

    def rotate_port_tenant_token(self, port_id):
        def operation(conn):
            row = conn.execute("SELECT id FROM ports WHERE id = ?", (port_id,)).fetchone()
            if row is None:
                raise ValidationError("端口记录不存在。")
            token = self.generate_unique_port_token(conn, "tenant_token")
            conn.execute(
                "UPDATE ports SET tenant_token = ?, updated_at = ? WHERE id = ?",
                (token, utc_iso_now(), port_id),
            )
            return token

        return self.repository.apply_state_update(operation)

    def rotate_port_subscription_token(self, port_id):
        def operation(conn):
            row = conn.execute("SELECT id FROM ports WHERE id = ?", (port_id,)).fetchone()
            if row is None:
                raise ValidationError("端口记录不存在。")
            token = self.generate_unique_port_token(conn, "subscription_token")
            conn.execute(
                "UPDATE ports SET subscription_token = ?, updated_at = ? WHERE id = ?",
                (token, utc_iso_now(), port_id),
            )
            return token

        return self.repository.apply_state_update(operation)

    def rotate_port_tenant_credentials(self, port_id):
        def operation(conn):
            row = conn.execute("SELECT id FROM ports WHERE id = ?", (port_id,)).fetchone()
            if row is None:
                raise ValidationError("端口记录不存在。")
            username = self.generate_unique_tenant_username(conn)
            password = generate_tenant_password()
            conn.execute(
                "UPDATE ports SET tenant_username = ?, tenant_password = ?, updated_at = ? WHERE id = ?",
                (username, password, utc_iso_now(), port_id),
            )
            return {"tenant_username": username, "tenant_password": password}

        return self.repository.apply_state_update(operation)

    def get_port_usage_bytes(self, conn, listen_port):
        row = conn.execute(
            """
            SELECT
                COALESCE(total_bytes_sent, 0) + COALESCE(total_bytes_received, 0) AS usage_bytes
            FROM traffic_totals
            WHERE listen_port = ?
            """,
            (listen_port,),
        ).fetchone()
        if row is None:
            return 0
        return int(row["usage_bytes"])

    def delete_port_in_tx(self, conn, port_id, listen_port):
        conn.execute("DELETE FROM ports WHERE id = ?", (port_id,))
        conn.execute("DELETE FROM traffic_totals WHERE listen_port = ?", (listen_port,))
        conn.execute("DELETE FROM traffic_daily WHERE listen_port = ?", (listen_port,))
        conn.execute("DELETE FROM upstream_probes WHERE listen_port = ?", (listen_port,))
        conn.execute("DELETE FROM upstream_probe_history WHERE listen_port = ?", (listen_port,))

    def cleanup_expired_ports_in_tx(self, conn):
        rows = conn.execute(
            """
            SELECT id, listen_port
            FROM ports
            WHERE expires_at IS NOT NULL
              AND expires_at <= ?
              AND COALESCE(customer_id, 0) = 0
              AND COALESCE(service_subscription_id, 0) = 0
            """,
            (utc_iso_now(),),
        ).fetchall()
        for row in rows:
            self.delete_port_in_tx(conn, row["id"], row["listen_port"])
        return len(rows)

    def get_port_by_tenant_token(self, tenant_token):
        today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
        with self.repository.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    p.*,
                    COALESCE(t.total_connections, 0) AS total_connections,
                    COALESCE(t.total_bytes_sent, 0) AS total_bytes_sent,
                    COALESCE(t.total_bytes_received, 0) AS total_bytes_received,
                    t.last_seen AS last_seen,
                    pr.is_reachable AS probe_is_reachable,
                    pr.checked_at AS probe_checked_at,
                    pr.failure_reason AS probe_failure_reason,
                    COALESCE(d.total_connections, 0) AS today_connections,
                    COALESCE(d.total_bytes_sent, 0) AS today_bytes_sent,
                    COALESCE(d.total_bytes_received, 0) AS today_bytes_received
                FROM ports p
                LEFT JOIN traffic_totals t ON t.listen_port = p.listen_port
                LEFT JOIN upstream_probes pr ON pr.listen_port = p.listen_port
                LEFT JOIN traffic_daily d ON d.listen_port = p.listen_port AND d.stat_date = ?
                WHERE p.tenant_token = ?
                LIMIT 1
                """,
                (today, tenant_token),
            ).fetchone()

        if row is None:
            return None
        return self.serialize_port_row(row)

    def get_port_by_tenant_username(self, tenant_username):
        today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
        with self.repository.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    p.*,
                    COALESCE(t.total_connections, 0) AS total_connections,
                    COALESCE(t.total_bytes_sent, 0) AS total_bytes_sent,
                    COALESCE(t.total_bytes_received, 0) AS total_bytes_received,
                    t.last_seen AS last_seen,
                    pr.is_reachable AS probe_is_reachable,
                    pr.checked_at AS probe_checked_at,
                    pr.failure_reason AS probe_failure_reason,
                    COALESCE(d.total_connections, 0) AS today_connections,
                    COALESCE(d.total_bytes_sent, 0) AS today_bytes_sent,
                    COALESCE(d.total_bytes_received, 0) AS today_bytes_received
                FROM ports p
                LEFT JOIN traffic_totals t ON t.listen_port = p.listen_port
                LEFT JOIN upstream_probes pr ON pr.listen_port = p.listen_port
                LEFT JOIN traffic_daily d ON d.listen_port = p.listen_port AND d.stat_date = ?
                WHERE p.tenant_username = ?
                LIMIT 1
                """,
                (today, str(tenant_username or "").strip()),
            ).fetchone()

        if row is None:
            return None
        return self.serialize_port_row(row)

    def get_port_subscription_record_by_token(self, subscription_token):
        with self.repository.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    p.id,
                    p.listen_port,
                    p.note,
                    p.enabled,
                    p.expires_at,
                    p.traffic_limit_bytes,
                    COALESCE(t.total_bytes_sent, 0) AS total_bytes_sent,
                    COALESCE(t.total_bytes_received, 0) AS total_bytes_received
                FROM ports p
                LEFT JOIN traffic_totals t ON t.listen_port = p.listen_port
                WHERE p.subscription_token = ?
                LIMIT 1
                """,
                (subscription_token,),
            ).fetchone()

        if row is None:
            return None

        item = dict(row)
        item["traffic_usage_bytes"] = int(item["total_bytes_sent"]) + int(item["total_bytes_received"])
        status = status_payload(
            bool(item["enabled"]),
            item["expires_at"],
            item["traffic_limit_bytes"],
            item["traffic_usage_bytes"],
        )
        item["status"] = status["code"]
        item["status_label"] = status["label"]
        return item
