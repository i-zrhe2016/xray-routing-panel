"""Transactional Xray configuration application orchestration."""

import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

from ..config import (
    AI_NODE_CONFIG_OUT,
    AI_NODE_PROBE_HOST,
    CONTROL_PLANE_BACKUP_UPSTREAM_URL,
    CONTROL_PLANE_BACKUP_XRAY_ENABLED,
    DATAPLANE_EXTERNAL_RELOADER_ENABLED,
    XRAY_CLIENT_CONFIG_PATH,
    XRAY_CONFIG_PATH,
    XRAY_ENV_FILE_PATH,
    XRAY_PANEL_PORTS_PATH,
)
from ..helpers import port_is_expired, utc_iso_now, utc_now
from ..observability.logging import emit_business_event
from .envfile import load_env_file
from .file_io import write_bytes_atomic, write_text_atomic
from .operation_lock import LockBusyError, exclusive_file_lock


class XrayApplyService:
    """Apply database-backed Xray changes as one external transaction.

    The service owns the ordering of artifact rendering, validation, remote
    synchronization, restarts, database commit and rollback.  It deliberately
    accepts generic repository and node capabilities so the operation does not
    need to reach through ``PanelState``.
    """

    def __init__(
        self,
        repository=None,
        node_controller=None,
        ports_service=None,
        traffic_service=None,
        ai_nodes=None,
        ai_node=None,
        write_lock=None,
    ):
        self.repository = repository
        self.node_controller = node_controller
        self.ports_service = ports_service
        self.traffic_service = traffic_service
        self.ai_nodes = dict(ai_nodes or {})
        self.ai_node = ai_node or next(iter(self.ai_nodes.values()), None)
        self.write_lock = write_lock or threading.Lock()
        self._last_backup_mode = None

    def bind_services(self, *, ports_service, traffic_service):
        """Attach domain services needed by the apply transaction."""
        self.ports_service = ports_service
        self.traffic_service = traffic_service

    @contextmanager
    def apply_lock(self):
        """Hold the cross-process lock for a caller-owned apply transaction."""
        with exclusive_file_lock(self._ai_manager_apply_lock_path()):
            yield

    def raise_apply_lock_busy(self, exc):
        """Translate a lock collision for callers that own the transaction."""
        self._raise_apply_lock_busy(exc)

    def _ai_manager_apply_lock_path(self):
        configured = os.environ.get("AI_DOMAIN_MANAGER_LOCK_PATH", "").strip()
        if configured:
            return Path(configured)
        return XRAY_CONFIG_PATH.with_name(".ai-domain-manager.lock")

    @staticmethod
    def _raise_apply_lock_busy(exc):
        raise RuntimeError("配置正在应用，请稍后重试。") from exc

    def disable_auto_stopped_ports(self, reload_xray=True):
        try:
            with (
                exclusive_file_lock(self._ai_manager_apply_lock_path()),
                self.write_lock,
                self.repository.connect() as conn,
            ):
                conn.execute("BEGIN IMMEDIATE")
                changed = self.disable_auto_stopped_ports_in_tx(conn)
                if changed:
                    self._persist_and_reload_locked(conn, reload_xray=reload_xray)
                else:
                    conn.commit()
                return changed
        except LockBusyError:
            # Dashboard reads and the maintenance worker should remain
            # available while the resident AI manager owns the apply lock. The
            # next maintenance/dashboard cycle will retry the cleanup.
            return 0

    def apply_mutation(self, operation):
        try:
            with exclusive_file_lock(self._ai_manager_apply_lock_path()), self.write_lock:
                self.traffic_service.sync_traffic_state_locked()
                with self.repository.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = operation(conn)
                        self.disable_auto_stopped_ports_in_tx(conn)
                        self._persist_and_reload_locked(conn, reload_xray=True)
                        return result
                    except Exception:
                        conn.rollback()
                        raise
        except LockBusyError as exc:
            self._raise_apply_lock_busy(exc)

    def disable_auto_stopped_ports_in_tx(self, conn):
        now_dt = utc_now()
        now_text = now_dt.isoformat(timespec="seconds")
        cleaned = self.ports_service.cleanup_expired_ports_in_tx(conn)
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
        try:
            with exclusive_file_lock(self._ai_manager_apply_lock_path()):
                return self.persist_and_reload_locked(conn, reload_xray)
        except LockBusyError as exc:
            self._raise_apply_lock_busy(exc)

    def persist_and_reload_locked(self, conn, reload_xray):
        """Apply artifacts while the caller owns the DB and apply locks."""
        return self._persist_and_reload_locked(conn, reload_xray)

    def _persist_and_reload_locked(self, conn, reload_xray):
        previous_panel_ports = (
            XRAY_PANEL_PORTS_PATH.read_text(encoding="utf-8") if XRAY_PANEL_PORTS_PATH.exists() else None
        )
        previous_config = XRAY_CONFIG_PATH.read_text(encoding="utf-8") if XRAY_CONFIG_PATH.exists() else None
        subscription_paths = [XRAY_CLIENT_CONFIG_PATH, XRAY_CONFIG_PATH.parent / "client-share.txt"]
        previous_subscriptions = {path: path.read_bytes() if path.exists() else None for path in subscription_paths}
        backup_config_path = XRAY_CONFIG_PATH.parent / "config-backup.json"
        previous_backup_config = backup_config_path.read_text(encoding="utf-8") if backup_config_path.exists() else None
        panel_ports_payload = self.render_panel_ports_payload(conn)
        backup_restart_attempted = False
        data_plane_restart_attempted = False
        pending_apply_path = XRAY_CONFIG_PATH.with_name(XRAY_CONFIG_PATH.name + ".pending-apply")
        pending_apply_preexisting = pending_apply_path.exists()
        pending_marker_created = False
        try:
            self.write_json_file(XRAY_PANEL_PORTS_PATH, panel_ports_payload)
            self.render_xray_config()
            self.xray_config_test()
            if reload_xray and DATAPLANE_EXTERNAL_RELOADER_ENABLED:
                # Create the marker only after the complete config has been
                # rendered and validated, so the watcher never restarts an
                # old config merely because an apply is starting.
                pending_apply_path.parent.mkdir(parents=True, exist_ok=True)
                pending_apply_path.touch()
                pending_marker_created = not pending_apply_preexisting
            if CONTROL_PLANE_BACKUP_XRAY_ENABLED:
                backup_restart_attempted = True
                if not self.restart_backup_xray():
                    raise RuntimeError("控制面备用 Xray 重载失败，端口变更未提交。")
            # In external-reloader mode the watcher validates, restarts, and
            # removes the marker after the new process is observable. In direct
            # mode the panel owns the restart itself.
            if reload_xray and not DATAPLANE_EXTERNAL_RELOADER_ENABLED:
                data_plane_restart_attempted = True
                if not self.restart_data_plane():
                    raise RuntimeError("数据面重载失败，端口变更未提交。")
            if not DATAPLANE_EXTERNAL_RELOADER_ENABLED:
                pending_apply_path.unlink(missing_ok=True)
            conn.commit()
        except Exception:
            conn.rollback()
            if pending_marker_created and not DATAPLANE_EXTERNAL_RELOADER_ENABLED:
                pending_apply_path.unlink(missing_ok=True)
            for path, content in previous_subscriptions.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    write_bytes_atomic(path, content)
            if previous_panel_ports is None:
                XRAY_PANEL_PORTS_PATH.unlink(missing_ok=True)
            else:
                write_text_atomic(XRAY_PANEL_PORTS_PATH, previous_panel_ports)
            if previous_config is None:
                XRAY_CONFIG_PATH.unlink(missing_ok=True)
            else:
                write_text_atomic(XRAY_CONFIG_PATH, previous_config)
            if previous_backup_config is None:
                backup_config_path.unlink(missing_ok=True)
            else:
                write_text_atomic(backup_config_path, previous_backup_config)
            if backup_restart_attempted:
                try:
                    if not self.restart_backup_xray():
                        emit_business_event(
                            "node.backup.rollback_failed",
                            result="failure",
                            actor_type="system",
                            resource_type="node",
                            resource_id="control_plane_backup",
                        )
                except Exception as exc:  # noqa: BLE001 - rollback must preserve the original apply failure
                    emit_business_event(
                        "node.backup.rollback_failed",
                        result="failure",
                        actor_type="system",
                        resource_type="node",
                        resource_id="control_plane_backup",
                        exc=exc,
                    )
            try:
                if self.node_controller.supports_sync():
                    self.node_controller.sync_generated_files(validate_config=True)
                # A timeout can occur after the node has already loaded the new
                # config. Restore its running state as well as the files.
                if data_plane_restart_attempted and not self.restart_data_plane():
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
        try:
            with exclusive_file_lock(self._ai_manager_apply_lock_path()):
                return self._write_current_config_locked()
        except LockBusyError as exc:
            self._raise_apply_lock_busy(exc)

    def _write_current_config_locked(self):
        with self.repository.connect() as conn:
            self.write_json_file(XRAY_PANEL_PORTS_PATH, self.render_panel_ports_payload(conn))
        self.render_xray_config()
        try:
            changed_paths = self.xray_config_test() or []
        except RuntimeError as exc:
            if not self.node_controller.is_remote:
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

        if DATAPLANE_EXTERNAL_RELOADER_ENABLED:
            # The external watcher owns process restarts. It observes the
            # atomically rendered config (and pending marker when applicable).
            return
        config_path = str(self.node_controller.config.config_path or "")
        if config_path not in changed_paths or not self.node_controller.supports_restart():
            return
        try:
            if not self.restart_data_plane():
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
        write_text_atomic(path, json.dumps(payload, indent=2, ensure_ascii=True) + "\n")

    def render_xray_config(self):
        try:
            self.sync_data_plane_dynamic_routing()
        except RuntimeError as exc:
            if not self.node_controller.is_remote:
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
            backup_upstream_url = self.resolve_backup_upstream_url()
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
        if any(node.supports_sync() for node in self.ai_nodes.values()):
            command += [
                "--ai-node-config-out",
                str(AI_NODE_CONFIG_OUT),
            ]
        self.run_command(command, "Xray 配置渲染失败")

    def sync_data_plane_dynamic_routing(self):
        if not self.node_controller.supports_dynamic_routing_pull():
            return False
        return self.node_controller.sync_dynamic_routing_from_remote()

    def sync_data_plane_artifacts(self):
        if not self.node_controller.supports_sync():
            return []
        return self.node_controller.sync_generated_files(validate_config=True)

    def xray_config_test(self):
        if self.node_controller.supports_sync():
            return self.sync_data_plane_artifacts()
        self.node_controller.test_config()
        return []

    def restart_data_plane(self):
        return self.node_controller.restart()

    def resolve_backup_upstream_url(self):
        """Determine the backup Xray's upstream URL based on AI reachability."""
        if CONTROL_PLANE_BACKUP_UPSTREAM_URL:
            return CONTROL_PLANE_BACKUP_UPSTREAM_URL
        if not self.ai_nodes:
            return ""
        if not self.ai_node_running():
            return ""
        return self.derive_ai_node_share_url()

    def derive_ai_node_share_url(self):
        """Build a vless:// share URL targeting the managed AI node."""
        try:
            values = load_env_file(XRAY_ENV_FILE_PATH)
        except Exception:  # noqa: BLE001 - optional environment data must not break backup rendering
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
        params = urlencode(
            {
                "encryption": "none",
                "flow": values["XRAY_FLOW"],
                "security": "reality",
                "sni": values["XRAY_SERVER_NAME"],
                "fp": values["XRAY_FINGERPRINT"],
                "pbk": values["XRAY_REALITY_PUBLIC_KEY"],
                "sid": values["XRAY_REALITY_SHORT_ID"],
                "type": "tcp",
                "headerType": "none",
            }
        )
        return f"vless://{values['XRAY_CLIENT_UUID']}@{host}:{port}?{params}#ai-node"

    def ai_node_running(self):
        return any(self._node_running(controller) for controller in self.ai_nodes.values())

    @staticmethod
    def _node_running(controller):
        try:
            return bool(controller.is_running())
        except (OSError, RuntimeError, ValueError):
            return False

    def backup_xray_mode(self):
        """Return the current backup Xray mode: 'relay', 'direct', or 'disabled'."""
        if not CONTROL_PLANE_BACKUP_XRAY_ENABLED:
            return "disabled"
        url = self.resolve_backup_upstream_url()
        return "relay" if url else "direct"

    def sync_backup_xray_mode(self):
        """Re-render and restart the backup Xray when its mode changes."""
        if not CONTROL_PLANE_BACKUP_XRAY_ENABLED:
            return False
        if not self.ai_nodes:
            return False
        try:
            with exclusive_file_lock(self._ai_manager_apply_lock_path()):
                current_mode = self.backup_xray_mode()
                previous_mode = self._last_backup_mode
                if previous_mode is not None and previous_mode == current_mode:
                    return False
                self.render_xray_config()
                if not self.restart_backup_xray():
                    raise RuntimeError("控制面备用 Xray 重载失败。")
                # Only record the mode after both rendering and restart have
                # succeeded; a failed maintenance attempt must be retried.
                self._last_backup_mode = current_mode
                return True
        except LockBusyError:
            # The manager owns the same runtime files. Let the next maintenance
            # cycle retry instead of treating a lock collision as a mode sync.
            return False

    def restart_backup_xray(self):
        """Restart the local backup Xray container."""
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
        except Exception:  # noqa: BLE001 - restart failure is reported as an unavailable backup node
            return False

    def run_command(self, command, error_prefix, timeout=None):
        completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)
        if completed.returncode == 0:
            return completed
        detail = completed.stderr.strip() or completed.stdout.strip() or "未知错误"
        raise RuntimeError(f"{error_prefix}: {detail}")
