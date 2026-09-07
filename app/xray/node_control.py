import json
import os
import shlex
import shutil
import socket
import ssl
import subprocess
import ipaddress
import uuid
from dataclasses import dataclass
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
print(json.dumps(result))
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

REMOTE_SOCKET_CHECK_SCRIPT = """
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
timeout = float(sys.argv[3])
try:
    with socket.create_connection((host, port), timeout=timeout):
        raise SystemExit(0)
except OSError:
    raise SystemExit(1)
"""

REMOTE_SOCKET_PROBE_SCRIPT = """
import json
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
timeout = float(sys.argv[3])
result = {"ok": False, "error": "", "method": "tcp"}
try:
    with socket.create_connection((host, port), timeout=timeout):
        result["ok"] = True
except OSError as exc:
    result["error"] = str(exc)[:200]
print(json.dumps(result, ensure_ascii=True))
"""

REMOTE_REALITY_PROBE_SCRIPT = """
import json
import socket
import ssl
import sys

host = sys.argv[1]
port = int(sys.argv[2])
server_name = sys.argv[3]
timeout = float(sys.argv[4])
result = {
    "ok": False,
    "tls_handshake": False,
    "cert_chain_valid": False,
    "cert_matches_sni": None,
    "error": "",
}
try:
    context = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=server_name) as tls:
            tls.getpeercert()
    result.update(
        ok=True,
        tls_handshake=True,
        cert_chain_valid=True,
        cert_matches_sni=True,
    )
except ssl.CertificateError as exc:
    result.update(
        tls_handshake=True,
        cert_chain_valid=True,
        cert_matches_sni=False,
        error=f"证书与 SNI 不匹配：{exc}",
    )
except ssl.SSLError as exc:
    result["error"] = f"TLS 握手失败：{exc}"
except OSError as exc:
    result["error"] = f"TCP 连接失败：{exc}"
print(json.dumps(result, ensure_ascii=True))
"""

PUBLIC_IP_DISCOVERY_SCRIPT = """
import ipaddress
import sys
import urllib.request

urls = [
    "https://ipv4.icanhazip.com",
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://ifconfig.co/ip",
]
timeout = float(sys.argv[1])
for url in urls:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            value = response.read().decode("utf-8", errors="ignore").strip()
        ipaddress.ip_address(value)
        print(value)
        raise SystemExit(0)
    except Exception:
        continue
raise SystemExit(1)
"""


def parse_api_endpoint(raw):
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("tcp://"):
        text = text.removeprefix("tcp://")
    if text.startswith("["):
        closing = text.find("]")
        if closing <= 0 or closing + 2 >= len(text) or text[closing + 1] != ":":
            return None
        host = text[1:closing]
        port_text = text[closing + 2:]
    else:
        host, separator, port_text = text.rpartition(":")
        if not separator:
            return None
    try:
        port = int(port_text)
    except ValueError:
        return None
    if port < 1 or port > 65535:
        return None
    return (host or "127.0.0.1", port)


def resolve_local_bin(raw):
    text = str(raw or "").strip()
    if not text:
        return None
    if os.path.sep not in text:
        return shutil.which(text)
    return text if os.path.isfile(text) else None


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


def _cert_common_name(name_field):
    for rdn in name_field or ():
        for entry in rdn:
            if len(entry) == 2 and entry[0] in ("commonName", "CN"):
                return entry[1]
    return ""


def _apply_cert_fields(result, cert):
    if not cert:
        return
    result["cert_subject_cn"] = _cert_common_name(cert.get("subject"))
    result["cert_issuer_cn"] = _cert_common_name(cert.get("issuer"))
    result["cert_not_after"] = str(cert.get("notAfter", ""))


def reality_handshake_probe(host, port, server_name, timeout=6.0):
    """Probe a VLESS+Reality node from the control plane by doing a TLS handshake.

    A healthy Reality inbound forwards any non-authenticated TLS client to its
    masquerade destination, so a plain probe with the configured SNI should
    complete and present that site's real, chain-valid certificate. A failed
    handshake, an untrusted chain, or a certificate whose host differs from the
    SNI all signal that the data-plane Reality service is missing, broken, or
    pointed at the wrong ``dest`` — the classic "port open but node unusable"
    failure mode that a bare TCP probe cannot detect.
    """
    result = {
        "ok": False,
        "tls_handshake": False,
        "cert_chain_valid": False,
        "cert_matches_sni": None,
        "cert_subject_cn": "",
        "cert_issuer_cn": "",
        "cert_not_after": "",
        "error": "",
    }
    if not host or not server_name:
        result["error"] = "缺少节点地址或 SNI，无法进行 Reality 握手探测。"
        return result
    try:
        port = int(port)
    except (TypeError, ValueError):
        result["error"] = f"无效端口：{port!r}"
        return result

    def _handshake(check_hostname):
        context = ssl.create_default_context()
        context.check_hostname = check_hostname
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=server_name) as tls:
                return tls.getpeercert()

    # 1) Strict: require a trusted chain AND a certificate that matches the SNI.
    try:
        _apply_cert_fields(result, _handshake(True))
        result.update(tls_handshake=True, cert_chain_valid=True, cert_matches_sni=True, ok=True)
        return result
    except ssl.CertificateError as exc:
        # Chain is trusted, but the presented host differs from the SNI.
        result.update(tls_handshake=True, cert_chain_valid=True, cert_matches_sni=False)
        result["error"] = f"证书与 SNI 不匹配：{exc}"
    except ssl.SSLError as exc:
        result["error"] = f"TLS 握手失败（证书链无效或非 Reality 回落）：{exc}"
        return result
    except OSError as exc:
        result["error"] = f"TCP 连接失败：{exc}"
        return result

    # 2) Best-effort: capture the presented certificate for diagnostics when the
    #    chain is trusted but the host did not match the SNI.
    try:
        _apply_cert_fields(result, _handshake(False))
    except (ssl.SSLError, OSError):
        pass
    return result


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


class DataPlaneController:
    def __init__(self, config: DataPlaneConfig):
        self.config = config

    @property
    def mode(self):
        if self.config.ssh_target:
            return "ssh"
        if resolve_local_bin(self.config.local_bin):
            return "local"
        if self.config.container_name:
            return "docker"
        return "unmanaged"

    @property
    def is_remote(self):
        return self.mode == "ssh"

    def is_configured(self):
        if self.mode != "unmanaged":
            return True
        return bool(self.config.upstream_host and self.config.upstream_port)

    def supports_restart(self):
        if self.config.restart_command:
            return True
        if self.mode == "docker":
            return bool(self.config.container_name)
        if self.mode == "ssh":
            return bool(self.config.container_name or self.config.restart_command)
        return False

    def supports_sync(self):
        return bool(
            self.mode == "ssh"
            and self.config.config_path
            and self.config.source_config_path
        )

    def supports_dynamic_routing_pull(self):
        return bool(
            self.mode == "ssh"
            and self.config.dynamic_routing_path
            and self.config.source_dynamic_routing_path
        )

    def supports_ai_report_pull(self):
        return bool(
            self.mode == "ssh"
            and self.config.ai_report_path
            and self.config.source_ai_report_path
        )

    def supports_ai_domains_snapshot_pull(self):
        return bool(self.mode == "ssh" and self.config.panel_db_path)

    def supports_stats(self):
        return bool(self.mode in {"ssh", "local", "docker"} and self.config.api_server)

    def supports_logs(self):
        return bool(self.mode == "ssh" and self.config.access_log_path)

    def display_target(self):
        if self.mode == "ssh":
            return self.config.ssh_target
        if self.mode == "local":
            return resolve_local_bin(self.config.local_bin) or self.config.local_bin
        if self.mode == "docker":
            return self.config.container_name
        if self.config.upstream_host and self.config.upstream_port:
            return f"{self.config.upstream_host}:{self.config.upstream_port}"
        return ""

    def _run_subprocess(self, command, error_prefix, timeout=None, input_text=None):
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

    def _run_remote(self, args, error_prefix, timeout=None, input_text=None):
        if timeout is None:
            timeout = self.config.remote_command_timeout
        remote_command = join_shell_args(args)
        command = [
            self.config.ssh_bin,
            *self._normalized_ssh_options(),
            self.config.ssh_target,
            remote_command,
        ]
        return self._run_subprocess(command, error_prefix, timeout=timeout, input_text=input_text)

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

    def _run_local_shell(self, shell_command, error_prefix, timeout=None):
        return self._run_subprocess(["sh", "-lc", shell_command], error_prefix, timeout=timeout)

    def _run_remote_shell(self, shell_command, error_prefix, timeout=None):
        return self._run_remote(["sh", "-lc", shell_command], error_prefix, timeout=timeout)

    def _sync_text_file_to_remote(self, content, target_path, error_prefix, validate_config=False):
        """Upload a generated text artifact through a unique remote temp path."""

        remote_tmp = build_temp_target_path(target_path)
        try:
            self._run_remote(
                ["python3", "-c", REMOTE_WRITE_FILE_SCRIPT, remote_tmp],
                f"{error_prefix}上传失败",
                input_text=content,
            )
            if validate_config:
                self.test_config(config_path=remote_tmp)
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
            except Exception:
                pass
            raise

    def probe_tcp_endpoint(self, host, port, timeout_seconds):
        result = {"ok": False, "error": "", "management_error": False, "method": "tcp"}
        if self.mode == "ssh":
            try:
                completed = self._run_remote(
                    [
                        "python3",
                        "-c",
                        REMOTE_SOCKET_PROBE_SCRIPT,
                        str(host),
                        str(int(port)),
                        str(timeout_seconds),
                    ],
                    f"{self.config.label} AI 上游 TCP 探测失败",
                    timeout=max(self.config.remote_command_timeout, float(timeout_seconds) + 1),
                )
                payload = json.loads(completed.stdout or "{}")
                if not isinstance(payload, dict):
                    raise RuntimeError("远端 TCP 探测返回格式无效")
                result.update(payload)
            except (OSError, RuntimeError, json.JSONDecodeError) as exc:
                result.update(error=str(exc), management_error=True)
            return result

        try:
            with socket.create_connection((str(host), int(port)), timeout=timeout_seconds):
                result["ok"] = True
        except OSError as exc:
            result["error"] = str(exc)[:200]
        return result

    def probe_reality_endpoint(self, host, port, server_name, timeout_seconds):
        result = {
            "ok": False,
            "error": "",
            "management_error": False,
            "method": "reality",
        }
        if self.mode == "ssh":
            try:
                completed = self._run_remote(
                    [
                        "python3",
                        "-c",
                        REMOTE_REALITY_PROBE_SCRIPT,
                        str(host),
                        str(int(port)),
                        str(server_name),
                        str(timeout_seconds),
                    ],
                    f"{self.config.label} AI 上游 REALITY 探测失败",
                    timeout=max(self.config.remote_command_timeout, float(timeout_seconds) + 1),
                )
                payload = json.loads(completed.stdout or "{}")
                if not isinstance(payload, dict):
                    raise RuntimeError("远端 REALITY 探测返回格式无效")
                result.update(payload)
            except (OSError, RuntimeError, json.JSONDecodeError) as exc:
                result.update(error=str(exc), management_error=True)
            return result

        result.update(reality_handshake_probe(host, port, server_name, timeout=timeout_seconds))
        return result

    def resolve_public_ip(self, timeout_seconds=5):
        command = ["python3", "-c", PUBLIC_IP_DISCOVERY_SCRIPT, str(timeout_seconds)]
        if self.mode == "ssh":
            completed = self._run_remote(command, f"{self.config.label} 公网 IP 获取失败", timeout=timeout_seconds + 2)
        else:
            completed = self._run_subprocess(command, f"{self.config.label} 公网 IP 获取失败", timeout=timeout_seconds + 2)
        value = (completed.stdout or "").strip()
        try:
            ipaddress.ip_address(value)
        except ValueError as exc:
            raise RuntimeError(f"{self.config.label} 公网 IP 格式无效。") from exc
        return value

    def _container_exists(self):
        if not self.config.container_name:
            return False
        command = [self.config.docker_bin, "container", "inspect", self.config.container_name]
        try:
            if self.mode == "ssh":
                self._run_remote(command, f"{self.config.label} 容器检查失败")
                return True
            self._run_subprocess(command, f"{self.config.label} 容器检查失败")
            return True
        except (OSError, RuntimeError):
            return False

    def _docker_running(self):
        if not self._container_exists():
            return False
        command = [
            self.config.docker_bin,
            "inspect",
            "--format",
            "{{.State.Running}}",
            self.config.container_name,
        ]
        try:
            if self.mode == "ssh":
                completed = self._run_remote(command, f"{self.config.label} 容器状态检查失败")
            else:
                completed = self._run_subprocess(command, f"{self.config.label} 容器状态检查失败")
            return completed.returncode == 0 and completed.stdout.strip().lower() == "true"
        except (OSError, RuntimeError):
            return False

    def _socket_running_locally(self, timeout_seconds=1):
        endpoint = parse_api_endpoint(self.config.api_server)
        if endpoint is None:
            return False
        try:
            with socket.create_connection(endpoint, timeout=timeout_seconds):
                return True
        except OSError:
            return False

    def is_running(self, timeout_seconds=1):
        if self.mode == "local":
            return self._socket_running_locally(timeout_seconds=timeout_seconds)
        if self.mode == "docker":
            return self._docker_running()
        if self.mode == "ssh":
            endpoint = parse_api_endpoint(self.config.api_server)
            if endpoint is None:
                return False
            try:
                completed = self._run_remote(
                    [
                        "python3",
                        "-c",
                        REMOTE_SOCKET_CHECK_SCRIPT,
                        endpoint[0],
                        str(endpoint[1]),
                        str(timeout_seconds),
                    ],
                    f"{self.config.label} 运行状态检查失败",
                    timeout=max(self.config.remote_command_timeout, float(timeout_seconds) + 1),
                )
                return completed.returncode == 0
            except (OSError, RuntimeError):
                return False
        if self.config.upstream_host and self.config.upstream_port:
            try:
                with socket.create_connection(
                    (self.config.upstream_host, int(self.config.upstream_port)),
                    timeout=timeout_seconds,
                ):
                    return True
            except OSError:
                return False
        return False

    def sync_generated_files(self, validate_config=False):
        if self.mode != "ssh":
            return []

        uploaded = []
        if self.config.source_config_path and self.config.config_path:
            source = self.config.source_config_path
            if not source.is_file():
                raise RuntimeError(f"{self.config.label} source config not found: {source}")
            content = source.read_text(encoding="utf-8")
            replaced = self._sync_text_file_to_remote(
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
            self._sync_text_file_to_remote(
                content,
                self.config.panel_ports_path,
                f"{self.config.label} 面板端口文件",
            )
            uploaded.append(self.config.panel_ports_path)

        # The generated config embeds this fragment, while the panel keeps the
        # fragment as the source of truth for its next render. Keep both copies
        # in sync so a panel restart cannot silently drop AI routing.
        if self.config.dynamic_routing_path and self.config.source_dynamic_routing_path:
            source = self.config.source_dynamic_routing_path
            if source.is_file():
                content = source.read_text(encoding="utf-8")
                self._sync_text_file_to_remote(
                    content,
                    self.config.dynamic_routing_path,
                    f"{self.config.label} 动态路由",
                )
            else:
                self._run_remote(
                    ["python3", "-c", REMOTE_DELETE_FILE_SCRIPT, self.config.dynamic_routing_path],
                    f"{self.config.label} 动态路由删除失败",
                )
            uploaded.append(self.config.dynamic_routing_path)

        return uploaded

    def sync_text_file_from_remote(
        self,
        remote_path,
        local_path,
        error_prefix,
        preserve_local_when_missing=False,
    ):
        completed = self._run_remote(
            ["python3", "-c", REMOTE_READ_FILE_SCRIPT, remote_path],
            error_prefix,
        )
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{self.config.label} 远端文件返回无效 JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{self.config.label} 远端文件返回格式无效")

        if not payload.get("exists"):
            if not preserve_local_when_missing:
                local_path.unlink(missing_ok=True)
            return False

        local_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(local_path, str(payload.get("data", "")))
        return True

    def sync_dynamic_routing_from_remote(self):
        if not self.supports_dynamic_routing_pull():
            return False

        local_path = self.config.source_dynamic_routing_path
        if local_path is None:
            return False
        return self.sync_text_file_from_remote(
            self.config.dynamic_routing_path,
            local_path,
            f"{self.config.label} 动态路由读取失败",
        )

    def remove_dynamic_routing(self):
        """Remove the active AI routing fragment from the managed data plane."""
        removed = False
        local_path = self.config.source_dynamic_routing_path
        if local_path is not None:
            removed = local_path.unlink(missing_ok=True) or removed
        if self.mode == "ssh" and self.config.dynamic_routing_path:
            self._run_remote(
                ["python3", "-c", REMOTE_DELETE_FILE_SCRIPT, self.config.dynamic_routing_path],
                f"{self.config.label} 动态路由删除失败",
            )
            removed = True
        return removed

    def sync_ai_report_from_remote(self):
        if not self.supports_ai_report_pull():
            return False
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
        if not self.supports_ai_domains_snapshot_pull():
            return {"exists": False, "ai_domains": []}
        completed = self._run_remote(
            ["python3", "-c", REMOTE_AI_DOMAINS_SNAPSHOT_SCRIPT, self.config.panel_db_path],
            f"{self.config.label} AI 域名快照读取失败",
        )
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{self.config.label} AI 域名快照返回无效 JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{self.config.label} AI 域名快照返回格式无效")
        rows = payload.get("ai_domains", [])
        if not isinstance(rows, list):
            raise RuntimeError(f"{self.config.label} AI 域名快照数据格式无效")
        return {
            "exists": bool(payload.get("exists")),
            "ai_domains": [item for item in rows if isinstance(item, dict)],
        }

    def read_live_server_config(self):
        """Read the xray server config the data plane is actually running.

        Returns ``{available, source, config, error}``. In ssh mode the live
        remote ``config_path`` is read over the wire; in local/docker mode the
        on-disk ``config_path`` is read directly. When neither is reachable
        (e.g. an unmanaged upstream) it falls back to the panel-generated
        ``source_config_path`` so the caller can still compare artifacts, and
        labels the source honestly so callers never mistake a stale local file
        for the live data plane.
        """
        result = {"available": False, "source": "", "config": None, "error": ""}

        text = None
        if self.mode == "ssh" and self.config.config_path:
            try:
                completed = self._run_remote(
                    ["python3", "-c", REMOTE_READ_FILE_SCRIPT, self.config.config_path],
                    f"{self.config.label} 配置读取失败",
                )
                payload = json.loads(completed.stdout or "{}")
            except json.JSONDecodeError as exc:
                result["error"] = f"数据面配置返回无效 JSON：{exc}"
                return result
            except RuntimeError as exc:
                result["error"] = str(exc)[:300]
                return result
            if not isinstance(payload, dict) or not payload.get("exists"):
                result["error"] = f"数据面未找到配置文件：{self.config.config_path}"
                return result
            text = str(payload.get("data", ""))
            result["source"] = f"数据面实时配置 (ssh:{self.config.config_path})"
        else:
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
        active_config_path = str(config_path or self.config.config_path or "").strip()
        if not active_config_path:
            return None

        if self.mode == "local":
            local_bin = resolve_local_bin(self.config.local_bin)
            if not local_bin:
                return None
            return self._run_subprocess(
                [local_bin, "run", "-test", "-config", active_config_path],
                f"{self.config.label} 配置校验失败",
            )

        if self.mode == "docker":
            if not self._container_exists() or not self._docker_running():
                return None
            return self._run_subprocess(
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

        if self.mode == "ssh":
            if self.config.container_name:
                return self._validate_config_in_remote_container(active_config_path)
            return self._run_remote(
                [self.config.xray_bin, "run", "-test", "-config", active_config_path],
                f"{self.config.label} 配置校验失败",
            )
        return None

    def _validate_config_in_remote_container(self, host_config_path):
        """Validate a config file living on the remote host using the data-plane
        container's own xray binary.

        The container mounts only the live config file (single-file bind mount),
        so a freshly written temp file on the host is invisible inside the
        container. Copy it into a fixed scratch path within the running
        container and run ``xray -test`` against it there. This leaves the live
        config untouched and validates with the exact xray build that will serve
        traffic.

        The scratch path is fixed, so each run overwrites the previous one (it
        never accumulates) and it is wiped whenever the container is recreated.
        No explicit cleanup is attempted: data-plane images are commonly
        distroless and ship neither ``rm`` nor a shell, and xray never reads the
        scratch path at runtime, so a lingering temp file is inert.
        """
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
            if self.mode == "ssh":
                self._run_remote_shell(self.config.restart_command, f"{self.config.label} 重启失败")
            else:
                self._run_local_shell(self.config.restart_command, f"{self.config.label} 重启失败")
            return True

        if self.mode == "docker":
            if not self._container_exists():
                return False
            action = "start" if not self._docker_running() else "restart"
            self._run_subprocess(
                [self.config.docker_bin, action, self.config.container_name],
                f"{self.config.label} 重启失败",
            )
            return True

        if self.mode == "ssh":
            if not self.config.container_name or not self._container_exists():
                return False
            action = "start" if not self._docker_running() else "restart"
            self._run_remote(
                [self.config.docker_bin, action, self.config.container_name],
                f"{self.config.label} 重启失败",
            )
            return True

        return False

    def read_access_log_delta(self, recorded_inode, offset, since_epoch=None):
        if not self.supports_logs():
            return {"exists": False, "inode": "", "offset": 0, "data": ""}
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
            raise RuntimeError(f"{self.config.label} 访问日志返回格式无效")
        return {
            "exists": bool(payload.get("exists")),
            "inode": str(payload.get("inode", "")),
            "offset": int(payload.get("offset", 0) or 0),
            "data": str(payload.get("data", "")),
        }

    def run_statsquery(self, timeout_seconds, pattern):
        if not self.supports_stats():
            return None

        if self.mode == "local":
            local_bin = resolve_local_bin(self.config.local_bin)
            if not local_bin:
                return None
            return self._run_subprocess(
                [
                    local_bin,
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

        if self.mode == "docker":
            return self._run_subprocess(
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

        if self.mode == "ssh":
            if self.config.container_name:
                return self._run_remote(
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
            return self._run_remote(
                [
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
        return None

    def status_summary(self):
        xray_running = None
        error = ""
        configured = self.is_configured()
        if configured:
            try:
                xray_running = self.is_running()
            except Exception as exc:
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


ManagedNodeConfig = DataPlaneConfig
ManagedNodeController = DataPlaneController
