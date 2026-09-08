import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.xray.ai_routing.repository import ensure_ai_domain_schema
from components.xray_ops.ai_domains import build_ai_domain_analysis, codex_context
from components.xray_ops.report_contract import _validate_ai_domain_analysis

UTC = timezone.utc
START = datetime(2030, 1, 1, 16, tzinfo=UTC)
END = START + timedelta(days=1)


def _write_hourly(path: Path, start: datetime, domains, route_status="applied", target=None):
    payload = {
        "generated_at": (start + timedelta(hours=1)).isoformat(),
        "window_start": start.isoformat(),
        "window_end": (start + timedelta(hours=1)).isoformat(),
        "domains": domains,
        "route_status": {"status": route_status, "reason": ""},
        "ai_target": target,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _item(domain, hits, classification, first_seen, last_seen, **extra):
    return {
        "domain": domain,
        "hits": hits,
        "classification": classification,
        "reason": extra.pop("reason", "test reason"),
        "protocols": ["tcp"],
        "first_seen": first_seen.isoformat(),
        "last_seen": last_seen.isoformat(),
        **extra,
    }


def test_build_ai_domain_analysis_merges_history_database_and_route_changes(tmp_path):
    report_dir = tmp_path / "hourly-domains" / "history"
    report_dir.mkdir(parents=True)
    target = {"upstream_host": "nat.qq.pw", "upstream_port": 27166}
    _write_hourly(
        report_dir / "before.json",
        START - timedelta(hours=1),
        [_item("chatgpt.com", 1, "ai", START - timedelta(minutes=30), START - timedelta(minutes=10))],
        target=target,
    )
    _write_hourly(
        report_dir / "first.json",
        START,
        [
            _item("chatgpt.com", 3, "ai", START + timedelta(minutes=5), START + timedelta(minutes=50)),
            _item(
                "cdn.example.com",
                2,
                "not_ai",
                START + timedelta(minutes=10),
                START + timedelta(minutes=40),
                source="codex",
                model="gpt-5.5",
            ),
        ],
        target=target,
    )
    _write_hourly(
        report_dir / "second.json",
        START + timedelta(hours=1),
        [
            _item("chatgpt.com", 4, "ai", START + timedelta(hours=1, minutes=5), START + timedelta(hours=1, minutes=50)),
            _item("new-ai.example", 1, "unknown", START + timedelta(hours=1, minutes=10), START + timedelta(hours=1, minutes=20)),
        ],
        route_status="fallback_to_primary",
    )
    (report_dir.parent / ".state.json").write_text(json.dumps({"offset": 123}), encoding="utf-8")

    panel_db = tmp_path / "panel.db"
    panel_db.touch()
    with sqlite3.connect(panel_db) as connection:
        ensure_ai_domain_schema(connection)
        connection.execute(
            """
            INSERT INTO ai_domains
            (domain, classification, reason, source, model, first_seen, last_seen, total_hits,
             last_protocols, last_report_window_start, last_report_window_end, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?)
            """,
            (
                "new-ai.example",
                "ai",
                "classified by Codex",
                "codex",
                "gpt-5.5",
                (START + timedelta(hours=1, minutes=10)).isoformat(),
                (START + timedelta(hours=1, minutes=20)).isoformat(),
                1,
                START.isoformat(),
                END.isoformat(),
                END.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO ai_domain_observations
            (domain, window_start, window_end, hits, classification, reason, source, model,
             protocols, first_seen, last_seen, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "new-ai.example",
                (START + timedelta(hours=1)).isoformat(),
                (START + timedelta(hours=2)).isoformat(),
                1,
                "ai",
                "classified by Codex",
                "codex",
                "gpt-5.5",
                '["tcp"]',
                (START + timedelta(hours=1, minutes=10)).isoformat(),
                (START + timedelta(hours=1, minutes=20)).isoformat(),
                END.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO ai_domain_observations
            (domain, window_start, window_end, hits, classification, reason, source, model,
             protocols, first_seen, last_seen, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "chatgpt.com",
                START.isoformat(),
                (START + timedelta(hours=1)).isoformat(),
                3,
                "ai",
                "classified by Codex",
                "codex",
                "gpt-5.5",
                '["tcp"]',
                (START + timedelta(minutes=5)).isoformat(),
                (START + timedelta(minutes=50)).isoformat(),
                END.isoformat(),
            ),
        )
        connection.commit()

    analysis = build_ai_domain_analysis(report_dir.parent, panel_db, START, END)
    _validate_ai_domain_analysis(analysis)

    assert analysis["status"] == "available"
    assert analysis["domain_count"] == 3
    assert analysis["observed_hits"] == 10
    assert analysis["classification_counts"] == {"ai": 2, "not_ai": 1, "unknown": 0}
    assert analysis["classification_hits"] == {"ai": 8, "not_ai": 2, "unknown": 0}
    assert analysis["new_domain_count"] == 2
    assert {item["domain"] for item in analysis["new_domains"]} == {"cdn.example.com", "new-ai.example"}
    assert analysis["codex"]["status"] == "success"
    assert analysis["codex"]["classified_count"] == 3

    domains = {item["domain"]: item for item in analysis["domains"]}
    assert domains["chatgpt.com"]["hits"] == 7
    assert domains["chatgpt.com"]["traffic_direction"] == "mixed"
    assert {route["outbound_tag"] for route in domains["chatgpt.com"]["traffic_routes"]} == {"ai_proxy", "direct"}
    assert domains["chatgpt.com"]["source"] == "codex"
    assert domains["cdn.example.com"]["traffic_direction"] == "direct"
    assert domains["new-ai.example"]["classification"] == "ai"
    assert domains["new-ai.example"]["traffic_direction"] == "direct"

    context = codex_context(analysis, max_domains=1)
    assert context["truncated"] is True
    assert len(context["domains"]) == 1
    assert len(context["new_domains"]) <= 1


def test_build_ai_domain_analysis_reports_unconfigured_sources(tmp_path):
    analysis = build_ai_domain_analysis(
        tmp_path / "missing-reports",
        tmp_path / "missing-panel.db",
        START,
        END,
    )

    assert analysis["status"] == "unknown"
    assert analysis["domain_count"] == 0
    assert {item["error_class"] for item in analysis["sources"]} == {
        "ai_domain_history_not_configured",
        "panel_db_missing",
    }
