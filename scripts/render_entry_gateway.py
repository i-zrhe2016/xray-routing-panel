#!/usr/bin/env python3
"""Render the TCP gateway from the deployed Xray config, then validate atomically.

The companion legacy forwarder sends a PROXY v2 header whose destination port is
the original alias port. This selects the compatible account after traffic
reaches the single public listener; HAProxy 2.8 does not require custom TLVs.
"""

import argparse
import json
import re
import subprocess
from pathlib import Path


def render_gateway(config, socket_dir, https_backend="127.0.0.1:18443"):
    inbounds = config.get("inbounds", [])
    entries = [i for i in inbounds if re.fullmatch(r"unified-\d+", i.get("tag", ""))]
    if len(entries) != 1:
        raise ValueError("Expected exactly one unified Xray inbound")
    entry = entries[0]
    entry_port = int(entry["tag"].split("-")[1])
    if not 1 <= entry_port <= 65535:
        raise ValueError("Invalid entry port")
    if not re.fullmatch(r"127\.0\.0\.1:\d+", https_backend):
        raise ValueError("HTTPS backend must be a loopback TCP endpoint")
    socket_dir = str(Path(socket_dir))
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", socket_dir):
        raise ValueError("Unsafe socket directory")
    legacy = sorted(
        (i for i in inbounds if re.fullmatch(r"panel-\d+", i.get("tag", ""))),
        key=lambda i: int(i["tag"].split("-")[1]),
    )
    ports = [int(i["tag"].split("-")[1]) for i in legacy]
    if entry_port in ports or any(not 1 <= port <= 65535 for port in ports) or len(set(ports)) != len(ports):
        raise ValueError("Invalid or conflicting legacy ports")
    snis = entry["streamSettings"]["realitySettings"]["serverNames"]
    if not snis or any(not re.fullmatch(r"[A-Za-z0-9.-]+", sni) for sni in snis):
        raise ValueError("Invalid REALITY server name")
    for inbound in [entry, *legacy]:
        expected = f"/var/log/xray/entry-{inbound['tag']}.sock"
        if inbound.get("listen") != expected or not inbound["streamSettings"]["sockopt"].get("acceptProxyProtocol"):
            raise ValueError("Xray must use the expected private socket and PROXY protocol")
    lines = [
        "global",
        "    maxconn 32768",
        "    hard-stop-after 1h",
        "",
        "defaults",
        "    mode tcp",
        "    timeout connect 5s",
        "    timeout client 1h",
        "    timeout server 1h",
        "",
        "frontend unified_entry",
        f"    bind :{entry_port}",
        f"    bind :::{entry_port} v6only",
        "    tcp-request connection expect-proxy layer4 if { src 127.0.0.2 }",
        "    tcp-request inspect-delay 5s",
        "    tcp-request content accept if { req.ssl_hello_type 1 }",
    ]
    for port in ports:
        lines.append(f"    use_backend legacy_{port} if {{ dst_port {port} }}")
    lines.extend(
        [
            f"    use_backend proxy_entry if {{ req.ssl_sni -i {' '.join(snis)} }}",
            "    default_backend subscription_https",
            "",
            "backend proxy_entry",
            f"    server xray {socket_dir}/entry-{entry['tag']}.sock send-proxy-v2",
            "",
            "backend subscription_https",
            f"    server https {https_backend}",
            "",
        ]
    )
    for port in ports:
        lines.extend(
            [
                f"backend legacy_{port}",
                f"    server xray {socket_dir}/entry-panel-{port}.sock send-proxy-v2",
                "",
            ]
        )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--socket-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--haproxy", default="/usr/sbin/haproxy")
    args = parser.parse_args()
    output = Path(args.output)
    content = render_gateway(json.loads(Path(args.config).read_text()), args.socket_dir)
    if output.exists() and output.read_text() == content:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    candidate = output.with_suffix(".candidate")
    candidate.write_text(content)
    try:
        subprocess.run([args.haproxy, "-c", "-f", str(candidate)], check=True, capture_output=True)
        candidate.replace(output)
    finally:
        candidate.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
