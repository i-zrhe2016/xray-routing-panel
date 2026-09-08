"""Backend for an Xray process managed by a local Docker container."""

from __future__ import annotations

from .backend import NodeBackend


class DockerBackend(NodeBackend):
    mode = "docker"
    native_restart = True
    stats_supported = True

    def display_target(self):
        return self.config.container_name

    def _container_exists(self):
        if not self.config.container_name:
            return False
        try:
            self.run_subprocess(
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
            completed = self.run_subprocess(
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
        del timeout_seconds
        return self._docker_running()

    def test_config(self, config_path=None):
        active_config_path = str(config_path or self.config.config_path or "").strip()
        if not active_config_path or not self._container_exists() or not self._docker_running():
            return None
        return self.run_subprocess(
            [
                self.config.docker_bin,
                "exec",
                self.config.container_name,
                self.config.xray_bin,
                "run",
                "-test",
                "-config",
                active_config_path,
            ],
            f"{self.config.label} 配置校验失败",
        )

    def restart(self):
        if self.config.restart_command:
            self.run_subprocess(
                ["sh", "-lc", self.config.restart_command],
                f"{self.config.label} 重启失败",
            )
            return True
        if not self._container_exists():
            return False
        action = "start" if not self._docker_running() else "restart"
        self.run_subprocess(
            [self.config.docker_bin, action, self.config.container_name],
            f"{self.config.label} 重启失败",
        )
        return True

    def run_statsquery(self, timeout_seconds, pattern):
        if not self.supports_stats() or not self.config.container_name:
            return None
        return self.run_subprocess(
            [
                self.config.docker_bin,
                "exec",
                self.config.container_name,
                self.config.xray_bin,
                "api",
                "statsquery",
                f"--server={self.config.api_server}",
                "-timeout",
                str(timeout_seconds),
                "-pattern",
                pattern,
                "-reset",
            ],
            f"{self.config.label} 流量查询失败",
        )


__all__ = ["DockerBackend"]
