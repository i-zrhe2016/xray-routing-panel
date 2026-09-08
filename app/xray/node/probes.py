"""Network, API-socket, and public-IP probes for managed Xray nodes."""

from __future__ import annotations

import ipaddress
import json
import socket
import ssl
from collections.abc import Callable

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
        port_text = text[closing + 2 :]
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
    """Probe a VLESS+Reality node with a TLS handshake."""

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
        with (
            socket.create_connection((host, port), timeout=timeout) as raw,
            context.wrap_socket(raw, server_hostname=server_name) as tls,
        ):
            return tls.getpeercert()

    try:
        _apply_cert_fields(result, _handshake(True))
        result.update(
            tls_handshake=True,
            cert_chain_valid=True,
            cert_matches_sni=True,
            ok=True,
        )
        return result
    except ssl.CertificateError as exc:
        result.update(tls_handshake=True, cert_chain_valid=True, cert_matches_sni=False)
        result["error"] = f"证书与 SNI 不匹配：{exc}"
    except ssl.SSLError as exc:
        result["error"] = f"TLS 握手失败（证书链无效或非 Reality 回落）：{exc}"
        return result
    except OSError as exc:
        result["error"] = f"TCP 连接失败：{exc}"
        return result

    try:
        _apply_cert_fields(result, _handshake(False))
    except (ssl.SSLError, OSError):
        pass
    return result


def probe_tcp_endpoint(
    host,
    port,
    timeout_seconds,
    *,
    label,
    remote_runner: Callable | None = None,
    remote_command_timeout=8.0,
):
    result = {"ok": False, "error": "", "management_error": False, "method": "tcp"}
    if remote_runner is not None:
        try:
            completed = remote_runner(
                [
                    "python3",
                    "-c",
                    REMOTE_SOCKET_PROBE_SCRIPT,
                    str(host),
                    str(int(port)),
                    str(timeout_seconds),
                ],
                f"{label} AI 上游 TCP 探测失败",
                timeout=max(float(remote_command_timeout), float(timeout_seconds) + 1),
            )
            payload = json.loads(completed.stdout or "{}")
            if not isinstance(payload, dict):
                raise RuntimeError("远端 TCP 探测返回格式无效")  # noqa: TRY004
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


def probe_reality_endpoint(
    host,
    port,
    server_name,
    timeout_seconds,
    *,
    label,
    remote_runner: Callable | None = None,
    remote_command_timeout=8.0,
):
    result = {
        "ok": False,
        "error": "",
        "management_error": False,
        "method": "reality",
    }
    if remote_runner is not None:
        try:
            completed = remote_runner(
                [
                    "python3",
                    "-c",
                    REMOTE_REALITY_PROBE_SCRIPT,
                    str(host),
                    str(int(port)),
                    str(server_name),
                    str(timeout_seconds),
                ],
                f"{label} AI 上游 REALITY 探测失败",
                timeout=max(float(remote_command_timeout), float(timeout_seconds) + 1),
            )
            payload = json.loads(completed.stdout or "{}")
            if not isinstance(payload, dict):
                raise RuntimeError("远端 REALITY 探测返回格式无效")  # noqa: TRY004
            result.update(payload)
        except (OSError, RuntimeError, json.JSONDecodeError) as exc:
            result.update(error=str(exc), management_error=True)
        return result

    result.update(reality_handshake_probe(host, port, server_name, timeout=timeout_seconds))
    return result


def resolve_public_ip(run_command, label, timeout_seconds=5):
    completed = run_command(
        ["python3", "-c", PUBLIC_IP_DISCOVERY_SCRIPT, str(timeout_seconds)],
        f"{label} 公网 IP 获取失败",
        timeout=timeout_seconds + 2,
    )
    value = (completed.stdout or "").strip()
    try:
        ipaddress.ip_address(value)
    except ValueError as exc:
        raise RuntimeError(f"{label} 公网 IP 格式无效。") from exc
    return value


def socket_running(endpoint, timeout_seconds=1):
    if endpoint is None:
        return False
    try:
        with socket.create_connection(endpoint, timeout=timeout_seconds):
            return True
    except OSError:
        return False


def remote_socket_running(
    run_remote,
    endpoint,
    label,
    timeout_seconds=1,
    remote_command_timeout=8.0,
):
    if endpoint is None:
        return False
    try:
        completed = run_remote(
            [
                "python3",
                "-c",
                REMOTE_SOCKET_CHECK_SCRIPT,
                endpoint[0],
                str(endpoint[1]),
                str(timeout_seconds),
            ],
            f"{label} 运行状态检查失败",
            timeout=max(float(remote_command_timeout), float(timeout_seconds) + 1),
        )
        return completed.returncode == 0
    except (OSError, RuntimeError):
        return False


__all__ = [
    "PUBLIC_IP_DISCOVERY_SCRIPT",
    "REMOTE_REALITY_PROBE_SCRIPT",
    "REMOTE_SOCKET_CHECK_SCRIPT",
    "REMOTE_SOCKET_PROBE_SCRIPT",
    "_apply_cert_fields",
    "_cert_common_name",
    "parse_api_endpoint",
    "probe_reality_endpoint",
    "probe_tcp_endpoint",
    "reality_handshake_probe",
    "remote_socket_running",
    "resolve_public_ip",
    "socket_running",
]
