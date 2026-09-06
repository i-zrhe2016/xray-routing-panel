"""Optional real Xray/HAProxy test with ephemeral credentials and REALITY target."""

import base64
import copy
import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, x25519
from cryptography.x509.oid import NameOID

from app.xray.render_config import build_client_config, build_server_config
from scripts.legacy_entry_forwarder import Forwarder
from scripts.render_entry_gateway import render_gateway
from scripts.smoke_unified_entry import probe
from tests.test_unified_entry import values


@pytest.mark.skipif(
    not os.environ.get("XRAY_TEST_BINARY") or not shutil.which("haproxy"),
    reason="Set XRAY_TEST_BINARY and install haproxy for real transport tests",
)
def test_real_reality_legacy_forwarding_https_and_account_stats(tmp_path):
    binary = os.environ["XRAY_TEST_BINARY"]

    def free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args):
            pass

    cert_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "proxy.example")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(cert_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("proxy.example"), x509.DNSName("web.example")]), False)
        .sign(cert_key, hashes.SHA256())
    )
    (tmp_path / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "key.pem").write_bytes(
        cert_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(tmp_path / "cert.pem", tmp_path / "key.pem")

    class HTTPSServer(ThreadingHTTPServer):
        def get_request(self):
            connection, address = self.socket.accept()
            return ctx.wrap_socket(connection, server_side=True, do_handshake_on_connect=False), address

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    https = HTTPSServer(("127.0.0.1", 0), Handler)
    for server in [http, https]:
        threading.Thread(target=server.serve_forever, daemon=True).start()

    env = values()
    reality_key = x25519.X25519PrivateKey.generate()
    env.update(
        XRAY_UNIFIED_PORT=str(free_port()),
        XRAY_PUBLIC_HOST="127.0.0.1",
        XRAY_REALITY_PRIVATE_KEY=base64.urlsafe_b64encode(reality_key.private_bytes_raw()).decode().rstrip("="),
        XRAY_REALITY_PUBLIC_KEY=base64.urlsafe_b64encode(reality_key.public_key().public_bytes_raw())
        .decode()
        .rstrip("="),
        XRAY_SERVER_NAME="www.amazon.com",
        XRAY_DEST="www.amazon.com:443",
        XRAY_API_SERVER=f"127.0.0.1:{free_port()}",
        XRAY_LOGLEVEL="debug",
    )
    ports = [free_port(), free_port()]
    config = build_server_config(env, panel_ports=ports)
    # Xray 26.5 blocks loopback destinations on authenticated proxy inbounds by
    # default. Allow only this test's HTTP fixture, never production destinations.
    config["outbounds"][0]["settings"] = {
        "finalRules": [{"action": "allow", "ip": ["127.0.0.1"], "port": http.server_port}]
    }
    # HAProxy's production path maps the container's log mount onto the host.
    gateway = render_gateway(config, str(tmp_path), f"127.0.0.1:{https.server_port}")
    for inbound in config["inbounds"]:
        inbound["listen"] = str(tmp_path / Path(inbound["listen"]).name)
    config["log"].update(access=str(tmp_path / "access.log"), error=str(tmp_path / "error.log"))
    (tmp_path / "server.json").write_text(json.dumps(config))
    (tmp_path / "haproxy.cfg").write_text(gateway)
    forwarder = Forwarder(int(env["XRAY_UNIFIED_PORT"]), ports)
    client = build_client_config(env, ports)
    client["log"] = {"loglevel": "debug", "error": str(tmp_path / "client-error.log")}
    processes = []
    try:
        for command in [
            [binary, "run", "-c", str(tmp_path / "server.json")],
            ["haproxy", "-db", "-f", str(tmp_path / "haproxy.cfg")],
        ]:
            processes.append(subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        forwarder.listen("0.0.0.0", socket.AF_INET)
        forwarder.listen("::", socket.AF_INET6)
        for _ in range(50):
            assert all(p.poll() is None for p in processes), (tmp_path / "error.log").read_text()
            try:
                with socket.create_connection(("127.0.0.1", int(env["XRAY_UNIFIED_PORT"])), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.1)
        target = f"http://127.0.0.1:{http.server_port}/"
        assert probe(binary, client, target) == "200"
        for port in ports:
            legacy = copy.deepcopy(client)
            upstream = legacy["outbounds"][0]["settings"]["vnext"][0]
            upstream.update(port=port)
            upstream["users"][0]["id"] = env["XRAY_CLIENT_UUID"]
            assert probe(binary, legacy, target) == "200"
        upstream["port"] = int(env["XRAY_UNIFIED_PORT"])
        probe(binary, legacy, target, expect_success=False)
        web = subprocess.run(
            [
                "curl",
                "-ksSf",
                "--max-time",
                "5",
                "--noproxy",
                "*",
                "--resolve",
                f"web.example:{env['XRAY_UNIFIED_PORT']}:127.0.0.1",
                f"https://web.example:{env['XRAY_UNIFIED_PORT']}/",
            ],
            capture_output=True,
            check=False,
        )
        assert web.returncode == 0 and web.stdout == b"ok", web.stderr
        stats = subprocess.run(
            [binary, "api", "statsquery", f"--server={env['XRAY_API_SERVER']}", "-pattern", ">>>panel-"],
            capture_output=True,
            text=True,
            check=True,
        )
        counters = json.loads(stats.stdout)["stat"]
        assert any(i["name"].startswith(f"user>>>panel-user-{ports[0]}") and i.get("value", 0) > 0 for i in counters)
        for port in ports:
            assert any(i["name"].startswith(f"inbound>>>panel-{port}") and i.get("value", 0) > 0 for i in counters)
    finally:
        for process in processes:
            process.terminate()
            process.wait(timeout=5)
        http.shutdown()
        https.shutdown()
