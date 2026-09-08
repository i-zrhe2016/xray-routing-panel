"""Backend for an Xray process managed directly on the local host."""

from __future__ import annotations

import os
import shutil

from . import probes
from .backend import NodeBackend


def resolve_local_bin(raw):
    text = str(raw or "").strip()
    if not text:
        return None
    if "/" not in text:
        return shutil.which(text)
    return text if os.path.isfile(text) else None


class LocalBackend(NodeBackend):
    mode = "local"
    stats_supported = True

    def __init__(self, config, run_subprocess=None, run_remote=None):
        super().__init__(config, run_subprocess=run_subprocess, run_remote=run_remote)
        self.local_bin = resolve_local_bin(config.local_bin)

    def display_target(self):
        return self.local_bin or self.config.local_bin

    def is_running(self, timeout_seconds=1):
        return probes.socket_running(
            probes.parse_api_endpoint(self.config.api_server),
            timeout_seconds,
        )

    def test_config(self, config_path=None):
        active_config_path = str(config_path or self.config.config_path or "").strip()
        if not active_config_path or not self.local_bin:
            return None
        return self.run_subprocess(
            [self.local_bin, "run", "-test", "-config", active_config_path],
            f"{self.config.label} 配置校验失败",
        )

    def restart(self):
        if not self.config.restart_command:
            return False
        self.run_subprocess(
            ["sh", "-lc", self.config.restart_command],
            f"{self.config.label} 重启失败",
        )
        return True

    def run_statsquery(self, timeout_seconds, pattern):
        if not self.supports_stats() or not self.local_bin:
            return None
        return self.run_subprocess(
            [
                self.local_bin,
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


__all__ = ["LocalBackend", "resolve_local_bin"]
