"""SQLite persistence for AI domains and AI routing state."""

from __future__ import annotations

import json
import sqlite3

from .common import connect_panel_db, run_with_sqlite_lock_retry


def read_panel_target(panel_db_path, preferred_listen_port):
    if not panel_db_path.is_file():
        return None

    def read_target():
        conn = connect_panel_db(panel_db_path)
        try:
            if preferred_listen_port:
                row = conn.execute(
                    """
                    SELECT listen_port, upstream_host, upstream_port, note, updated_at
                    FROM ports
                    WHERE enabled = 1 AND listen_port = ?
                    LIMIT 1
                    """,
                    (preferred_listen_port,),
                ).fetchone()
                if row:
                    return dict(row)
            row = conn.execute(
                """
                SELECT listen_port, upstream_host, upstream_port, note, updated_at
                FROM ports
                WHERE enabled = 1
                ORDER BY updated_at DESC, listen_port ASC
                LIMIT 1
                """
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    return run_with_sqlite_lock_retry(read_target)


def ensure_ai_domain_schema(conn):
    conn.executescript(
        """
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
        """
    )


def _save_ai_domains_to_panel_db_once(panel_db_path, report, decisions):
    status = {
        "status": "skipped",
        "reason": "",
        "path": str(panel_db_path),
        "domains_upserted": 0,
        "observations_upserted": 0,
    }
    if not panel_db_path.is_file():
        status["reason"] = "panel_db_missing"
        return status

    observed_ai_items = [item for item in report["domains"] if item["classification"] == "ai"]
    observed_ai_by_domain = {item["domain"]: item for item in observed_ai_items}
    conn = connect_panel_db(panel_db_path)
    try:
        ensure_ai_domain_schema(conn)

        historical_ai_domains = {
            row["domain"]
            for row in conn.execute(
                "SELECT DISTINCT domain FROM ai_domain_observations WHERE classification = 'ai'"
            ).fetchall()
        }
        historical_ai_domains.update(
            row["domain"]
            for row in conn.execute(
                "SELECT domain FROM ai_domains WHERE classification = 'ai' AND total_hits > 0"
            ).fetchall()
        )
        ai_domains = sorted(
            domain
            for domain, item in decisions["domains"].items()
            if item.get("classification") == "ai"
            and (domain in observed_ai_by_domain or domain in historical_ai_domains)
        )

        for item in observed_ai_items:
            decision = decisions["domains"].get(item["domain"], {})
            conn.execute(
                """
                INSERT INTO ai_domain_observations (
                    domain, window_start, window_end, hits, classification,
                    reason, source, model, protocols, first_seen, last_seen, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(domain, window_start, window_end) DO UPDATE SET
                    hits = excluded.hits,
                    classification = excluded.classification,
                    reason = excluded.reason,
                    source = excluded.source,
                    model = excluded.model,
                    protocols = excluded.protocols,
                    first_seen = excluded.first_seen,
                    last_seen = excluded.last_seen,
                    created_at = excluded.created_at
                """,
                (
                    item["domain"],
                    report["window_start"],
                    report["window_end"],
                    item["hits"],
                    item["classification"],
                    item["reason"],
                    str(decision.get("source", "")).strip(),
                    str(decision.get("model", "")).strip(),
                    json.dumps(item["protocols"], ensure_ascii=True),
                    item.get("first_seen"),
                    item.get("last_seen"),
                    report["generated_at"],
                ),
            )
            status["observations_upserted"] += 1

        for domain in ai_domains:
            item = observed_ai_by_domain.get(
                domain,
                {
                    "domain": domain,
                    "classification": "ai",
                    "reason": decisions["domains"].get(domain, {}).get("reason", ""),
                    "protocols": [],
                    "first_seen": None,
                    "last_seen": None,
                },
            )
            decision = decisions["domains"].get(domain, {})
            aggregate = conn.execute(
                """
                SELECT COALESCE(SUM(hits), 0) AS total_hits,
                       MIN(COALESCE(first_seen, last_seen)) AS first_seen,
                       MAX(last_seen) AS last_seen
                FROM ai_domain_observations
                WHERE domain = ?
                """,
                (domain,),
            ).fetchone()
            existing = conn.execute("SELECT last_protocols FROM ai_domains WHERE domain = ?", (domain,)).fetchone()
            protocols = json.dumps(item["protocols"], ensure_ascii=True)
            if not item["protocols"] and existing and existing["last_protocols"]:
                protocols = existing["last_protocols"]
            conn.execute(
                """
                INSERT INTO ai_domains (
                    domain, classification, reason, source, model, first_seen, last_seen,
                    total_hits, last_protocols, last_report_window_start,
                    last_report_window_end, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    classification = excluded.classification,
                    reason = excluded.reason,
                    source = excluded.source,
                    model = excluded.model,
                    first_seen = excluded.first_seen,
                    last_seen = excluded.last_seen,
                    total_hits = excluded.total_hits,
                    last_protocols = excluded.last_protocols,
                    last_report_window_start = excluded.last_report_window_start,
                    last_report_window_end = excluded.last_report_window_end,
                    updated_at = excluded.updated_at
                """,
                (
                    domain,
                    item["classification"],
                    item["reason"],
                    str(decision.get("source", "")).strip(),
                    str(decision.get("model", "")).strip(),
                    aggregate["first_seen"] or item.get("first_seen"),
                    aggregate["last_seen"] or item.get("last_seen"),
                    int(aggregate["total_hits"]),
                    protocols,
                    report["window_start"],
                    report["window_end"],
                    report["generated_at"],
                ),
            )
            status["domains_upserted"] += 1

        if ai_domains:
            placeholders = ", ".join("?" for _ in ai_domains)
            conn.execute(f"DELETE FROM ai_domains WHERE domain NOT IN ({placeholders})", ai_domains)
        else:
            conn.execute("DELETE FROM ai_domains")

        conn.commit()
        status["status"] = "written"
        return status
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_ai_domains_to_panel_db(panel_db_path, report, decisions):
    try:
        return run_with_sqlite_lock_retry(lambda: _save_ai_domains_to_panel_db_once(panel_db_path, report, decisions))
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "database is locked" not in message and "database is busy" not in message:
            raise
        return {
            "status": "skipped",
            "reason": "database_locked",
            "path": str(panel_db_path),
            "domains_upserted": 0,
            "observations_upserted": 0,
        }


def read_ai_routing_manual_mode(panel_db_path):
    path = str(panel_db_path or "").strip()
    if not path:
        return "auto"

    def read_mode():
        conn = connect_panel_db(path)
        try:
            return conn.execute("SELECT value FROM app_state WHERE key = 'ai_routing_manual_mode'").fetchone()
        finally:
            conn.close()

    try:
        row = run_with_sqlite_lock_retry(read_mode)
    except (OSError, sqlite3.Error):
        return "auto"
    mode = str(row[0] if row else "auto").strip().lower()
    return mode if mode in {"auto", "primary", "backup", "forced_fallback"} else "auto"


__all__ = [
    "connect_panel_db",
    "ensure_ai_domain_schema",
    "read_ai_routing_manual_mode",
    "read_panel_target",
    "run_with_sqlite_lock_retry",
    "save_ai_domains_to_panel_db",
]
