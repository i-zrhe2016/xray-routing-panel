from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import (
    CONTROL_PLANE_BACKUP_XRAY_ENABLED,
    LOCAL_TZ,
)
from ..dns_failover import CloudflareApiError, resolve_public_ip
from ..errors import ValidationError
from ..helpers import (
    format_display_time,
    utc_iso_now,
)
from ..observability.logging import emit_business_event


class DnsFailoverService:
    """DNS failover policy backed by explicit storage, node and DNS manager ports."""

    def __init__(
        self,
        repository=None,
        node_controller=None,
        failover_manager=None,
        backup_xray=None,
        write_lock=None,
        public_ip_resolver=None,
    ):
        self.repository = repository
        self.node_controller = node_controller
        self.failover_manager = failover_manager
        self.backup_xray = backup_xray
        self.write_lock = write_lock
        self.public_ip_resolver = public_ip_resolver if public_ip_resolver is not None else resolve_public_ip

    def _backup_xray_mode(self):
        if self.backup_xray is None:
            return "disabled"
        return self.backup_xray.backup_xray_mode()

    def ensure_dns_failover_schema(self, conn):
        conn.executescript(
            """
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
        conn.execute(
            """
            INSERT INTO dns_failover_state (singleton_id)
            VALUES (1)
            ON CONFLICT(singleton_id) DO NOTHING
            """
        )

    def parse_time_of_day_minutes(self, value):
        text = str(value or "").strip()
        if not text:
            return None
        hour_text, _, minute_text = text.partition(":")
        try:
            hour = int(hour_text)
            minute = int(minute_text)
        except ValueError:
            return None
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            return None
        return (hour * 60) + minute

    def resolve_dns_failover_peak_timezone(self, raw):
        text = str(raw or "").strip()
        if not text:
            return LOCAL_TZ, "local"
        if len(text) == 6 and text[0] in {"+", "-"} and text[3] == ":":
            try:
                hours = int(text[1:3])
                minutes = int(text[4:6])
            except ValueError as exc:
                raise ValidationError("DNS_FAILOVER_PEAK_TIMEZONE 格式无效。") from exc
            offset = timedelta(hours=hours, minutes=minutes)
            if text[0] == "-":
                offset = -offset
            return timezone(offset), text
        try:
            return ZoneInfo(text), text
        except ZoneInfoNotFoundError as exc:
            raise ValidationError("DNS_FAILOVER_PEAK_TIMEZONE 无法识别。") from exc

    def peak_window_active(self, current_minutes, start_minutes, end_minutes):
        """Whether ``current_minutes`` falls inside the window, handling spans that cross midnight."""
        if start_minutes < end_minutes:
            return start_minutes <= current_minutes < end_minutes
        return current_minutes >= start_minutes or current_minutes < end_minutes

    def dns_failover_peak_window_status(self, now=None):
        config = self.failover_manager.config
        status = {
            "enabled": bool(config.peak_enabled),
            "configured": False,
            "active": False,
            "target": "backup",
            "target_label": config.backup_label,
            "preferred_target": "primary",
            "preferred_target_label": self.failover_manager.target_label("primary"),
            "next_preferred_target": "",
            "next_preferred_target_label": "",
            "start": str(config.peak_start or "").strip(),
            "end": str(config.peak_end or "").strip(),
            "timezone": str(config.peak_timezone or "").strip(),
            "timezone_label": "服务器本地时区",
            "current_time": "",
            "next_transition_at": "",
            "seconds_to_next_transition": 0,
            "config_error": "",
        }
        if not status["enabled"]:
            return status

        start_minutes = self.parse_time_of_day_minutes(status["start"])
        end_minutes = self.parse_time_of_day_minutes(status["end"])
        if start_minutes is None or end_minutes is None:
            status["config_error"] = "缺少有效的 DNS_FAILOVER_PEAK_START / DNS_FAILOVER_PEAK_END。"
            return status
        if start_minutes == end_minutes:
            status["config_error"] = "DNS_FAILOVER_PEAK_START 与 DNS_FAILOVER_PEAK_END 不能相同。"
            return status

        try:
            tzinfo, tz_label = self.resolve_dns_failover_peak_timezone(status["timezone"])
        except ValidationError as exc:
            status["config_error"] = str(exc)
            return status

        current = (now or datetime.now(timezone.utc)).astimezone(tzinfo)
        current_minutes = (current.hour * 60) + current.minute
        active = self.peak_window_active(current_minutes, start_minutes, end_minutes)
        preferred_target = "backup" if active else "primary"

        # The preference flips at the window's end boundary while active, at its start boundary otherwise.
        next_boundary_minutes = end_minutes if active else start_minutes
        current_seconds = (current.hour * 3600) + (current.minute * 60) + current.second
        delta_seconds = ((next_boundary_minutes * 60) - current_seconds) % 86400 or 86400
        next_transition = current + timedelta(seconds=delta_seconds)
        next_preferred_target = "primary" if active else "backup"

        status.update(
            {
                "configured": True,
                "active": active,
                "preferred_target": preferred_target,
                "preferred_target_label": self.failover_manager.target_label(preferred_target),
                "next_preferred_target": next_preferred_target,
                "next_preferred_target_label": self.failover_manager.target_label(next_preferred_target),
                "timezone_label": tz_label,
                "current_time": current.strftime("%Y-%m-%d %H:%M:%S"),
                "next_transition_at": next_transition.strftime("%Y-%m-%d %H:%M"),
                "seconds_to_next_transition": int(delta_seconds),
            }
        )
        return status

    def evaluate_dns_failover_transition(
        self,
        current_target,
        consecutive_failures,
        consecutive_successes,
        probe_ok,
        preferred_target="primary",
    ):
        preferred = str(preferred_target or "").strip().lower()
        if preferred not in {"primary", "backup"}:
            preferred = "primary"
        alternate = "backup" if preferred == "primary" else "primary"

        if probe_ok:
            if current_target == alternate and consecutive_successes >= self.failover_manager.config.recovery_threshold:
                return {
                    "target": preferred,
                    "reason": "peak_recovery" if preferred == "backup" else "auto_recovery",
                }
            return None
        if current_target == preferred and consecutive_failures >= self.failover_manager.config.failure_threshold:
            return {
                "target": alternate,
                "reason": "peak_failover" if preferred == "backup" else "auto_failover",
            }
        return None

    def dns_failover_status(self):
        config = self.failover_manager.config
        resolved_contents = self.resolve_dns_failover_contents()
        peak_window = self.dns_failover_peak_window_status()
        with self.repository.connect() as conn:
            row = conn.execute("SELECT * FROM dns_failover_state WHERE singleton_id = 1").fetchone()
        item = dict(row) if row is not None else {}
        current_target = item.get("current_target") or "primary"
        record_content = item.get("current_record_content") or ""
        if current_target not in {"primary", "backup"}:
            inferred = self.failover_manager.current_target_from_content(
                record_content,
                primary_content=resolved_contents["primary_content"],
                backup_content=resolved_contents["backup_content"],
            )
            current_target = inferred if inferred in {"primary", "backup"} else "primary"
        return {
            "enabled": bool(config.enabled),
            "configured": bool(
                config.enabled
                and config.configured()
                and config.record_type_supported()
                and not resolved_contents["error"]
                and (not peak_window["enabled"] or peak_window["configured"])
            ),
            "config_error": self.dns_failover_config_error(),
            "current_target": current_target,
            "current_target_label": self.failover_manager.target_label(current_target),
            "record_name": config.record_name,
            "record_type": config.record_type,
            "record_content": record_content,
            "primary_content": resolved_contents["primary_content"],
            "backup_content": resolved_contents["backup_content"],
            "record_ttl": item.get("current_record_ttl") or (1 if config.record_proxied else config.record_ttl),
            "record_proxied": (
                bool(item.get("current_record_proxied"))
                if item.get("current_record_proxied") is not None
                else bool(config.record_proxied)
            ),
            "probe_host": config.probe_host,
            "probe_port": config.probe_port,
            "failure_threshold": config.failure_threshold,
            "recovery_threshold": config.recovery_threshold,
            "last_probe_status": item.get("last_probe_status") or "unknown",
            "last_probe_status_label": self.dns_failover_probe_status_label(item.get("last_probe_status")),
            "last_probe_checked_at": item.get("last_probe_checked_at"),
            "last_probe_checked_at_display": (
                format_display_time(item.get("last_probe_checked_at")) if item.get("last_probe_checked_at") else "暂无"
            ),
            "last_probe_error": item.get("last_probe_error") or "",
            "consecutive_failures": int(item.get("consecutive_failures") or 0),
            "consecutive_successes": int(item.get("consecutive_successes") or 0),
            "last_switch_at": item.get("last_switch_at"),
            "last_switch_at_display": (
                format_display_time(item.get("last_switch_at")) if item.get("last_switch_at") else "暂无"
            ),
            "last_switch_reason": item.get("last_switch_reason") or "",
            "last_error": item.get("last_error") or "",
            "backup_label": config.backup_label,
            "control_plane_backup_xray_enabled": bool(CONTROL_PLANE_BACKUP_XRAY_ENABLED),
            "control_plane_backup_xray_label": "控制面备用 Xray",
            "backup_xray_mode": self._backup_xray_mode(),
            "fast_propagation_note": "已使用低 TTL；非代理记录建议保持 60 秒以尽快生效。",
            "peak_window": peak_window,
        }

    def dns_failover_config_error(self):
        config = self.failover_manager.config
        if not config.enabled:
            return ""
        if not config.record_type_supported():
            return "当前仅支持 A、AAAA、CNAME 记录。"
        missing = []
        if not config.probe_host:
            missing.append("DNS_FAILOVER_PROBE_HOST")
        if not config.probe_port:
            missing.append("DNS_FAILOVER_PROBE_PORT")
        if not config.api_token:
            missing.append("CF_API_TOKEN")
        if not config.zone_id:
            missing.append("CF_ZONE_ID")
        if not config.record_id:
            missing.append("CF_DNS_RECORD_ID")
        if not config.record_name:
            missing.append("CF_DNS_RECORD_NAME")
        if missing:
            return "缺少配置: " + ", ".join(missing)
        resolved_contents = self.resolve_dns_failover_contents()
        if resolved_contents["error"]:
            return resolved_contents["error"]
        peak_window = self.dns_failover_peak_window_status()
        if peak_window["enabled"] and not peak_window["configured"]:
            return peak_window["config_error"]
        return ""

    def resolve_dns_failover_contents(self):
        config = self.failover_manager.config
        primary_content = str(config.primary_content or "").strip()
        backup_content = str(config.backup_content or "").strip()
        error = ""
        if not primary_content:
            if self.node_controller.is_remote:
                error = "远端数据面启用 DNS 故障切换时，必须设置 DNS_FAILOVER_PRIMARY_CONTENT。"
            else:
                try:
                    primary_content = self.node_controller.resolve_public_ip(timeout_seconds=max(config.timeout, 3.0))
                except Exception as exc:
                    error = f"主数据面公网 IP 自动获取失败: {exc}"
        if not backup_content and not error and CONTROL_PLANE_BACKUP_XRAY_ENABLED:
            try:
                backup_content = self.public_ip_resolver(timeout=max(config.timeout, 3.0))
            except Exception as exc:
                error = str(exc)
        if not backup_content and not error:
            error = "请设置 DNS_FAILOVER_BACKUP_CONTENT，或启用 CONTROL_PLANE_BACKUP_XRAY_ENABLED。"
        return {
            "primary_content": primary_content,
            "backup_content": backup_content,
            "error": error,
        }

    def dns_failover_probe_status_label(self, status):
        if status == "healthy":
            return "探测成功"
        if status == "unhealthy":
            return "探测失败"
        return "未检测"

    def write_dns_failover_history(self, conn, event_type, event_status, target="", record_content="", detail=""):
        conn.execute(
            """
            INSERT INTO dns_failover_history (
                event_type, event_status, target, record_content, detail, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                event_status,
                target,
                record_content,
                detail[:500],
                utc_iso_now(),
            ),
        )
        conn.execute(
            """
            DELETE FROM dns_failover_history
            WHERE id NOT IN (
                SELECT id FROM dns_failover_history ORDER BY id DESC LIMIT 500
            )
            """
        )

    def update_dns_failover_state(self, conn, **values):
        allowed = {
            "current_target",
            "current_record_content",
            "current_record_ttl",
            "current_record_proxied",
            "last_probe_status",
            "last_probe_checked_at",
            "last_probe_error",
            "consecutive_failures",
            "consecutive_successes",
            "last_switch_at",
            "last_switch_reason",
            "last_error",
        }
        updates = {key: value for key, value in values.items() if key in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{column} = ?" for column in updates)
        conn.execute(
            f"UPDATE dns_failover_state SET {assignments} WHERE singleton_id = 1",
            list(updates.values()),
        )

    def get_dns_failover_state_row(self, conn):
        row = conn.execute("SELECT * FROM dns_failover_state WHERE singleton_id = 1").fetchone()
        if row is None:
            self.ensure_dns_failover_schema(conn)
            row = conn.execute("SELECT * FROM dns_failover_state WHERE singleton_id = 1").fetchone()
        return row

    def run_dns_failover_check(self, force=False):
        status = self.dns_failover_status()
        if not status["enabled"]:
            return status
        if not status["configured"]:
            emit_business_event(
                "dns_failover.checked",
                result="failure",
                actor_type="admin" if force else "system",
                resource_type="dns",
                error_code="configuration_missing",
                message=status.get("config_error", ""),
            )
            with self.write_lock:
                with self.repository.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    self.update_dns_failover_state(conn, last_error=status["config_error"])
                    self.write_dns_failover_history(conn, "probe", "skipped", detail=status["config_error"])
                    conn.commit()
            return self.dns_failover_status()

        probe = self.failover_manager.probe_once()
        probe_status = "healthy" if probe["ok"] else "unhealthy"
        emit_business_event(
            "dns_failover.checked",
            actor_type="admin" if force else "system",
            resource_type="dns",
            metadata={"probe_status": probe_status},
        )
        if not probe["ok"]:
            emit_business_event(
                "probe.failed",
                result="failure",
                actor_type="admin" if force else "system",
                resource_type="dns",
                error_code="probe_failed",
                message=probe.get("error", ""),
            )
        switch_result = None
        with self.write_lock:
            with self.repository.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = self.get_dns_failover_state_row(conn)
                current_target = str(row["current_target"] or "").strip() or "primary"
                if current_target not in {"primary", "backup"}:
                    inferred = self.failover_manager.current_target_from_content(
                        row["current_record_content"],
                        primary_content=status["primary_content"],
                        backup_content=status["backup_content"],
                    )
                    current_target = inferred if inferred in {"primary", "backup"} else "primary"
                consecutive_failures = int(row["consecutive_failures"] or 0)
                consecutive_successes = int(row["consecutive_successes"] or 0)
                if probe["ok"]:
                    consecutive_successes += 1
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    consecutive_successes = 0
                self.update_dns_failover_state(
                    conn,
                    last_probe_status=probe_status,
                    last_probe_checked_at=utc_iso_now(),
                    last_probe_error=probe["error"],
                    consecutive_failures=consecutive_failures,
                    consecutive_successes=consecutive_successes,
                    last_error="",
                )
                detail = probe["error"] or self.dns_failover_probe_status_label(probe_status)
                self.write_dns_failover_history(
                    conn,
                    "probe",
                    "ok" if probe["ok"] else "error",
                    target=current_target,
                    record_content=row["current_record_content"] or "",
                    detail=detail,
                )
                peak_window = self.dns_failover_peak_window_status()
                preferred_target = peak_window["preferred_target"] if peak_window["configured"] else "primary"
                transition = self.evaluate_dns_failover_transition(
                    current_target,
                    consecutive_failures,
                    consecutive_successes,
                    probe["ok"],
                    preferred_target=preferred_target,
                )
                conn.commit()

        if transition is not None:
            switch_result = self.switch_dns_target(transition["target"], reason=transition["reason"], auto=True)
        return self.dns_failover_status() if switch_result is None else switch_result

    def switch_dns_target(self, target, reason="manual_switch", auto=False):
        target = str(target or "").strip().lower()
        if target not in {"primary", "backup"}:
            raise ValidationError("target 仅支持 primary 或 backup。")
        status = self.dns_failover_status()
        if not status["enabled"]:
            raise ValidationError("DNS 故障切换未启用。")
        if not status["configured"]:
            raise ValidationError(status["config_error"] or "DNS 故障切换配置不完整。")

        primary_content = status["primary_content"]
        backup_content = status["backup_content"]

        try:
            record = self.failover_manager.get_record()
        except CloudflareApiError as exc:
            with self.write_lock:
                with self.repository.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    self.update_dns_failover_state(conn, last_error=str(exc))
                    self.write_dns_failover_history(
                        conn,
                        "switch",
                        "error",
                        target=target,
                        detail=str(exc),
                    )
                    conn.commit()
            if auto:
                emit_business_event(
                    "dns_failover.switched",
                    result="failure",
                    actor_type="system",
                    resource_type="dns",
                    error_code="provider_error",
                    message=str(exc),
                    exc=exc,
                )
            raise ValidationError(str(exc)) from exc
        current_content = str(record.get("content") or "").strip()
        current_target = self.failover_manager.current_target_from_content(
            current_content,
            primary_content=primary_content,
            backup_content=backup_content,
        )
        if current_target == target:
            with self.write_lock:
                with self.repository.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    self.update_dns_failover_state(
                        conn,
                        current_target=target,
                        current_record_content=current_content,
                        current_record_ttl=record.get("ttl"),
                        current_record_proxied=1 if record.get("proxied") else 0,
                        last_error="",
                    )
                    self.write_dns_failover_history(
                        conn,
                        "switch",
                        "noop",
                        target=target,
                        record_content=current_content,
                        detail="DNS 已在目标指向，无需重复更新。",
                    )
                    conn.commit()
            emit_business_event(
                "dns_failover.switched",
                actor_type="system" if auto else None,
                resource_type="dns",
                metadata={"target": target, "automatic": auto},
            )
            return self.dns_failover_status()

        try:
            updated = self.failover_manager.sync_target(
                target,
                primary_content=primary_content,
                backup_content=backup_content,
            )
        except CloudflareApiError as exc:
            with self.write_lock:
                with self.repository.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    self.update_dns_failover_state(conn, last_error=str(exc))
                    self.write_dns_failover_history(
                        conn,
                        "switch",
                        "error",
                        target=target,
                        record_content=current_content,
                        detail=str(exc),
                    )
                    conn.commit()
            if auto:
                emit_business_event(
                    "dns_failover.switched",
                    result="failure",
                    actor_type="system",
                    resource_type="dns",
                    error_code="provider_error",
                    message=str(exc),
                    exc=exc,
                )
            raise ValidationError(str(exc)) from exc

        updated_content = str(updated.get("content") or "").strip()
        updated_target = self.failover_manager.current_target_from_content(
            updated_content,
            primary_content=primary_content,
            backup_content=backup_content,
        )
        with self.write_lock:
            with self.repository.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self.update_dns_failover_state(
                    conn,
                    current_target=updated_target if updated_target in {"primary", "backup"} else target,
                    current_record_content=updated_content,
                    current_record_ttl=updated.get("ttl"),
                    current_record_proxied=1 if updated.get("proxied") else 0,
                    last_switch_at=utc_iso_now(),
                    last_switch_reason=reason,
                    last_error="",
                )
                self.write_dns_failover_history(
                    conn,
                    "switch",
                    "ok",
                    target=target,
                    record_content=updated_content,
                    detail="自动切换完成。" if auto else "手动切换完成。",
                )
                conn.commit()
        emit_business_event(
            "dns_failover.switched",
            actor_type="system" if auto else None,
            resource_type="dns",
            metadata={"target": target, "automatic": auto},
        )
        return self.dns_failover_status()

    def refresh_dns_failover_record_snapshot(self):
        status = self.dns_failover_status()
        if not status["enabled"] or not status["configured"]:
            return status
        try:
            record = self.failover_manager.get_record()
        except CloudflareApiError as exc:
            with self.write_lock:
                with self.repository.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    self.update_dns_failover_state(conn, last_error=str(exc))
                    conn.commit()
            return self.dns_failover_status()
        current_content = str(record.get("content") or "").strip()
        current_target = self.failover_manager.current_target_from_content(
            current_content,
            primary_content=status["primary_content"],
            backup_content=status["backup_content"],
        )
        with self.write_lock:
            with self.repository.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self.update_dns_failover_state(
                    conn,
                    current_target=current_target if current_target in {"primary", "backup"} else "primary",
                    current_record_content=current_content,
                    current_record_ttl=record.get("ttl"),
                    current_record_proxied=1 if record.get("proxied") else 0,
                    last_error="",
                )
                conn.commit()
        return self.dns_failover_status()
