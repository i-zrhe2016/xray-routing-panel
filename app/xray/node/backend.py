"""Shared node configuration and the backend contract.

Backends own command and transport details.  The controller only selects one
backend and delegates this interface.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import probes


@dataclass(frozen=True)
class DataPlaneConfig:
    role: str
    label: str
    api_server: str = ""
    xray_bin: str = "/usr/local/bin/xray"
    local_bin: str = ""
    docker_bin: str = "docker"
    container_name: str = ""
    restart_command: str = ""
    ssh_target: str = ""
    ssh_bin: str = "ssh"
    ssh_options: tuple[str, ...] = ()
    ssh_known_hosts_file: str = ""
    remote_command_timeout: float = 8.0
    config_path: str = ""
    dynamic_routing_path: str = ""
    ai_report_path: str = ""
    panel_db_path: str = ""
    access_log_path: str = ""
    panel_ports_path: str = ""
    source_config_path: Path | None = None
    source_dynamic_routing_path: Path | None = None
    source_ai_report_path: Path | None = None
    source_panel_ports_path: Path | None = None
    upstream_host: str = ""
    upstream_port: int | None = None


class NodeBackend:
    """Interface implemented by SSH, Docker, local, and unmanaged nodes."""

    mode = "unmanaged"
    is_remote = False
    native_restart = False
    remote_sync = False
    remote_snapshot = False
    stats_supported = False
    logs_supported = False

    def __init__(self, config: DataPlaneConfig, run_subprocess=None, run_remote=None):
        self.config = config
        self._subprocess_runner = run_subprocess or self.execute_subprocess
        self._remote_runner = run_remote or self.execute_remote

    def bind_runners(self, run_subprocess=None, run_remote=None, test_config=None):
        """Bind controller-compatible hooks while keeping execution in a backend."""

        if run_subprocess is not None:
            self._subprocess_runner = run_subprocess
        if run_remote is not None:
            self._remote_runner = run_remote
        if test_config is not None and hasattr(self, "files"):
            self.files._test_config = test_config

    def execute_subprocess(self, command, error_prefix, timeout=None, input_text=None):
        try:
            completed = subprocess.run(
                command,
                input=input_text,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            limit = f"{timeout:g} 秒" if timeout is not None else "配置的超时时间"
            raise RuntimeError(f"{error_prefix}: 命令执行超时（{limit}）。") from exc
        if completed.returncode == 0:
            return completed
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise RuntimeError(f"{error_prefix}: {detail}")

    def execute_remote(self, args, error_prefix, timeout=None, input_text=None):
        raise RuntimeError(f"{self.config.label} 不支持远程命令。")

    def run_subprocess(self, command, error_prefix, timeout=None, input_text=None):
        return self._subprocess_runner(
            command,
            error_prefix,
            timeout=timeout,
            input_text=input_text,
        )

    def run_remote(self, args, error_prefix, timeout=None, input_text=None):
        return self._remote_runner(
            args,
            error_prefix,
            timeout=timeout,
            input_text=input_text,
        )

    def is_configured(self):
        if self.mode != "unmanaged":
            return True
        return bool(self.config.upstream_host and self.config.upstream_port)

    def supports_restart(self):
        return bool(self.config.restart_command or self.native_restart)

    def supports_sync(self):
        return bool(self.remote_sync and self.config.config_path and self.config.source_config_path)

    def supports_dynamic_routing_pull(self):
        return bool(self.remote_sync and self.config.dynamic_routing_path and self.config.source_dynamic_routing_path)

    def supports_ai_report_pull(self):
        return bool(self.remote_sync and self.config.ai_report_path and self.config.source_ai_report_path)

    def supports_ai_domains_snapshot_pull(self):
        return bool(self.remote_snapshot and self.config.panel_db_path)

    def supports_stats(self):
        return bool(self.stats_supported and self.config.api_server)

    def supports_logs(self):
        return bool(self.logs_supported and self.config.access_log_path)

    def display_target(self):
        if self.config.upstream_host and self.config.upstream_port:
            return f"{self.config.upstream_host}:{self.config.upstream_port}"
        return ""

    def probe_tcp_endpoint(self, host, port, timeout_seconds):
        return probes.probe_tcp_endpoint(
            host,
            port,
            timeout_seconds,
            label=self.config.label,
            remote_runner=self.run_remote if self.is_remote else None,
            remote_command_timeout=self.config.remote_command_timeout,
        )

    def probe_reality_endpoint(self, host, port, server_name, timeout_seconds):
        return probes.probe_reality_endpoint(
            host,
            port,
            server_name,
            timeout_seconds,
            label=self.config.label,
            remote_runner=self.run_remote if self.is_remote else None,
            remote_command_timeout=self.config.remote_command_timeout,
        )

    def resolve_public_ip(self, timeout_seconds=5):
        runner = self.run_remote if self.is_remote else self.run_subprocess
        return probes.resolve_public_ip(runner, self.config.label, timeout_seconds)

    def is_running(self, timeout_seconds=1):
        raise NotImplementedError

    def sync_generated_files(self, validate_config=False):
        return []

    def sync_text_file_from_remote(
        self,
        remote_path,
        local_path,
        error_prefix,
        preserve_local_when_missing=False,
    ):
        raise RuntimeError(f"{self.config.label} 不支持远程文件读取。")

    def sync_dynamic_routing_from_remote(self):
        return False

    def remove_dynamic_routing(self):
        removed = False
        local_path = self.config.source_dynamic_routing_path
        if local_path is not None:
            removed = local_path.unlink(missing_ok=True) or removed
        return removed

    def sync_ai_report_from_remote(self):
        return False

    def read_ai_domains_snapshot_from_remote(self):
        return {"exists": False, "ai_domains": []}

    def read_live_server_config(self):
        """Read the best available local or generated Xray config."""

        result = {"available": False, "source": "", "config": None, "error": ""}
        candidate = None
        source_label = ""
        if self.config.config_path and Path(self.config.config_path).is_file():
            candidate = Path(self.config.config_path)
            source_label = f"数据面本地配置 ({self.config.config_path})"
        elif self.config.source_config_path and self.config.source_config_path.is_file():
            candidate = self.config.source_config_path
            source_label = f"面板生成的配置 ({self.config.source_config_path})"
        if candidate is None:
            result["error"] = "未找到可比对的数据面配置（unmanaged 模式且文件缺失）。"
            return result
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            result["error"] = str(exc)[:300]
            return result
        result["source"] = source_label
        try:
            result["config"] = json.loads(text or "{}")
        except json.JSONDecodeError as exc:
            result["error"] = f"配置 JSON 解析失败：{exc}"
            return result
        result["available"] = True
        return result

    def test_config(self, config_path=None):
        return None

    def restart(self):
        return False

    def read_access_log_delta(self, recorded_inode, offset, since_epoch=None):
        return {"exists": False, "inode": "", "offset": 0, "data": ""}

    def run_statsquery(self, timeout_seconds, pattern):
        return None

    def status_summary(self):
        xray_running = None
        error = ""
        configured = self.is_configured()
        if configured:
            try:
                xray_running = self.is_running()
            except Exception as exc:  # noqa: BLE001 - status must expose backend failures
                error = str(exc)
        return {
            "role": self.config.role,
            "label": self.config.label,
            "configured": configured,
            "reachable": bool(xray_running) if xray_running is not None else False,
            "xray_running": xray_running,
            "management_target": self.display_target(),
            "api_server": self.config.api_server,
            "config_path": self.config.config_path,
            "access_log_path": self.config.access_log_path,
            "supports_sync": self.supports_sync(),
            "supports_restart": self.supports_restart(),
            "last_error": error,
        }


class UnmanagedBackend(NodeBackend):
    """Fallback backend for an externally managed upstream endpoint."""

    def is_running(self, timeout_seconds=1):
        endpoint = probes.parse_api_endpoint(self.config.api_server)
        if endpoint is not None:
            return probes.socket_running(endpoint, timeout_seconds)
        if self.config.upstream_host and self.config.upstream_port:
            return probes.socket_running(
                (self.config.upstream_host, int(self.config.upstream_port)),
                timeout_seconds,
            )
        return False

    def restart(self):
        if not self.config.restart_command:
            return False
        self.run_subprocess(
            ["sh", "-lc", self.config.restart_command],
            f"{self.config.label} 重启失败",
        )
        return True


__all__ = ["DataPlaneConfig", "NodeBackend", "UnmanagedBackend"]
