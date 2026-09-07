import json
import os
import subprocess
import sys
from pathlib import Path

from ..config import (
    AI_DOMAIN_MANAGER_CONTAINER_NAME,
    AI_DOMAIN_MANAGER_DOCKER_BIN,
    AI_DOMAIN_MANAGER_EXECUTION_MODE,
    AI_ROUTING_ENABLED,
    XRAY_CONFIG_PATH,
    XRAY_ENV_FILE_PATH,
)
from ..errors import ValidationError
from ..helpers import (
    utc_iso_now,
)
from ..observability.logging import emit_business_event
from ..xray.ai_domain_manager import LOCK_BUSY_EXIT_CODE, build_ai_upstream_candidates, ensure_ai_domain_schema
from ..xray.envfile import load_env_file
from ..xray.operation_lock import LockBusyError, exclusive_file_lock


class AiRoutingService:
    def __init__(self, panel):
        self._panel = panel
    def sync_data_plane_ai_state(self):
        before = self._panel.read_ai_domain_report()
        result = {
            "report_synced": False,
            "snapshot_synced": False,
        }
        try:
            if self._panel.data_plane.supports_ai_report_pull():
                result["report_synced"] = self._panel.data_plane.sync_ai_report_from_remote()
            if self._panel.data_plane.supports_ai_domains_snapshot_pull():
                snapshot = self._panel.data_plane.read_ai_domains_snapshot_from_remote()
                if snapshot.get("exists"):
                    self._panel.replace_ai_domains_snapshot(snapshot.get("ai_domains", []))
                    result["snapshot_synced"] = True
        except Exception as exc:
            emit_business_event(
                "ai_routing.changed",
                result="failure",
                actor_type="system",
                error_code="sync_failed",
                exc=exc,
            )
            raise
        after = self._panel.read_ai_domain_report()
        before_signature = (before or {}).get("generated_at"), (before or {}).get("route_status")
        after_signature = (after or {}).get("generated_at"), (after or {}).get("route_status")
        if (result["report_synced"] or result["snapshot_synced"]) and before_signature != after_signature:
            emit_business_event(
                "ai_routing.changed",
                actor_type="system",
                metadata={"route_status": (after or {}).get("route_status", "unknown")},
            )
        return result
    def replace_ai_domains_snapshot(self, rows):
        def operation(conn):
            ensure_ai_domain_schema(conn)
            conn.execute("DELETE FROM ai_domain_observations")
            conn.execute("DELETE FROM ai_domains")

            payloads = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                domain = str(row.get("domain", "")).strip()
                if not domain:
                    continue
                last_protocols = row.get("last_protocols", "[]")
                if isinstance(last_protocols, list):
                    last_protocols = json.dumps(last_protocols, ensure_ascii=True)
                else:
                    last_protocols = str(last_protocols or "[]")
                payloads.append(
                    (
                        domain,
                        str(row.get("classification", "ai") or "ai").strip() or "ai",
                        str(row.get("reason", "") or "").strip(),
                        str(row.get("source", "") or "").strip(),
                        str(row.get("model", "") or "").strip(),
                        str(row.get("first_seen") or "").strip() or None,
                        str(row.get("last_seen") or "").strip() or None,
                        int(row.get("total_hits", 0) or 0),
                        last_protocols,
                        str(row.get("last_report_window_start") or "").strip() or None,
                        str(row.get("last_report_window_end") or "").strip() or None,
                        str(row.get("updated_at") or "").strip() or utc_iso_now(),
                    )
                )

            if payloads:
                conn.executemany(
                    """
                    INSERT INTO ai_domains (
                        domain,
                        classification,
                        reason,
                        source,
                        model,
                        first_seen,
                        last_seen,
                        total_hits,
                        last_protocols,
                        last_report_window_start,
                        last_report_window_end,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payloads,
                )
            return len(payloads)

        return self._panel.apply_state_update(operation)
    def ai_domain_sync_mode_label(self):
        mode = self._panel.data_plane.mode
        if mode == "ssh":
            return "远端镜像"
        if mode in {"local", "docker"}:
            return "本地运行"
        return "本地缓存"

    def ai_routing_manual_state(self):
        report = self._panel.read_ai_domain_report()
        candidates = []
        if isinstance(report, dict):
            target = report.get("ai_target")
            if isinstance(target, dict):
                candidates = [item for item in target.get("candidates", []) if isinstance(item, dict)]
        if not candidates:
            candidates = self._configured_ai_candidates()
        report_selected_index = None
        if isinstance(report, dict):
            target = report.get("ai_target")
            if isinstance(target, dict):
                try:
                    report_selected_index = int(target.get("selected_index"))
                except (TypeError, ValueError):
                    report_selected_index = None
        with self._panel.connect() as conn:
            mode = str(self._panel.get_state(conn, "ai_routing_manual_mode", "auto") or "auto").strip().lower()
            updated_at = str(self._panel.get_state(conn, "ai_routing_manual_updated_at", "") or "").strip()
        if mode not in {"auto", "primary", "backup", "forced_fallback"}:
            mode = "auto"
        selected_index = {"primary": 0, "backup": 1}.get(mode, report_selected_index)
        for index, candidate in enumerate(candidates):
            candidate["index"] = index
            candidate["number"] = index + 1
            candidate["selected"] = selected_index == index
            candidate["label"] = (
                "主 AI 节点" if index == 0 else
                ("备用 AI 节点" if index == 1 else f"AI 节点 {index + 1}")
            )
        return {
            "mode": mode,
            "mode_label": {
                "auto": "自动探测",
                "primary": "人工指定主 AI",
                "backup": "人工指定备用 AI",
                "forced_fallback": "人工强制回退",
            }[mode],
            "updated_at": updated_at,
            "updated_at_display": self._panel.format_optional_display_time(updated_at) if updated_at else "暂无",
            "candidates": candidates,
            "candidate_count": len(candidates),
        }

    def _configured_ai_candidates(self):
        try:
            values = load_env_file(XRAY_ENV_FILE_PATH)
            candidates = build_ai_upstream_candidates(
                values.get("AI_UPSTREAM_HOST", ""),
                int(values.get("AI_UPSTREAM_PORT", "27166")),
                upstreams_raw=values.get("AI_UPSTREAMS", ""),
                fallbacks_raw=values.get("AI_UPSTREAM_FALLBACKS", ""),
                fallback_share_url=values.get("AI_UPSTREAM_FALLBACK_URL", ""),
            )
        except (OSError, ValueError, TypeError):
            return []
        return [
            {
                "upstream_host": item.get("upstream_host", ""),
                "upstream_port": int(item.get("upstream_port", 0) or 0),
                "candidate_type": item.get("candidate_type", "template"),
                "candidate_label": item.get("candidate_label", ""),
                "is_reachable": None,
                "selected": False,
            }
            for item in candidates
        ]

    def _trigger_ai_domain_manager(self, manual_mode=None):
        run_options = {}
        if AI_DOMAIN_MANAGER_EXECUTION_MODE == "local":
            command = [sys.executable, "-m", "app.xray.ai_domain_manager", "--once"]
            package_parent = Path(__file__).resolve().parents[2]
            if not (package_parent / "app").is_dir():
                package_parent = Path(__file__).resolve().parents[1]
            run_options["cwd"] = str(package_parent)
        else:
            if not AI_DOMAIN_MANAGER_CONTAINER_NAME:
                raise RuntimeError("AI 域名管理器容器未配置。")
            command = [
                AI_DOMAIN_MANAGER_DOCKER_BIN,
                "exec",
                AI_DOMAIN_MANAGER_CONTAINER_NAME,
                "python3",
                "-m",
                "app.xray.ai_domain_manager",
                "--once",
        ]
        if manual_mode is not None:
            command.extend(("--manual-mode", str(manual_mode), "--manual-lock-held"))
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=240,
                **run_options,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("AI 路由重算超时，保持原有实际路由。") from exc
        if completed.returncode == LOCK_BUSY_EXIT_CODE:
            raise RuntimeError("AI 路由正在应用配置，请稍后重试。")
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "AI 域名管理器执行失败").strip()
            raise RuntimeError(detail[-500:])

    def _manual_mode_lock_path(self):
        configured = os.environ.get("AI_DOMAIN_MANAGER_MANUAL_LOCK_PATH", "").strip()
        if configured:
            return configured
        return XRAY_CONFIG_PATH.with_name(".ai-domain-manager-manual.lock")

    def set_ai_routing_manual_mode(self, mode):
        mode = str(mode or "").strip().lower()
        if mode not in {"auto", "primary", "backup", "forced_fallback"}:
            raise ValidationError("AI 路由模式仅支持 auto、primary、backup 或 forced_fallback。")
        if not AI_ROUTING_ENABLED:
            raise ValidationError("AI 路由未启用。")

        if mode in {"primary", "backup"}:
            candidate_state = self.ai_routing_manual_state()
            if len(candidate_state["candidates"]) < 2:
                raise ValidationError("当前只配置了一个 AI 节点，无法执行双节点切换。")

        updated_at = utc_iso_now()

        def operation(conn):
            self._panel.set_state(conn, "ai_routing_manual_mode", mode)
            self._panel.set_state(conn, "ai_routing_manual_updated_at", updated_at)

        if mode == "forced_fallback" and not self._panel.data_plane.is_configured():
            raise ValidationError("数据面未配置，无法应用 AI 回退。")
        try:
            # Hold a second shared gate across the child manager and the final
            # panel-state commit.  Scheduled manager runs take this gate before
            # their apply lock, preventing an old-mode run from racing the
            # short interval between node apply and database commit.
            with exclusive_file_lock(self._manual_mode_lock_path()):
                previous_mode = self.ai_routing_manual_state()["mode"]
                # Keep the persisted mode unchanged while the manager applies
                # the requested mode explicitly.  This prevents a concurrent
                # resident manager from observing a half-applied mode and
                # makes a failed request leave both database and node on the
                # old route.
                self._trigger_ai_domain_manager(mode)
                try:
                    self._panel.apply_state_update(operation)
                except Exception:
                    # The node is already on the requested route.  If the
                    # final panel state commit fails, make a best-effort
                    # compensating apply so the database and running node do
                    # not silently diverge.
                    if previous_mode != mode:
                        try:
                            self._trigger_ai_domain_manager(previous_mode)
                        except Exception as rollback_exc:  # noqa: BLE001 - preserve the original state-commit error
                            emit_business_event(
                                "ai_routing.manual_switched",
                                result="failure",
                                actor_type="system",
                                error_code="state_commit_rollback_failed",
                                message=str(rollback_exc),
                            )
                    raise
        except LockBusyError as exc:
            raise RuntimeError("AI 路由正在应用配置，请稍后重试。") from exc
        emit_business_event(
            "ai_routing.manual_switched",
            actor_type="admin",
            resource_type="ai_routing",
            metadata={"mode": mode},
        )
        return self.ai_routing_manual_state()

    def ai_route_status_label(self, status, reason=""):
        status_text = str(status or "").strip().lower()
        if status_text == "applied":
            return "已应用 AI 路由"
        if status_text == "fallback_to_primary":
            return "AI 节点不可达，已回退主链路"
        if status_text == "probe_error":
            return "AI 节点探测失败，已停用动态 AI 路由"
        if status_text == "manual_fallback":
            return "人工强制回退到主链路"
        if status_text == "manual_target_unreachable":
            return "人工指定 AI 不可达，已停用动态路由"
        if status_text == "idle":
            return "当前窗口无 AI 域名"
        if status_text == "pending_proxy_template":
            return "等待代理模板"
        if status_text == "disabled":
            return "AI 路由未启用"
        if reason:
            return reason
        return "未知状态"
    def ai_route_status_tone(self, status):
        status_text = str(status or "").strip().lower()
        if status_text in {"applied", "manual_selected"}:
            return "ok"
        if status_text in {"fallback_to_primary", "probe_error", "manual_fallback", "manual_target_unreachable", "idle", "disabled"}:
            return "warn"
        return "bad"
    def ai_source_label(self, value):
        text = str(value or "").strip()
        return text or "未标记"
    def decode_json_text_list(self, value):
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        raw = str(value or "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item).strip() for item in parsed if str(item).strip()]
    def serialize_ai_report_domain(self, item):
        protocols = self._panel.decode_json_text_list(item.get("protocols", []))
        return {
            "domain": str(item.get("domain", "")).strip(),
            "hits": int(item.get("hits", 0) or 0),
            "classification": str(item.get("classification", "unknown") or "unknown").strip() or "unknown",
            "reason": str(item.get("reason", "") or "").strip(),
            "protocols": protocols,
            "protocols_display": ", ".join(protocols) if protocols else "暂无",
            "first_seen": str(item.get("first_seen") or "").strip() or None,
            "last_seen": str(item.get("last_seen") or "").strip() or None,
            "first_seen_display": self._panel.format_optional_display_time(item.get("first_seen")),
            "last_seen_display": self._panel.format_optional_display_time(item.get("last_seen")),
        }
    def serialize_ai_domain_snapshot_row(self, row):
        item = dict(row)
        protocols = self._panel.decode_json_text_list(item.get("last_protocols", "[]"))
        return {
            **item,
            "classification": str(item.get("classification", "ai") or "ai").strip() or "ai",
            "source_display": self._panel.ai_source_label(item.get("source")),
            "protocols": protocols,
            "protocols_display": ", ".join(protocols) if protocols else "暂无",
            "first_seen_display": self._panel.format_optional_display_time(item.get("first_seen")),
            "last_seen_display": self._panel.format_optional_display_time(item.get("last_seen")),
            "updated_at_display": self._panel.format_optional_display_time(item.get("updated_at")),
            "last_report_window_start_display": self._panel.format_optional_display_time(
                item.get("last_report_window_start")
            ),
            "last_report_window_end_display": self._panel.format_optional_display_time(
                item.get("last_report_window_end")
            ),
        }
    def read_ai_domain_report(self):
        report_path = self._panel.data_plane.config.source_ai_report_path or self._panel.data_plane_ai_report_source_path()
        if not report_path.is_file():
            return None
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None

        route_status = payload.get("route_status", {})
        if not isinstance(route_status, dict):
            route_status = {}
        route_status_code = str(route_status.get("status", "unknown") or "unknown").strip() or "unknown"
        route_status_reason = str(route_status.get("reason", "") or "").strip()

        domains = []
        for raw_item in payload.get("domains", []):
            if not isinstance(raw_item, dict):
                continue
            domain_item = self._panel.serialize_ai_report_domain(raw_item)
            if domain_item["domain"]:
                domains.append(domain_item)
        current_ai_domains = [item for item in domains if item["classification"] == "ai"]

        ai_target = payload.get("ai_target")
        if not isinstance(ai_target, dict):
            ai_target = None
        panel_target = payload.get("panel_target")
        if not isinstance(panel_target, dict):
            panel_target = None

        return {
            "generated_at": str(payload.get("generated_at") or "").strip() or None,
            "generated_at_display": self._panel.format_optional_display_time(payload.get("generated_at")),
            "window_start": str(payload.get("window_start") or "").strip() or None,
            "window_start_display": self._panel.format_optional_display_time(payload.get("window_start")),
            "window_end": str(payload.get("window_end") or "").strip() or None,
            "window_end_display": self._panel.format_optional_display_time(payload.get("window_end")),
            "unique_domains": int(payload.get("unique_domains", len(domains)) or 0),
            "ai_domain_count": len(current_ai_domains),
            "domains": domains,
            "current_ai_domains": current_ai_domains,
            "protocols": [item for item in payload.get("protocols", []) if isinstance(item, dict)],
            "route_status": route_status_code,
            "route_status_reason": route_status_reason,
            "route_status_label": self._panel.ai_route_status_label(route_status_code, route_status_reason),
            "route_status_tone": self._panel.ai_route_status_tone(route_status_code),
            "config_changed": bool(route_status.get("config_changed")),
            "config_retried": bool(route_status.get("config_retried")),
            "pending_domains_without_classifier": self._panel.normalize_pending_domain_count(
                route_status.get("pending_domains_without_classifier", 0)
            ),
            "ai_target": ai_target,
            "panel_target": panel_target,
        }
    def normalize_pending_domain_count(self, value):
        if isinstance(value, (list, tuple, set, dict)):
            return len(value)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0
    def query_ai_domain_aggregate(self):
        with self._panel.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_ai_domains,
                    COALESCE(SUM(total_hits), 0) AS total_hits,
                    MAX(updated_at) AS updated_at
                FROM ai_domains
                """
            ).fetchone()
        return {
            "total_ai_domains": int(row["total_ai_domains"] or 0),
            "total_hits": int(row["total_hits"] or 0),
            "updated_at": row["updated_at"],
            "updated_at_display": self._panel.format_optional_display_time(row["updated_at"]),
        }
    def query_top_ai_domains(self, limit=100):
        with self._panel.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    domain,
                    classification,
                    reason,
                    source,
                    model,
                    first_seen,
                    last_seen,
                    total_hits,
                    last_protocols,
                    last_report_window_start,
                    last_report_window_end,
                    updated_at
                FROM ai_domains
                ORDER BY total_hits DESC, last_seen DESC, domain ASC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [self._panel.serialize_ai_domain_snapshot_row(row) for row in rows]
    def query_ai_domain_source_breakdown(self):
        with self._panel.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    CASE
                        WHEN TRIM(source) = '' THEN ''
                        ELSE source
                    END AS source,
                    COUNT(*) AS domain_count,
                    COALESCE(SUM(total_hits), 0) AS total_hits,
                    MAX(updated_at) AS updated_at
                FROM ai_domains
                GROUP BY source
                ORDER BY total_hits DESC, domain_count DESC, source ASC
                """
            ).fetchall()
        return [
            {
                "source": row["source"],
                "source_display": self._panel.ai_source_label(row["source"]),
                "domain_count": int(row["domain_count"] or 0),
                "total_hits": int(row["total_hits"] or 0),
                "updated_at": row["updated_at"],
                "updated_at_display": self._panel.format_optional_display_time(row["updated_at"]),
            }
            for row in rows
        ]
    def ai_routing_status(self, sync_error=""):
        report = self._panel.read_ai_domain_report()
        aggregate = self._panel.query_ai_domain_aggregate()
        manual = self.ai_routing_manual_state()
        configured = AI_ROUTING_ENABLED
        if manual["mode"] == "forced_fallback" and configured:
            status_code = "manual_fallback"
            status_label = "人工强制回退到主链路"
            tone = "warn"
        elif not configured:
            status_code = "disabled"
            status_label = "AI 路由未启用"
            tone = "warn"
        elif report is not None:
            status_code = report["route_status"]
            status_label = report["route_status_label"]
            tone = report["route_status_tone"]
        elif sync_error:
            status_code = "sync_error"
            status_label = "AI 路由同步失败"
            tone = "bad"
        else:
            status_code = "waiting_report"
            status_label = "等待 AI 路由报告"
            tone = "warn"
        return {
            "configured": configured,
            "status": status_code,
            "status_label": status_label,
            "status_tone": tone,
            "sync_mode_label": self._panel.ai_domain_sync_mode_label(),
            "report_generated_at_display": report["generated_at_display"] if report else "暂无",
            "current_ai_domains": report["ai_domain_count"] if report else 0,
            "total_ai_domains": aggregate["total_ai_domains"],
            "manual_mode": manual["mode"],
            "manual_mode_label": manual["mode_label"],
            "manual_updated_at": manual["updated_at"],
            "manual_updated_at_display": manual["updated_at_display"],
            "ai_candidates": manual["candidates"],
            "ai_candidate_count": manual["candidate_count"],
            "sync_error": str(sync_error or "").strip(),
        }
    def query_ai_domain_overview(self, sync_error=""):
        report = self._panel.read_ai_domain_report()
        aggregate = self._panel.query_ai_domain_aggregate()
        return {
            "available": AI_ROUTING_ENABLED and bool(report or aggregate["total_ai_domains"] > 0),
            "enabled": AI_ROUTING_ENABLED,
            "sync_mode": self._panel.data_plane.mode,
            "sync_mode_label": self._panel.ai_domain_sync_mode_label(),
            "sync_error": str(sync_error or "").strip(),
            "report_available": report is not None,
            "current_ai_domains": report["ai_domain_count"] if report else 0,
            "unique_domains": report["unique_domains"] if report else 0,
            "report_generated_at_display": (
                report["generated_at_display"] if report else "暂无"
            ),
            "route_status": report["route_status"] if report else "unknown",
            "route_status_label": report["route_status_label"] if report else "暂无报告",
            "route_status_tone": report["route_status_tone"] if report else "warn",
            "total_ai_domains": aggregate["total_ai_domains"],
            "total_hits": aggregate["total_hits"],
            "aggregate_updated_at_display": aggregate["updated_at_display"],
        }
    def get_ai_domain_dashboard(self, sync_error=""):
        report = self._panel.read_ai_domain_report()
        aggregate = self._panel.query_ai_domain_aggregate()
        top_ai_domains = self._panel.query_top_ai_domains()
        source_breakdown = self._panel.query_ai_domain_source_breakdown()
        return {
            "available": AI_ROUTING_ENABLED and bool(report or top_ai_domains),
            "enabled": AI_ROUTING_ENABLED,
            "sync_mode": self._panel.data_plane.mode,
            "sync_mode_label": self._panel.ai_domain_sync_mode_label(),
            "sync_error": str(sync_error or "").strip(),
            "report": (
                report
                if report is not None
                else {
                    "generated_at_display": "暂无",
                    "window_start_display": "暂无",
                    "window_end_display": "暂无",
                    "unique_domains": 0,
                    "ai_domain_count": 0,
                    "domains": [],
                    "current_ai_domains": [],
                    "route_status": "unknown",
                    "route_status_label": "暂无报告",
                    "route_status_tone": "warn",
                    "config_changed": False,
                    "config_retried": False,
                    "pending_domains_without_classifier": 0,
                    "ai_target": None,
                    "panel_target": None,
                }
            ),
            "aggregate": aggregate,
            "top_ai_domains": top_ai_domains,
            "source_breakdown": source_breakdown,
        }
