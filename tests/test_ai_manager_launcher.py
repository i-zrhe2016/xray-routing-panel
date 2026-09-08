import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from app.xray.ai_routing.launcher import AiDomainManagerRunner
from app.xray.ai_routing.manager import LOCK_BUSY_EXIT_CODE


def test_launcher_builds_local_one_shot_command():
    runner = AiDomainManagerRunner(
        execution_mode="local",
        project_root=Path("/tmp/panel"),
        python_executable="python-test",
    )

    with patch("app.xray.ai_routing.launcher.subprocess.run", return_value=Mock(returncode=0)) as run:
        runner.run("forced_fallback")

    assert run.call_args.args[0] == [
        "python-test",
        "-m",
        "app.xray.ai_routing.runner",
        "--once",
        "--manual-mode",
        "forced_fallback",
        "--manual-lock-held",
    ]
    assert run.call_args.kwargs["cwd"] == "/tmp/panel"
    assert run.call_args.kwargs["timeout"] == 240


def test_launcher_builds_container_command():
    runner = AiDomainManagerRunner(
        execution_mode="container",
        container_name="ai-manager",
        docker_bin="docker-test",
    )

    with patch("app.xray.ai_routing.launcher.subprocess.run", return_value=Mock(returncode=0)) as run:
        runner.run()

    assert run.call_args.args[0] == [
        "docker-test",
        "exec",
        "ai-manager",
        "python3",
        "-m",
        "app.xray.ai_routing.runner",
        "--once",
    ]
    assert "cwd" not in run.call_args.kwargs


@pytest.mark.parametrize(
    ("completed", "expected"),
    [
        (subprocess.CompletedProcess([], LOCK_BUSY_EXIT_CODE, stdout="", stderr=""), "正在应用配置"),
        (subprocess.CompletedProcess([], 1, stdout="", stderr="manager failed"), "manager failed"),
    ],
)
def test_launcher_preserves_failure_messages(completed, expected):
    runner = AiDomainManagerRunner(execution_mode="local", project_root="/tmp/panel")

    with patch("app.xray.ai_routing.launcher.subprocess.run", return_value=completed), pytest.raises(
        RuntimeError, match=expected
    ):
        runner.run()


def test_launcher_translates_timeout():
    runner = AiDomainManagerRunner(execution_mode="local", project_root="/tmp/panel")

    with patch(
        "app.xray.ai_routing.launcher.subprocess.run",
        side_effect=subprocess.TimeoutExpired([sys.executable], 240),
    ), pytest.raises(RuntimeError, match="重算超时"):
        runner.run()
