from datetime import datetime, timezone

from ..config import (
    LOCAL_TZ,
    XRAY_ACCESS_LOG_PATH,
)
from ..errors import ValidationError
from ..helpers import (
    utc_iso_now,
    utc_now,
)
from ._constants import XRAY_ACCESS_LOG_LINE_RE


class TrafficService:
    """Traffic synchronization using explicit storage, node and mutation ports."""

    def __init__(
        self,
        repository=None,
        node_controller=None,
        stats_reader=None,
        renderer=None,
        write_lock=None,
    ):
        self.repository = repository
        self.node_controller = node_controller
        self.stats_reader = stats_reader if stats_reader is not None else repository
        self.renderer = renderer if renderer is not None else repository
        self.write_lock = write_lock

    def ensure_traffic_schema(self, conn):
        conn.executescript(
            """
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
            """
        )

    def sync_traffic_state(self):
        with self.write_lock:
            return self.sync_traffic_state_locked()

    def sync_traffic_state_locked(self):
        with self.repository.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            log_updates = self.sync_xray_access_log_in_tx(conn)
            byte_updates = self.sync_xray_traffic_stats_in_tx(conn)
            conn.commit()
            return {
                "connection_updates": log_updates,
                "byte_updates": byte_updates,
            }

    def sync_xray_access_log_in_tx(self, conn):
        current_offset = int(self.repository.get_state(conn, "xray_access_log_offset", "0"))
        recorded_inode = self.repository.get_state(conn, "xray_access_log_inode", "")
        current_inode = ""
        new_offset = 0
        lines = []

        if self.node_controller.supports_logs():
            try:
                payload = self.node_controller.read_access_log_delta(recorded_inode, current_offset)
            except RuntimeError:
                return 0
            if not payload["exists"]:
                return 0
            current_inode = payload["inode"]
            new_offset = int(payload["offset"])
            lines = str(payload["data"]).splitlines()
        else:
            if not XRAY_ACCESS_LOG_PATH.exists():
                return 0

            stat = XRAY_ACCESS_LOG_PATH.stat()
            current_inode = str(stat.st_ino)
            if recorded_inode != current_inode or stat.st_size < current_offset:
                current_offset = 0

            with XRAY_ACCESS_LOG_PATH.open("r", encoding="utf-8", errors="ignore") as handle:
                handle.seek(current_offset)
                lines = handle.readlines()
                new_offset = handle.tell()

        aggregates = {}
        for line in lines:
            parsed = self.parse_xray_access_log_line(line)
            if parsed is None:
                continue
            listen_port, stat_date, seen_at = parsed
            item = aggregates.setdefault(
                (listen_port, stat_date),
                {
                    "connections": 0,
                    "last_seen": seen_at,
                },
            )
            item["connections"] += 1
            if seen_at > item["last_seen"]:
                item["last_seen"] = seen_at

        for (listen_port, stat_date), item in aggregates.items():
            conn.execute(
                """
                INSERT INTO traffic_totals (
                    listen_port, total_connections, total_bytes_sent, total_bytes_received, last_seen
                ) VALUES (?, ?, 0, 0, ?)
                ON CONFLICT(listen_port) DO UPDATE SET
                    total_connections = total_connections + excluded.total_connections,
                    last_seen = CASE
                        WHEN traffic_totals.last_seen IS NULL OR traffic_totals.last_seen < excluded.last_seen
                        THEN excluded.last_seen
                        ELSE traffic_totals.last_seen
                    END
                """,
                (
                    listen_port,
                    item["connections"],
                    item["last_seen"],
                ),
            )
            conn.execute(
                """
                INSERT INTO traffic_daily (
                    listen_port, stat_date, total_connections, total_bytes_sent, total_bytes_received
                ) VALUES (?, ?, ?, 0, 0)
                ON CONFLICT(listen_port, stat_date) DO UPDATE SET
                    total_connections = total_connections + excluded.total_connections
                """,
                (
                    listen_port,
                    stat_date,
                    item["connections"],
                ),
            )

        self.repository.set_state(conn, "xray_access_log_inode", current_inode)
        self.repository.set_state(conn, "xray_access_log_offset", str(new_offset))
        return len(aggregates)

    def parse_xray_access_log_line(self, line):
        match = XRAY_ACCESS_LOG_LINE_RE.match(line.strip())
        if match is None:
            return None

        tag = str(match.group("tag") or "").strip()
        if tag.startswith("unified-"):
            email = str(match.group("email") or "")
            if not email.startswith("panel-user-"):
                return None
            tag = "panel-" + email.removeprefix("panel-user-")
        if not tag.startswith("panel-"):
            return None

        try:
            listen_port = int(tag.removeprefix("panel-"))
        except ValueError:
            return None

        timestamp_text = match.group("seen_at")
        timestamp_format = "%Y/%m/%d %H:%M:%S.%f" if "." in timestamp_text else "%Y/%m/%d %H:%M:%S"
        try:
            seen_local = datetime.strptime(timestamp_text, timestamp_format).replace(tzinfo=LOCAL_TZ)
        except ValueError:
            return None
        seen_at = seen_local.astimezone(timezone.utc).isoformat(timespec="seconds")
        stat_date = seen_at[:10]
        return listen_port, stat_date, seen_at

    def sync_xray_traffic_stats_in_tx(self, conn):
        stats = self.stats_reader.read_xray_traffic_stats()
        if not stats:
            return 0

        now_text = utc_iso_now()
        stat_date = now_text[:10]
        for listen_port, item in stats.items():
            last_seen = now_text if item["bytes_sent"] or item["bytes_received"] else None
            conn.execute(
                """
                INSERT INTO traffic_totals (
                    listen_port, total_connections, total_bytes_sent, total_bytes_received, last_seen
                ) VALUES (?, 0, ?, ?, ?)
                ON CONFLICT(listen_port) DO UPDATE SET
                    total_bytes_sent = total_bytes_sent + excluded.total_bytes_sent,
                    total_bytes_received = total_bytes_received + excluded.total_bytes_received,
                    last_seen = CASE
                        WHEN excluded.last_seen IS NULL THEN traffic_totals.last_seen
                        WHEN traffic_totals.last_seen IS NULL OR traffic_totals.last_seen < excluded.last_seen
                        THEN excluded.last_seen
                        ELSE traffic_totals.last_seen
                    END
                """,
                (
                    listen_port,
                    item["bytes_sent"],
                    item["bytes_received"],
                    last_seen,
                ),
            )
            conn.execute(
                """
                INSERT INTO traffic_daily (
                    listen_port, stat_date, total_connections, total_bytes_sent, total_bytes_received
                ) VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(listen_port, stat_date) DO UPDATE SET
                    total_bytes_sent = total_bytes_sent + excluded.total_bytes_sent,
                    total_bytes_received = total_bytes_received + excluded.total_bytes_received
                """,
                (
                    listen_port,
                    stat_date,
                    item["bytes_sent"],
                    item["bytes_received"],
                ),
            )
        return len(stats)

    def reset_port_usage_in_tx(self, conn, listen_port):
        conn.execute(
            """
            UPDATE traffic_totals
            SET total_bytes_sent = 0, total_bytes_received = 0
            WHERE listen_port = ?
            """,
            (listen_port,),
        )
        conn.execute(
            """
            UPDATE traffic_daily
            SET total_bytes_sent = 0, total_bytes_received = 0
            WHERE listen_port = ?
            """,
            (listen_port,),
        )

    def reset_port_traffic(self, port_id):
        def operation(conn):
            row = conn.execute(
                """
                SELECT
                    p.id,
                    p.listen_port,
                    p.enabled,
                    p.expires_at,
                    p.traffic_limit_bytes,
                    COALESCE(t.total_bytes_sent, 0) AS total_bytes_sent,
                    COALESCE(t.total_bytes_received, 0) AS total_bytes_received
                FROM ports p
                LEFT JOIN traffic_totals t ON t.listen_port = p.listen_port
                WHERE p.id = ?
                """,
                (port_id,),
            ).fetchone()
            if row is None:
                raise ValidationError("端口记录不存在。")

            conn.execute(
                """
                UPDATE traffic_totals
                SET total_bytes_sent = 0, total_bytes_received = 0
                WHERE listen_port = ?
                """,
                (row["listen_port"],),
            )
            conn.execute(
                """
                UPDATE traffic_daily
                SET total_bytes_sent = 0, total_bytes_received = 0
                WHERE listen_port = ?
                """,
                (row["listen_port"],),
            )

            now_dt = utc_now()
            expired = False
            if row["expires_at"]:
                expired = datetime.fromisoformat(row["expires_at"]) <= now_dt
            usage_bytes = int(row["total_bytes_sent"]) + int(row["total_bytes_received"])
            quota_reached = row["traffic_limit_bytes"] is not None and usage_bytes >= int(row["traffic_limit_bytes"])

            next_enabled = int(row["enabled"])
            restored = False
            if quota_reached and not expired:
                next_enabled = 1
                restored = True

            conn.execute(
                "UPDATE ports SET enabled = ?, updated_at = ? WHERE id = ?",
                (next_enabled, now_dt.isoformat(timespec="seconds"), port_id),
            )
            return restored

        return self.renderer.apply_mutation(operation)
