"""SSH backend for a remotely managed Xray node."""

from __future__ import annotations

import json

from . import probes
from .backend import NodeBackend
from .files import RemoteFileOperations, join_shell_args


class SSHBackend(NodeBackend):
    mode = "ssh"
    is_remote = True
    native_restart = False
    remote_sync = True
    remote_snapshot = True
    stats_supported = True
    logs_supported = True

    def __init__(self, config, run_subprocess=None, run_remote=None):
        super().__init__(config, run_subprocess=run_subprocess, run_remote=run_remote)
        self.files = RemoteFileOperations(
            config,
            self._run_remote,
            self.test_config,
        )

    def display_target(self):
        return self.config.ssh_target

    def supports_restart(self):
        return bool(self.config.restart_command or self.config.container_name)

    def execute_remote(self, args, error_prefix, timeout=None, input_text=None):
        if timeout is None:
            timeout = self.config.remote_command_timeout
        remote_command = join_shell_args(args)
        command = [
            self.config.ssh_bin,
            *self._normalized_ssh_options(),
            self.config.ssh_target,
            remote_command,
        ]
        return self.execute_subprocess(
            command,
            error_prefix,
            timeout=timeout,
            input_text=input_text,
        )

    def _run_remote(self, args, error_prefix, timeout=None, input_text=None):
        return self.run_remote(
            args,
            error_prefix,
            timeout=timeout,
            input_text=input_text,
        )

    def _normalized_ssh_options(self):
        options = list(self.config.ssh_options or ())
        known_hosts_file = str(self.config.ssh_known_hosts_file or "").strip()

        # These options are appended after user-provided options so an old
        # environment value cannot re-enable key authentication or override
        # the direct internal-network connection policy.
        forced_keys = {
            "batchmode",
            "challengeresponseauthentication",
            "identitiesonly",
            "kbdinteractiveauthentication",
            "passwordauthentication",
            "pubkeyauthentication",
            "preferredauthentications",
            "stricthostkeychecking",
            "userknownhostsfile",
        }

        normalized = []
        index = 0
        while index < len(options):
            token = str(options[index])
            lowered = token.lower()
            if token == "-i":
                index += 2
                continue
            if lowered.startswith("-i") and len(token) > 2:
                index += 1
                continue
            if token == "-o" and index + 1 < len(options):
                option = str(options[index + 1])
                option_lowered = option.lower()
                option_key = option_lowered.split("=", 1)[0].strip()
                if option_key == "identityfile" or option_key in forced_keys:
                    index += 2
                    continue
                normalized.extend((token, option))
                index += 2
                continue
            option_key = lowered.split("=", 1)[0].strip()
            if option_key == "identityfile" or option_key in forced_keys:
                index += 1
                continue
            normalized.append(token)
            index += 1

        normalized.extend(
            (
                "-o",
                "BatchMode=no",
                "-o",
                "PubkeyAuthentication=no",
                "-o",
                "PreferredAuthentications=password,keyboard-interactive",
                "-o",
                "PasswordAuthentication=yes",
                "-o",
                "KbdInteractiveAuthentication=yes",
                "-o",
                "ChallengeResponseAuthentication=yes",
                "-o",
                "StrictHostKeyChecking=yes",
            )
        )
        if known_hosts_file:
            normalized.extend(("-o", f"UserKnownHostsFile={known_hosts_file}"))
        return tuple(normalized)

    def _container_exists(self):
        if not self.config.container_name:
            return False
        try:
            self._run_remote(
                [self.config.docker_bin, "container", "inspect", self.config.container_name],
                f"{self.config.label} 容器检查失败",
            )
            return True
        except (OSError, RuntimeError):
            return False

    def _docker_running(self):
        if not self._container_exists():
            return False
        try:
            completed = self._run_remote(
                [
                    self.config.docker_bin,
                    "inspect",
                    "--format",
                    "{{.State.Running}}",
                    self.config.container_name,
                ],
                f"{self.config.label} 容器状态检查失败",
            )
            return completed.stdout.strip().lower() == "true"
        except (OSError, RuntimeError):
            return False

    def is_running(self, timeout_seconds=1):
        endpoint = probes.parse_api_endpoint(self.config.api_server)
        return probes.remote_socket_running(
            self._run_remote,
            endpoint,
            self.config.label,
            timeout_seconds=timeout_seconds,
            remote_command_timeout=self.config.remote_command_timeout,
        )

    def sync_generated_files(self, validate_config=False):
        if not self.supports_sync():
            return []
        return self.files.sync_generated_files(validate_config=validate_config)

    def sync_text_file_from_remote(
        self,
        remote_path,
        local_path,
        error_prefix,
        preserve_local_when_missing=False,
    ):
        return self.files.sync_text_file_from_remote(
            remote_path,
            local_path,
            error_prefix,
            preserve_local_when_missing=preserve_local_when_missing,
        )

    def sync_dynamic_routing_from_remote(self):
        if not self.supports_dynamic_routing_pull():
            return False
        return self.files.sync_dynamic_routing_from_remote()

    def remove_dynamic_routing(self):
        removed = super().remove_dynamic_routing()
        if self.config.dynamic_routing_path:
            self.files.delete_remote_file(
                self.config.dynamic_routing_path,
                f"{self.config.label} 动态路由删除失败",
            )
            removed = True
        return removed

    def sync_ai_report_from_remote(self):
        if not self.supports_ai_report_pull():
            return False
        return self.files.sync_ai_report_from_remote()

    def read_ai_domains_snapshot_from_remote(self):
        if not self.supports_ai_domains_snapshot_pull():
            return {"exists": False, "ai_domains": []}
        return self.files.read_ai_domains_snapshot_from_remote()

    def read_live_server_config(self):
        result = {"available": False, "source": "", "config": None, "error": ""}
        if not self.config.config_path:
            return super().read_live_server_config()
        try:
            payload = self.files.read_remote_text(
                self.config.config_path,
                f"{self.config.label} 配置读取失败",
            )
        except RuntimeError as exc:
            result["error"] = str(exc)[:300]
            return result
        if not payload["exists"]:
            result["error"] = f"数据面未找到配置文件：{self.config.config_path}"
            return result
        text = payload["data"]
        result["source"] = f"数据面实时配置 (ssh:{self.config.config_path})"
        try:
            result["config"] = json.loads(text or "{}")
        except json.JSONDecodeError as exc:
            result["error"] = f"配置 JSON 解析失败：{exc}"
            return result
        result["available"] = True
        return result

    def test_config(self, config_path=None):
        active_config_path = str(config_path or self.config.config_path or "").strip()
        if not active_config_path:
            return None
        if self.config.container_name:
            return self._validate_config_in_remote_container(active_config_path)
        return self._run_remote(
            [self.config.xray_bin, "run", "-test", "-config", active_config_path],
            f"{self.config.label} 配置校验失败",
        )

    def _validate_config_in_remote_container(self, host_config_path):
        """Validate a host config with the exact Xray binary in the container."""

        container = self.config.container_name
        scratch = "/tmp/.xray-config-validate.json"
        self._run_remote(
            [self.config.docker_bin, "cp", host_config_path, f"{container}:{scratch}"],
            f"{self.config.label} 配置校验文件注入失败",
        )
        return self._run_remote(
            [
                self.config.docker_bin,
                "exec",
                container,
                self.config.xray_bin,
                "run",
                "-test",
                "-config",
                scratch,
            ],
            f"{self.config.label} 配置校验失败",
        )

    def restart(self):
        if self.config.restart_command:
            self._run_remote(
                ["sh", "-lc", self.config.restart_command],
                f"{self.config.label} 重启失败",
            )
            return True
        if not self.config.container_name or not self._container_exists():
            return False
        action = "start" if not self._docker_running() else "restart"
        self._run_remote(
            [self.config.docker_bin, action, self.config.container_name],
            f"{self.config.label} 重启失败",
        )
        return True

    def read_access_log_delta(self, recorded_inode, offset, since_epoch=None):
        if not self.supports_logs():
            return super().read_access_log_delta(recorded_inode, offset, since_epoch)
        return self.files.read_access_log_delta(recorded_inode, offset, since_epoch)

    def run_statsquery(self, timeout_seconds, pattern):
        if not self.supports_stats():
            return None
        command = [
            self.config.xray_bin,
            "api",
            "statsquery",
            f"--server={self.config.api_server}",
            "-timeout",
            str(timeout_seconds),
            "-pattern",
            pattern,
            "-reset",
        ]
        if self.config.container_name:
            command = [
                self.config.docker_bin,
                "exec",
                self.config.container_name,
                *command,
            ]
        return self._run_remote(command, f"{self.config.label} 流量查询失败")


__all__ = ["SSHBackend"]
