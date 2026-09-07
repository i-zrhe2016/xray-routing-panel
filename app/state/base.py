import json
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlencode

from ..config import (
    AI_NODE_CONFIG_OUT,
    AI_NODE_PROBE_HOST,
    CONTROL_PLANE_BACKUP_UPSTREAM_URL,
    CONTROL_PLANE_BACKUP_XRAY_ENABLED,
    DATAPLANE_CONFIG_PATH,
    DATAPLANE_LOCAL_BIN,
    DATAPLANE_SSH_TARGET,
    DB_PATH,
    DEFAULT_UPSTREAM_HOST,
    DEFAULT_UPSTREAM_PORT,
    MAINTENANCE_INTERVAL,
    PROBE_ENABLED,
    PROBE_INTERVAL,
    SEED_LISTEN_PORT,
    XRAY_CLIENT_CONFIG_PATH,
    XRAY_CONFIG_PATH,
    XRAY_ENV_FILE_PATH,
    XRAY_PANEL_PORTS_PATH,
    XRAY_STATS_QUERY_TIMEOUT,
)
from ..errors import ValidationError
from ..helpers import (
    generate_access_token,
    generate_subscription_token,
    generate_tenant_password,
    generate_tenant_username,
    localize_time,
    parse_port,
    port_is_expired,
    utc_iso_now,
    utc_now,
)
from ..observability.logging import emit_business_event
from ..xray.envfile import load_env_file


class CoreService:
    def __init__(self, panel):
        self._panel = panel
    def data_plane_config_path(self):
        explicit = DATAPLANE_CONFIG_PATH.strip()
        if explicit:
            return explicit
        if DATAPLANE_SSH_TARGET:
            return str(XRAY_CONFIG_PATH)
        if DATAPLANE_LOCAL_BIN:
            return str(XRAY_CONFIG_PATH)
        return "/etc/xray/config.json"
    def data_plane_ai_report_source_path(self):
        return XRAY_ENV_FILE_PATH.parent / "reports" / "hourly-domains" / "latest.json"
    def data_plane_status(self):
        return self._panel.data_plane.status_summary()
    def connect(self):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn
    def init_db(self):
        with self._panel.connect() as conn:
            conn.executescript(
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
                );

                CREATE TABLE IF NOT EXISTS traffic_totals (
                    listen_port INTEGER PRIMARY KEY,
                    total_connections INTEGER NOT NULL DEFAULT 0,
                    total_bytes_sent INTEGER NOT NULL DEFAULT 0,
                    total_bytes_received INTEGER NOT NULL DEFAULT 0,
                    last_seen TEXT
                );

                CREATE TABLE IF NOT EXISTS traffic_daily (
                    listen_port INTEGER NOT NULL,
                    stat_date TEXT NOT NULL,
                    total_connections INTEGER NOT NULL DEFAULT 0,
                    total_bytes_sent INTEGER NOT NULL DEFAULT 0,
                    total_bytes_received INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (listen_port, stat_date)
                );

                CREATE TABLE IF NOT EXISTS app_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS upstream_probes (
                    listen_port INTEGER PRIMARY KEY,
                    is_reachable INTEGER NOT NULL,
                    checked_at TEXT NOT NULL,
                    failure_reason TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS upstream_probe_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    listen_port INTEGER NOT NULL,
                    is_reachable INTEGER NOT NULL,
                    checked_at TEXT NOT NULL,
                    failure_reason TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS ai_domains (
                    domain TEXT PRIMARY KEY,
                    classification TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    first_seen TEXT,
                    last_seen TEXT,
                    total_hits INTEGER NOT NULL DEFAULT 0,
                    last_protocols TEXT NOT NULL DEFAULT '[]',
                    last_report_window_start TEXT,
                    last_report_window_end TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ai_domain_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    hits INTEGER NOT NULL,
                    classification TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    protocols TEXT NOT NULL DEFAULT '[]',
                    first_seen TEXT,
                    last_seen TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_domain_observations_window
                ON ai_domain_observations(domain, window_start, window_end);

                CREATE INDEX IF NOT EXISTS idx_ai_domain_observations_domain
                ON ai_domain_observations(domain);

                CREATE TABLE IF NOT EXISTS dns_failover_state (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    current_target TEXT NOT NULL DEFAULT 'primary',
                    current_record_content TEXT NOT NULL DEFAULT '',
                    current_record_ttl INTEGER,
                    current_record_proxied INTEGER,
                    last_probe_status TEXT NOT NULL DEFAULT 'unknown',
                    last_probe_checked_at TEXT,
                    last_probe_error TEXT NOT NULL DEFAULT '',
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    consecutive_successes INTEGER NOT NULL DEFAULT 0,
                    last_switch_at TEXT,
                    last_switch_reason TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS dns_failover_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    event_status TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    record_content TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                """
            )
            self._panel.ensure_port_schema(conn)
            self._panel.ensure_dns_failover_schema(conn)
            self._panel.ensure_commerce_schema(conn)
    def ensure_port_schema(self, conn):
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
        self._panel.cleanup_expired_ports_in_tx(conn)
        self._panel.ensure_port_tokens_in_tx(conn)
        self._panel.ensure_port_credentials_in_tx(conn)
    def ensure_dns_failover_schema(self, conn):
        conn.execute(
            """
            INSERT INTO dns_failover_state (singleton_id)
            VALUES (1)
            ON CONFLICT(singleton_id) DO NOTHING
            """
        )
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
                updates["tenant_token"] = self._panel.generate_unique_port_token(conn, "tenant_token")
            if not str(row["subscription_token"] or "").strip():
                updates["subscription_token"] = self._panel.generate_unique_port_token(conn, "subscription_token")
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
                updates["tenant_username"] = self._panel.generate_unique_tenant_username(conn)
            if not str(row["tenant_password"] or "").strip():
                updates["tenant_password"] = generate_tenant_password()
            if not updates:
                continue
            assignments = ", ".join(f"{column} = ?" for column in updates)
            values = list(updates.values()) + [row["id"]]
            conn.execute(f"UPDATE ports SET {assignments} WHERE id = ?", values)
    def ensure_subscription_token_in_tx(self, conn):
        token = str(self._panel.get_state(conn, "subscription_token", "") or "").strip()
        if token:
            return token
        token = generate_subscription_token()
        self._panel.set_state(conn, "subscription_token", token)
        return token
    def normalize_upstream_targets(self):
        with self._panel.connect() as conn:
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
        with self._panel.connect() as conn:
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
                    self._panel.generate_unique_port_token(conn, "tenant_token"),
                    self._panel.generate_unique_port_token(conn, "subscription_token"),
                    self._panel.generate_unique_tenant_username(conn),
                    generate_tenant_password(),
                    "默认初始化端口",
                    now,
                    now,
                ),
            )
    def bootstrap(self):
        self._panel.init_db()
        self._panel.seed_defaults()
        self._panel.normalize_upstream_targets()
        with self._panel.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._panel.cleanup_expired_ports_in_tx(conn)
            self._panel.ensure_subscription_token_in_tx(conn)
            self._panel.ensure_port_tokens_in_tx(conn)
            self._panel.ensure_port_credentials_in_tx(conn)
            conn.commit()
        self._panel.sync_traffic_state()
        self._panel.disable_auto_stopped_ports(reload_xray=False)
        self._panel.write_current_config()
        try:
            self._panel.refresh_dns_failover_record_snapshot()
        except Exception:
            pass
    def get_state(self, conn, key, default=None):
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return row["value"]
    def set_state(self, conn, key, value):
        conn.execute(
            """
            INSERT INTO app_state (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )
    def format_optional_display_time(self, value, default="暂无"):
        localized = localize_time(value)
        if localized is None:
            return default
        return localized.strftime("%Y-%m-%d %H:%M:%S")
    def disable_auto_stopped_ports(self, reload_xray=True):
        with self._panel.write_lock:
            with self._panel.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                changed = self._panel.disable_auto_stopped_ports_in_tx(conn)
                if changed:
                    self._panel.persist_and_reload(conn, reload_xray=reload_xray)
                else:
                    conn.commit()
                return changed
    def apply_mutation(self, operation):
        with self._panel.write_lock:
            self._panel.sync_traffic_state_locked()
            with self._panel.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    result = operation(conn)
                    self._panel.disable_auto_stopped_ports_in_tx(conn)
                    self._panel.persist_and_reload(conn, reload_xray=True)
                    return result
                except Exception:
                    conn.rollback()
                    raise
    def apply_state_update(self, operation):
        with self._panel.write_lock:
            with self._panel.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    result = operation(conn)
                    conn.commit()
                    return result
                except Exception:
                    conn.rollback()
                    raise
    def disable_auto_stopped_ports_in_tx(self, conn):
        now_dt = utc_now()
        now_text = now_dt.isoformat(timespec="seconds")
        cleaned = self._panel.cleanup_expired_ports_in_tx(conn)
        rows = conn.execute(
            """
            SELECT
                p.id,
                p.listen_port,
                p.expires_at,
                p.traffic_limit_bytes,
                COALESCE(t.total_bytes_sent, 0) AS total_bytes_sent,
                COALESCE(t.total_bytes_received, 0) AS total_bytes_received
            FROM ports p
            LEFT JOIN traffic_totals t ON t.listen_port = p.listen_port
            WHERE p.enabled = 1
            """,
        ).fetchall()
        changed = 0
        for row in rows:
            usage_bytes = int(row["total_bytes_sent"]) + int(row["total_bytes_received"])
            quota_reached = row["traffic_limit_bytes"] is not None and usage_bytes >= int(row["traffic_limit_bytes"])
            expired = port_is_expired(row["expires_at"], now_dt)
            if expired or quota_reached:
                conn.execute(
                    "UPDATE ports SET enabled = 0, updated_at = ? WHERE id = ?",
                    (now_text, row["id"]),
                )
                changed += 1
        return changed + cleaned
    def persist_and_reload(self, conn, reload_xray):
        previous_panel_ports = (
            XRAY_PANEL_PORTS_PATH.read_text(encoding="utf-8") if XRAY_PANEL_PORTS_PATH.exists() else None
        )
        previous_config = XRAY_CONFIG_PATH.read_text(encoding="utf-8") if XRAY_CONFIG_PATH.exists() else None
        subscription_paths = [XRAY_CLIENT_CONFIG_PATH, XRAY_CONFIG_PATH.parent / "client-share.txt"]
        previous_subscriptions = {
            path: path.read_bytes() if path.exists() else None for path in subscription_paths
        }
        backup_config_path = XRAY_CONFIG_PATH.parent / "config-backup.json"
        previous_backup_config = (
            backup_config_path.read_text(encoding="utf-8") if backup_config_path.exists() else None
        )
        panel_ports_payload = self._panel.render_panel_ports_payload(conn)
        backup_restart_attempted = False
        data_plane_restart_attempted = False
        try:
            self._panel.write_json_file(XRAY_PANEL_PORTS_PATH, panel_ports_payload)
            self._panel.render_xray_config()
            self._panel.xray_config_test()
            if CONTROL_PLANE_BACKUP_XRAY_ENABLED:
                backup_restart_attempted = True
                if not self._panel.restart_backup_xray():
                    raise RuntimeError("控制面备用 Xray 重载失败，端口变更未提交。")
            if reload_xray:
                data_plane_restart_attempted = True
                if not self._panel.restart_data_plane():
                    raise RuntimeError("数据面重载失败，端口变更未提交。")
            conn.commit()
        except Exception:
            conn.rollback()
            for path, content in previous_subscriptions.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content)
            if previous_panel_ports is None:
                XRAY_PANEL_PORTS_PATH.unlink(missing_ok=True)
            else:
                XRAY_PANEL_PORTS_PATH.write_text(previous_panel_ports, encoding="utf-8")
            if previous_config is None:
                XRAY_CONFIG_PATH.unlink(missing_ok=True)
            else:
                XRAY_CONFIG_PATH.write_text(previous_config, encoding="utf-8")
            if previous_backup_config is None:
                backup_config_path.unlink(missing_ok=True)
            else:
                backup_config_path.write_text(previous_backup_config, encoding="utf-8")
            if backup_restart_attempted:
                try:
                    if not self._panel.restart_backup_xray():
                        emit_business_event(
                            "node.backup.rollback_failed",
                            result="failure",
                            actor_type="system",
                            resource_type="node",
                            resource_id="control_plane_backup",
                        )
                except Exception as exc:
                    emit_business_event(
                        "node.backup.rollback_failed",
                        result="failure",
                        actor_type="system",
                        resource_type="node",
                        resource_id="control_plane_backup",
                        exc=exc,
                    )
            try:
                if self._panel.data_plane.supports_sync():
                    self._panel.data_plane.sync_generated_files(validate_config=True)
                # A timeout can occur after the node has already loaded the new
                # config. Restore its running state as well as the files.
                if data_plane_restart_attempted and not self._panel.restart_data_plane():
                    raise RuntimeError("数据面回滚重载失败。")
            except Exception as exc:  # noqa: BLE001 - preserve the original apply/commit failure
                emit_business_event(
                    "node.data_plane.rollback_failed",
                    result="failure",
                    actor_type="system",
                    resource_type="node",
                    resource_id="data_plane",
                    exc=exc,
                )
            raise
    def render_panel_ports_payload(self, conn):
        rows = conn.execute(
            """
            SELECT
                p.listen_port
            FROM ports
            AS p
            LEFT JOIN traffic_totals t ON t.listen_port = p.listen_port
            WHERE p.enabled = 1
              AND (p.expires_at IS NULL OR p.expires_at > ?)
              AND (
                    p.traffic_limit_bytes IS NULL
                    OR COALESCE(t.total_bytes_sent, 0) + COALESCE(t.total_bytes_received, 0) < p.traffic_limit_bytes
                  )
            ORDER BY p.listen_port ASC
            """,
            (utc_iso_now(),),
        ).fetchall()
        return {
            "ports": [int(row["listen_port"]) for row in rows],
        }
    def write_current_config(self):
        with self._panel.connect() as conn:
            self._panel.write_json_file(XRAY_PANEL_PORTS_PATH, self._panel.render_panel_ports_payload(conn))
        self._panel.render_xray_config()
        try:
            changed_paths = self._panel.xray_config_test() or []
        except RuntimeError as exc:
            if not self._panel.data_plane.is_remote:
                raise
            emit_business_event(
                "node.data_plane.diagnosed",
                result="failure",
                actor_type="system",
                resource_type="node",
                resource_id="data_plane",
                exc=exc,
            )
            return

        config_path = str(self._panel.data_plane.config.config_path or "")
        if config_path not in changed_paths or not self._panel.data_plane.supports_restart():
            return
        try:
            if not self._panel.restart_data_plane():
                raise RuntimeError("数据面配置已更新，但 Xray 未能重载。")
            emit_business_event(
                "node.data_plane.restarted",
                actor_type="system",
                resource_type="node",
                resource_id="data_plane",
                metadata={"reason": "startup_config_changed"},
            )
        except RuntimeError as exc:
            emit_business_event(
                "node.data_plane.restarted",
                result="failure",
                actor_type="system",
                resource_type="node",
                resource_id="data_plane",
                error_code="startup_reload_failed",
                exc=exc,
            )
    def write_json_file(self, path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    def render_xray_config(self):
        try:
            self._panel.sync_data_plane_dynamic_routing()
        except RuntimeError as exc:
            if not self._panel.data_plane.is_remote:
                raise
            emit_business_event(
                "ai_routing.changed",
                result="failure",
                actor_type="system",
                error_code="sync_failed",
                exc=exc,
            )
        share_path = XRAY_CONFIG_PATH.parent / "client-share.txt"
        command = [
            sys.executable,
            "-m",
            "app.xray.render_config",
            "--env-file",
            str(XRAY_ENV_FILE_PATH),
            "--config-out",
            str(XRAY_CONFIG_PATH),
            "--client-out",
            str(XRAY_CLIENT_CONFIG_PATH),
            "--share-out",
            str(share_path),
            "--panel-ports-file",
            str(XRAY_PANEL_PORTS_PATH),
        ]
        # When the control-plane backup Xray is enabled, also render
        # config-backup.json. The backup operates in dual mode:
        # - relay mode: forwards to CONTROL_PLANE_BACKUP_UPSTREAM_URL (or the
        #   auto-derived AI node URL when AI node is reachable)
        # - direct mode: freedom direct exit (when AI node is not reachable and
        #   no explicit upstream URL is configured)
        if CONTROL_PLANE_BACKUP_XRAY_ENABLED:
            backup_config_path = XRAY_CONFIG_PATH.parent / "config-backup.json"
            backup_upstream_url = self._panel.resolve_backup_upstream_url()
            command += [
                "--backup-config-out",
                str(backup_config_path),
            ]
            if backup_upstream_url:
                command += [
                    "--backup-upstream-url",
                    backup_upstream_url,
                ]
        # When the AI node is managed via SSH, also render config-ai-node.json
        # so the control plane can push it to the remote AI node.
        if any(node.supports_sync() for node in self._panel.ai_nodes.values()):
            command += [
                "--ai-node-config-out",
                str(AI_NODE_CONFIG_OUT),
            ]
        self._panel.run_command(command, "Xray 配置渲染失败")
    def sync_data_plane_dynamic_routing(self):
        if not self._panel.data_plane.supports_dynamic_routing_pull():
            return False
        return self._panel.data_plane.sync_dynamic_routing_from_remote()
    def sync_data_plane_artifacts(self):
        if not self._panel.data_plane.supports_sync():
            return []
        return self._panel.data_plane.sync_generated_files(validate_config=True)
    def xray_config_test(self):
        if self._panel.data_plane.supports_sync():
            return self._panel.sync_data_plane_artifacts()
        self._panel.data_plane.test_config()
        return []
    def restart_data_plane(self):
        return self._panel.data_plane.restart()
    def ai_nodes_status(self):
        statuses = []
        for node_id, controller in self._panel.ai_nodes.items():
            status = controller.status_summary()
            status["node_id"] = node_id
            statuses.append(status)
        return statuses
    def ai_node_status(self, nodes=None):
        nodes = self._panel.ai_nodes_status() if nodes is None else nodes
        if not nodes:
            return {
                "role": "ai_node",
                "label": "AI 节点",
                "configured": False,
                "reachable": False,
                "xray_running": None,
                "management_target": "",
                "supports_restart": False,
                "supports_sync": False,
                "last_error": "",
                "nodes": [],
                "node_count": 0,
                "all_reachable": False,
                "any_reachable": False,
            }
        if len(nodes) == 1:
            status = dict(nodes[0])
            status.update(
                {
                    "nodes": nodes,
                    "node_count": 1,
                    "all_reachable": bool(status.get("reachable")),
                    "any_reachable": bool(status.get("reachable")),
                }
            )
            return status

        reachable = [bool(node.get("reachable")) for node in nodes]
        running = [node.get("xray_running") for node in nodes]
        if all(value is True for value in running):
            aggregate_running = True
        elif any(value is True for value in running):
            aggregate_running = None
        else:
            aggregate_running = False
        errors = [
            f"{node.get('label')}: {node.get('last_error')}"
            for node in nodes
            if node.get("last_error")
        ]
        return {
            "role": "ai_node",
            "label": f"AI 节点（{len(nodes)}）",
            "configured": all(bool(node.get("configured")) for node in nodes),
            "reachable": any(reachable),
            "xray_running": aggregate_running,
            "management_target": "、".join(str(node.get("label") or node.get("node_id")) for node in nodes),
            "api_server": "",
            "config_path": "",
            "access_log_path": "",
            "supports_sync": all(bool(node.get("supports_sync")) for node in nodes),
            "supports_restart": any(bool(node.get("supports_restart")) for node in nodes),
            "last_error": "；".join(errors),
            "nodes": nodes,
            "node_count": len(nodes),
            "all_reachable": all(reachable),
            "any_reachable": any(reachable),
        }
    def ai_node_running(self):
        return any(
            self._node_running(controller)
            for controller in self._panel.ai_nodes.values()
        )
    @staticmethod
    def _node_running(controller):
        try:
            return bool(controller.is_running())
        except (OSError, RuntimeError, ValueError):
            return False
    def sync_ai_node_config(self):
        uploaded = []
        for controller in self._panel.ai_nodes.values():
            if controller.supports_sync():
                uploaded.extend(controller.sync_generated_files(validate_config=True))
        return uploaded
    def restart_ai_node_or_raise(self, node_id=None):
        if not self._panel.ai_nodes:
            raise ValidationError("AI 节点未配置（AI_NODE_SSH_TARGETS 为空）。")
        controller = self._panel.ai_nodes.get(node_id) if node_id else self._panel.ai_node
        if controller is None:
            raise ValidationError(f"AI 节点不存在：{node_id}。")
        if not controller.supports_restart():
            raise ValidationError("AI 节点未配置可用的重启方式。")
        restarted = controller.restart()
        if not restarted:
            raise ValidationError("AI 节点不可重启。")
        status = controller.status_summary()
        status["node_id"] = node_id or next(iter(self._panel.ai_nodes))
        return status
    def data_plane_configured(self):
        return self._panel.data_plane.is_configured()
    def data_plane_running(self):
        return self._panel.data_plane.is_running()
    def ai_node_reachable(self):
        return self._panel.ai_node_running()
    def resolve_backup_upstream_url(self):
        """Determine the backup Xray's upstream URL based on AI node reachability.

        Priority:
        1. Explicit CONTROL_PLANE_BACKUP_UPSTREAM_URL env var (always wins)
        2. When AI node is managed and reachable, auto-derive a vless:// URL
           from the xray .env REALITY params + AI node's public host:port
        3. Empty string → backup uses freedom direct (dual-mode fallback)
        """
        if CONTROL_PLANE_BACKUP_UPSTREAM_URL:
            return CONTROL_PLANE_BACKUP_UPSTREAM_URL
        if not self._panel.ai_nodes:
            return ""
        if not self._panel.ai_node_running():
            return ""
        return self._panel.derive_ai_node_share_url()
    def derive_ai_node_share_url(self):
        """Build a vless:// share URL targeting the AI node from xray .env values."""
        try:
            values = load_env_file(XRAY_ENV_FILE_PATH)
        except Exception:
            return ""
        required = [
            "XRAY_CLIENT_UUID",
            "XRAY_FLOW",
            "XRAY_REALITY_PUBLIC_KEY",
            "XRAY_REALITY_SHORT_ID",
            "XRAY_SERVER_NAME",
            "XRAY_FINGERPRINT",
        ]
        if any(not values.get(key) for key in required):
            return ""
        host = AI_NODE_PROBE_HOST
        if not host:
            return ""
        port = values.get("XRAY_PUBLIC_PORT") or values.get("XRAY_LISTEN_PORT", "443")
        params = urlencode({
            "encryption": "none",
            "flow": values["XRAY_FLOW"],
            "security": "reality",
            "sni": values["XRAY_SERVER_NAME"],
            "fp": values["XRAY_FINGERPRINT"],
            "pbk": values["XRAY_REALITY_PUBLIC_KEY"],
            "sid": values["XRAY_REALITY_SHORT_ID"],
            "type": "tcp",
            "headerType": "none",
        })
        return f"vless://{values['XRAY_CLIENT_UUID']}@{host}:{port}?{params}#ai-node"
    def backup_xray_mode(self):
        """Return the current backup Xray mode: 'relay', 'direct', or 'disabled'."""
        if not CONTROL_PLANE_BACKUP_XRAY_ENABLED:
            return "disabled"
        url = self._panel.resolve_backup_upstream_url()
        return "relay" if url else "direct"
    def sync_backup_xray_mode(self):
        """Re-render backup config and restart the backup container when the
        AI node reachability changed since the last render.

        Called from the maintenance loop; safe to no-op when the backup Xray
        is disabled or the AI node is not managed.
        """
        if not CONTROL_PLANE_BACKUP_XRAY_ENABLED:
            return False
        if not self._panel.ai_nodes:
            return False
        current_mode = self._panel.backup_xray_mode()
        previous_mode = getattr(self._panel, "_last_backup_mode", None)
        self._panel._last_backup_mode = current_mode
        if previous_mode is not None and previous_mode == current_mode:
            return False
        self._panel.render_xray_config()
        self._panel.restart_backup_xray()
        return True
    def restart_backup_xray(self):
        """Restart the local backup Xray container so it picks up the new config."""
        if not CONTROL_PLANE_BACKUP_XRAY_ENABLED:
            return False
        try:
            completed = subprocess.run(
                ["docker", "restart", "xray-reality-backup"],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            return completed.returncode == 0
        except Exception:
            return False
    def read_xray_traffic_stats(self):
        if not self._panel.data_plane_running():
            return {}
        try:
            completed = self._panel.data_plane.run_statsquery(
                XRAY_STATS_QUERY_TIMEOUT,
                ">>>panel-",
            )
            if completed is None:
                return {}
            payload = json.loads(completed.stdout or "{}")
        except (RuntimeError, json.JSONDecodeError):
            return {}

        counters = {}
        for item in payload.get("stat", []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            parts = name.split(">>>")
            if len(parts) != 4 or parts[0] not in {"inbound", "user"} or parts[2] != "traffic":
                continue
            tag = parts[1]
            prefix = "panel-user-" if parts[0] == "user" else "panel-"
            if not tag.startswith(prefix):
                continue
            try:
                listen_port = int(tag.removeprefix(prefix))
                value = int(item.get("value", 0) or 0)
            except (TypeError, ValueError):
                continue
            counter = counters.setdefault(
                listen_port,
                {
                    "bytes_sent": 0,
                    "bytes_received": 0,
                },
            )
            if parts[3] == "uplink":
                counter["bytes_received"] += value
            elif parts[3] == "downlink":
                counter["bytes_sent"] += value
        return counters
    def restart_data_plane_or_raise(self):
        if not self._panel.data_plane.is_configured():
            raise ValidationError("数据面未配置。")
        if not self._panel.data_plane.supports_restart():
            raise ValidationError("当前数据面未配置可用的重启方式。")
        restarted = self._panel.data_plane.restart()
        if not restarted:
            raise ValidationError("当前数据面不可重启。")
        return self._panel.data_plane.status_summary()
    def run_command(self, command, error_prefix, timeout=None):
        completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)
        if completed.returncode == 0:
            return completed
        detail = completed.stderr.strip() or completed.stdout.strip() or "未知错误"
        raise RuntimeError(f"{error_prefix}: {detail}")
    def maintenance_loop(self):
        last_probe_at = 0.0
        last_backup_mode_at = 0.0
        backup_mode_interval = max(PROBE_INTERVAL, 60)
        while not self._panel.stop_event.wait(MAINTENANCE_INTERVAL):
            try:
                self._panel.sync_traffic_state()
                self._panel.disable_auto_stopped_ports(reload_xray=True)
                now_monotonic = time.monotonic()
                if PROBE_ENABLED and now_monotonic - last_probe_at >= PROBE_INTERVAL:
                    self._panel.run_upstream_probes()
                    last_probe_at = now_monotonic
                if now_monotonic - last_backup_mode_at >= backup_mode_interval:
                    self._panel.sync_backup_xray_mode()
                    last_backup_mode_at = now_monotonic
            except Exception as exc:
                emit_business_event(
                    "maintenance.failed",
                    result="failure",
                    actor_type="system",
                    error_code="maintenance_exception",
                    exc=exc,
                )
                continue

    def dns_failover_loop(self):
        """Run DNS failover independently from data-plane maintenance tasks.

        Data-plane SSH, log synchronization, and stats collection may block or
        fail when the primary node is down. DNS failover must remain alive in
        exactly that situation, so it has its own loop and failure boundary.
        """
        while not self._panel.stop_event.is_set():
            try:
                if self._panel.dns_failover_manager.config.enabled:
                    self._panel.run_dns_failover_check()
            except Exception as exc:
                emit_business_event(
                    "dns_failover.checked",
                    result="failure",
                    actor_type="system",
                    error_code="check_failed",
                    exc=exc,
                )
            interval = max(1, self._panel.dns_failover_manager.config.interval)
            if self._panel.stop_event.wait(interval):
                return
    def stop(self):
        if self._panel.stop_event.is_set():
            return
        self._panel.stop_event.set()
        self._panel.sync_traffic_state()
