"""Process boundary for invoking the AI domain manager."""

import subprocess
import sys
from pathlib import Path

from .manager import LOCK_BUSY_EXIT_CODE


class AiDomainManagerRunner:
    """Build and execute one-shot AI domain-manager commands."""

    def __init__(
        self,
        *,
        execution_mode,
        container_name="",
        docker_bin="docker",
        timeout_seconds=240,
        project_root=None,
        python_executable=None,
    ):
        self.execution_mode = execution_mode
        self.container_name = container_name
        self.docker_bin = docker_bin
        self.timeout_seconds = timeout_seconds
        self.project_root = Path(project_root) if project_root is not None else None
        self.python_executable = python_executable or sys.executable

    def _local_project_root(self):
        if self.project_root is not None:
            return self.project_root
        project_root = Path(__file__).resolve().parents[3]
        if not (project_root / "app").is_dir():
            project_root = Path(__file__).resolve().parents[2]
        return project_root

    def build_command(self, manual_mode=None):
        if self.execution_mode == "local":
            command = [self.python_executable, "-m", "app.xray.ai_routing.runner", "--once"]
        else:
            if not self.container_name:
                raise RuntimeError("AI 域名管理器容器未配置。")
            command = [
                self.docker_bin,
                "exec",
                self.container_name,
                "python3",
                "-m",
                "app.xray.ai_routing.runner",
                "--once",
            ]
        if manual_mode is not None:
            command.extend(("--manual-mode", str(manual_mode), "--manual-lock-held"))
        return command

    def run(self, manual_mode=None):
        command = self.build_command(manual_mode=manual_mode)
        run_options = {}
        if self.execution_mode == "local":
            run_options["cwd"] = str(self._local_project_root())
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
                **run_options,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("AI 路由重算超时，保持原有实际路由。") from exc
        if completed.returncode == LOCK_BUSY_EXIT_CODE:
            raise RuntimeError("AI 路由正在应用配置，请稍后重试。")
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "AI 域名管理器执行失败").strip()
            raise RuntimeError(detail[-500:])
