#!/usr/bin/env python3
"""Forward legacy TCP aliases to the unified entry with a trusted PROXY header."""

import argparse
import ipaddress
import json
import socket
import struct
import threading
from pathlib import Path

PROXY_SIGNATURE = b"\r\n\r\n\x00\r\nQUIT\n"


def proxy_header(peer, local, family):
    source = ipaddress.ip_address(peer[0]).packed
    destination = ipaddress.ip_address(local[0]).packed
    if family == socket.AF_INET:
        if len(source) != 4 or len(destination) != 4:
            raise ValueError("IPv4 listener received a non-IPv4 address")
        address_block = source + destination
        family_protocol = 0x11
    else:
        if len(source) != 16 or len(destination) != 16:
            raise ValueError("IPv6 listener received a non-IPv6 address")
        address_block = source + destination
        family_protocol = 0x21
    address_block += struct.pack("!HH", int(peer[1]), int(local[1]))
    return PROXY_SIGNATURE + bytes((0x21, family_protocol)) + struct.pack("!H", len(address_block)) + address_block


def relay(left, right):
    try:
        while True:
            data = left.recv(65536)
            if not data:
                try:
                    right.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                return
            right.sendall(data)
    except OSError:
        return


class Forwarder:
    def __init__(self, entry_port, ports):
        self.entry_port = int(entry_port)
        self.ports = tuple(sorted(set(int(port) for port in ports)))
        self.listeners = []

    def listen(self, bind_address, family):
        for port in self.ports:
            server = socket.socket(family, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            server.bind((bind_address, port))
            server.listen(256)
            self.listeners.append(server)
            threading.Thread(target=self.accept_loop, args=(server, family), daemon=True).start()

    def accept_loop(self, listener, family):
        while True:
            try:
                client, peer = listener.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(client, peer, family), daemon=True).start()

    def handle(self, client, peer, family):
        upstream = None
        try:
            local = client.getsockname()
            upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            upstream.bind(("127.0.0.2", 0))
            upstream.settimeout(10)
            upstream.connect(("127.0.0.1", self.entry_port))
            upstream.settimeout(None)
            upstream.sendall(proxy_header(peer, local, family))
            threads = [
                threading.Thread(target=relay, args=(client, upstream), daemon=True),
                threading.Thread(target=relay, args=(upstream, client), daemon=True),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        except (OSError, ValueError):
            return
        finally:
            for connection in (client, upstream):
                if connection is not None:
                    try:
                        connection.close()
                    except OSError:
                        pass


def load_ports(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    inbounds = payload.get("inbounds", [])
    entries = [item for item in inbounds if str(item.get("tag", "")).startswith("unified-")]
    if len(entries) != 1:
        raise ValueError("Expected exactly one unified inbound")
    entry_port = int(entries[0]["tag"].split("-", 1)[1])
    ports = [int(item["tag"].split("-", 1)[1]) for item in inbounds if str(item.get("tag", "")).startswith("panel-")]
    if entry_port in ports:
        raise ValueError("Unified port conflicts with a legacy port")
    return entry_port, ports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    entry_port, ports = load_ports(args.config)
    forwarder = Forwarder(entry_port, ports)
    if ports:
        forwarder.listen("0.0.0.0", socket.AF_INET)
        forwarder.listen("::", socket.AF_INET6)
    threading.Event().wait()


if __name__ == "__main__":
    main()
