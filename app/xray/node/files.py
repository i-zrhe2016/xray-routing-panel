"""File transfer and incremental log operations for managed nodes."""

from __future__ import annotations

import json
import shlex
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.xray.file_io import write_text_atomic

REMOTE_FILE_DELTA_SCRIPT = """
from datetime import datetime, timezone
import json
import os
import sys

path = sys.argv[1]
recorded_inode = sys.argv[2]
offset = int(sys.argv[3])
since_epoch = None
if len(sys.argv) > 4 and sys.argv[4]:
    try:
        since_epoch = float(sys.argv[4])
    except ValueError:
        since_epoch = None


def is_after_cutoff(line):
    if since_epoch is None:
        return True
    parts = line.split(" ", 2)
    if len(parts) < 2:
        return False
    timestamp = f"{parts[0]} {parts[1]}"
    for format_string in ("%Y/%m/%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(timestamp, format_string).replace(tzinfo=timezone.utc)
            return parsed.timestamp() >= since_epoch
        except ValueError:
            continue
    return False

result = {
    "exists": False,
    "inode": "",
    "offset": 0,
    "data": "",
}
try:
    stat = os.stat(path)
except FileNotFoundError:
    print(json.dumps(result))
    raise SystemExit(0)

current_inode = str(stat.st_ino)
if recorded_inode != current_inode or stat.st_size < offset:
    offset = 0

with open(path, "r", encoding="utf-8", errors="ignore") as handle:
    handle.seek(offset)
    if since_epoch is None:
        data = handle.read()
    else:
        data = "".join(line for line in handle if is_after_cutoff(line))
    offset = handle.tell()

result = {
    "exists": True,
    "inode": current_inode,
    "offset": offset,
    "data": data,
}
print(json.dumps(result, ensure_ascii=True))
"""

REMOTE_WRITE_FILE_SCRIPT = """
import os
import pathlib
import sys

target = pathlib.Path(sys.argv[1])
target.parent.mkdir(parents=True, exist_ok=True)
tmp_path = target.with_name(target.name + ".codex-tmp")
tmp_path.write_text(sys.stdin.read(), encoding="utf-8")
os.replace(tmp_path, target)
"""

REMOTE_REPLACE_FILE_SCRIPT = """
import json
import os
import pathlib
import sys

source = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
target.parent.mkdir(parents=True, exist_ok=True)
changed = not target.is_file() or source.read_bytes() != target.read_bytes()
if changed:
    os.replace(source, target)
else:
    source.unlink(missing_ok=True)
print(json.dumps({"changed": changed}))
"""

REMOTE_DELETE_FILE_SCRIPT = """
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
path.unlink(missing_ok=True)
"""

REMOTE_READ_FILE_SCRIPT = """
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
if not path.is_file():
    print(json.dumps({"exists": False, "data": ""}))
    raise SystemExit(0)

print(
    json.dumps(
        {
            "exists": True,
            "data": path.read_text(encoding="utf-8", errors="ignore"),
        }
    )
)
"""

REMOTE_AI_DOMAINS_SNAPSHOT_SCRIPT = """
import json
import pathlib
import sqlite3
import sys

db_path = pathlib.Path(sys.argv[1])
result = {"exists": False, "ai_domains": []}
if not db_path.is_file():
    print(json.dumps(result))
    raise SystemExit(0)

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
try:
    rows = conn.execute(
        '''
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
        ORDER BY total_hits DESC, domain ASC
        '''
    ).fetchall()
finally:
    conn.close()

result["exists"] = True
result["ai_domains"] = [{key: row[key] for key in row.keys()} for row in rows]
print(json.dumps(result, ensure_ascii=True))
"""


def is_after_cutoff(line, since_epoch):
    """Return whether an Xray access-log line is at or after a timestamp."""

    if since_epoch is None:
        return True
    parts = line.split(" ", 2)
    if len(parts) < 2:
        return False
    timestamp = f"{parts[0]} {parts[1]}"
    for format_string in ("%Y/%m/%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(timestamp, format_string).replace(tzinfo=timezone.utc)
            return parsed.timestamp() >= since_epoch
        except ValueError:
            continue
    return False


def join_shell_args(args):
    return " ".join(shlex.quote(str(arg)) for arg in args)


def build_temp_target_path(path_text):
    path = Path(str(path_text))
    suffix = "".join(path.suffixes)
    token = uuid.uuid4().hex
    if not suffix:
        return f"{path}.codex-tmp-{token}"
    base_name = path.name[: -len(suffix)]
    return str(path.with_name(f"{base_name}.codex-tmp-{token}{suffix}"))


class RemoteFileOperations:
    """Operations that use a supplied remote command runner."""

    def __init__(self, config, run_remote, test_config):
        self.config = config
        self._run_remote = run_remote
        self._test_config = test_config

    def sync_text_file_to_remote(
        self,
        content,
        target_path,
        error_prefix,
        validate_config=False,
    ):
        """Upload a generated text artifact through a unique remote temp path."""

        remote_tmp = build_temp_target_path(target_path)
        try:
            self._run_remote(
                ["python3", "-c", REMOTE_WRITE_FILE_SCRIPT, remote_tmp],
                f"{error_prefix}上传失败",
                input_text=content,
            )
            if validate_config:
                self._test_config(config_path=remote_tmp)
            return self._run_remote(
                ["python3", "-c", REMOTE_REPLACE_FILE_SCRIPT, remote_tmp, target_path],
                f"{error_prefix}替换失败",
            )
        except Exception:
            try:
                self._run_remote(
                    ["python3", "-c", REMOTE_DELETE_FILE_SCRIPT, remote_tmp],
                    f"{error_prefix}临时文件清理失败",
                )
            except Exception:  # noqa: BLE001, S110 - cleanup must not mask the original failure
                pass
            raise

    def sync_generated_files(self, validate_config=False):
        uploaded = []
        if self.config.source_config_path and self.config.config_path:
            source = self.config.source_config_path
            if not source.is_file():
                raise RuntimeError(f"{self.config.label} source config not found: {source}")
            content = source.read_text(encoding="utf-8")
            replaced = self.sync_text_file_to_remote(
                content,
                self.config.config_path,
                f"{self.config.label} 配置",
                validate_config=validate_config,
            )
            config_changed = True
            try:
                replace_result = json.loads(replaced.stdout or "{}")
                if isinstance(replace_result, dict) and "changed" in replace_result:
                    config_changed = bool(replace_result["changed"])
            except json.JSONDecodeError:
                pass
            if config_changed:
                uploaded.append(self.config.config_path)

        if self.config.source_panel_ports_path and self.config.panel_ports_path:
            source = self.config.source_panel_ports_path
            if not source.is_file():
                raise RuntimeError(f"{self.config.label} panel ports file not found: {source}")
            content = source.read_text(encoding="utf-8")
            self.sync_text_file_to_remote(
                content,
                self.config.panel_ports_path,
                f"{self.config.label} 面板端口文件",
            )
            uploaded.append(self.config.panel_ports_path)

        # The generated config embeds this fragment, while the panel keeps the
        # fragment as the source of truth for its next render.
        if self.config.dynamic_routing_path and self.config.source_dynamic_routing_path:
            source = self.config.source_dynamic_routing_path
            if source.is_file():
                content = source.read_text(encoding="utf-8")
                self.sync_text_file_to_remote(
                    content,
                    self.config.dynamic_routing_path,
                    f"{self.config.label} 动态路由",
                )
            else:
                self.delete_remote_file(
                    self.config.dynamic_routing_path,
                    f"{self.config.label} 动态路由删除失败",
                )
            uploaded.append(self.config.dynamic_routing_path)
        return uploaded

    def delete_remote_file(self, remote_path, error_prefix):
        return self._run_remote(
            ["python3", "-c", REMOTE_DELETE_FILE_SCRIPT, remote_path],
            error_prefix,
        )

    def read_remote_text(self, remote_path, error_prefix):
        completed = self._run_remote(
            ["python3", "-c", REMOTE_READ_FILE_SCRIPT, remote_path],
            error_prefix,
        )
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{self.config.label} 远端文件返回无效 JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{self.config.label} 远端文件返回格式无效")  # noqa: TRY004
        return {
            "exists": bool(payload.get("exists")),
            "data": str(payload.get("data", "")),
        }

    def sync_text_file_from_remote(
        self,
        remote_path,
        local_path,
        error_prefix,
        preserve_local_when_missing=False,
    ):
        payload = self.read_remote_text(remote_path, error_prefix)
        if not payload["exists"]:
            if not preserve_local_when_missing:
                local_path.unlink(missing_ok=True)
            return False

        local_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(local_path, payload["data"])
        return True

    def sync_dynamic_routing_from_remote(self):
        local_path = self.config.source_dynamic_routing_path
        if local_path is None:
            return False
        return self.sync_text_file_from_remote(
            self.config.dynamic_routing_path,
            local_path,
            f"{self.config.label} 动态路由读取失败",
        )

    def sync_ai_report_from_remote(self):
        local_path = self.config.source_ai_report_path
        if local_path is None:
            return False
        return self.sync_text_file_from_remote(
            self.config.ai_report_path,
            local_path,
            f"{self.config.label} AI 域名报告读取失败",
            preserve_local_when_missing=True,
        )

    def read_ai_domains_snapshot_from_remote(self):
        completed = self._run_remote(
            [
                "python3",
                "-c",
                REMOTE_AI_DOMAINS_SNAPSHOT_SCRIPT,
                self.config.panel_db_path,
            ],
            f"{self.config.label} AI 域名快照读取失败",
        )
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{self.config.label} AI 域名快照返回无效 JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{self.config.label} AI 域名快照返回格式无效")  # noqa: TRY004
        rows = payload.get("ai_domains", [])
        if not isinstance(rows, list):
            raise RuntimeError(f"{self.config.label} AI 域名快照数据格式无效")  # noqa: TRY004
        return {
            "exists": bool(payload.get("exists")),
            "ai_domains": [item for item in rows if isinstance(item, dict)],
        }

    def read_access_log_delta(self, recorded_inode, offset, since_epoch=None):
        command_args = [
            "python3",
            "-c",
            REMOTE_FILE_DELTA_SCRIPT,
            self.config.access_log_path,
            str(recorded_inode or ""),
            str(int(offset or 0)),
        ]
        if since_epoch is not None:
            command_args.append(str(float(since_epoch)))
        completed = self._run_remote(
            command_args,
            f"{self.config.label} 访问日志读取失败",
        )
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{self.config.label} 访问日志返回无效 JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{self.config.label} 访问日志返回格式无效")  # noqa: TRY004
        return {
            "exists": bool(payload.get("exists")),
            "inode": str(payload.get("inode", "")),
            "offset": int(payload.get("offset", 0) or 0),
            "data": str(payload.get("data", "")),
        }


__all__ = [
    "REMOTE_AI_DOMAINS_SNAPSHOT_SCRIPT",
    "REMOTE_DELETE_FILE_SCRIPT",
    "REMOTE_FILE_DELTA_SCRIPT",
    "REMOTE_READ_FILE_SCRIPT",
    "REMOTE_REPLACE_FILE_SCRIPT",
    "REMOTE_WRITE_FILE_SCRIPT",
    "RemoteFileOperations",
    "build_temp_target_path",
    "is_after_cutoff",
    "join_shell_args",
]
