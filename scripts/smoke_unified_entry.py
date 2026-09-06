#!/usr/bin/env python3
"""Exercise REALITY on the shared entry and every legacy alias (no secrets logged)."""

import argparse
import copy
import json
import socket
import subprocess
import tempfile
import time
from pathlib import Path


def probe(binary, client, url, expect_success=True):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        socks_port = sock.getsockname()[1]
    client = copy.deepcopy(client)
    client.pop("panelSubscription", None)
    client["inbounds"] = [{"listen": "127.0.0.1", "port": socks_port, "protocol": "socks", "settings": {"udp": False}}]
    with tempfile.TemporaryDirectory(prefix="xray-entry-probe-") as directory:
        config = Path(directory) / "client.json"
        config.write_text(json.dumps(client))
        config.chmod(0o600)
        with (Path(directory) / "client.log").open("w+") as log:
            process = subprocess.Popen([binary, "run", "-c", str(config)], stdout=log, stderr=log)
            try:
                for _ in range(50):
                    if process.poll() is not None:
                        raise RuntimeError("Probe client failed to start")
                    try:
                        with socket.create_connection(("127.0.0.1", socks_port), timeout=0.1):
                            break
                    except OSError:
                        time.sleep(0.1)
                result = subprocess.run(
                    [
                        "curl",
                        "--silent",
                        "--show-error",
                        "--fail",
                        "--max-time",
                        "20",
                        "--proxy",
                        f"socks5h://127.0.0.1:{socks_port}",
                        "--noproxy",
                        "",
                        "--output",
                        "/dev/null",
                        "--write-out",
                        "%{http_code}",
                        url,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                succeeded = result.returncode == 0
                if succeeded != expect_success:
                    raise RuntimeError(
                        f"REALITY probe unexpected result: HTTP {result.stdout}, curl exit {result.returncode}"
                    )
                return result.stdout or "blocked"
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-config", required=True)
    parser.add_argument("--client-config", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--xray", default="/usr/local/bin/xray")
    parser.add_argument("--url", default="https://www.amazon.com/robots.txt")
    args = parser.parse_args()
    server = json.loads(Path(args.server_config).read_text())
    template = json.loads(Path(args.client_config).read_text())
    entry = next(i for i in server["inbounds"] if i["tag"].startswith("unified-"))
    port = int(entry["tag"].split("-")[1])
    for account in entry["settings"]["clients"]:
        client = copy.deepcopy(template)
        upstream = client["outbounds"][0]["settings"]["vnext"][0]
        upstream.update(address=args.host, port=port)
        upstream["users"][0]["id"] = account["id"]
        print(f"unified:{port} {account['email']}: HTTP {probe(args.xray, client, args.url)}", flush=True)
    checked_shared_rejection = False
    for inbound in server["inbounds"]:
        if not inbound["tag"].startswith("panel-"):
            continue
        old_port = int(inbound["tag"].split("-")[1])
        client = copy.deepcopy(template)
        upstream = client["outbounds"][0]["settings"]["vnext"][0]
        upstream.update(address=args.host, port=old_port)
        upstream["users"][0]["id"] = inbound["settings"]["clients"][0]["id"]
        print(f"legacy:{old_port}: HTTP {probe(args.xray, client, args.url)}", flush=True)
        # Old shared credentials must not authenticate on the new public entry.
        if not checked_shared_rejection:
            upstream["port"] = port
            probe(args.xray, client, args.url, expect_success=False)
            checked_shared_rejection = True


if __name__ == "__main__":
    main()
