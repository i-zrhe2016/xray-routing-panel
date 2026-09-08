import concurrent.futures
import socket

from ..config import PROBE_TIMEOUT
from ..helpers import utc_iso_now
from ..subscriptions import parse_xray_client_profile
from ..xray.node.probes import reality_handshake_probe


class DiagnosticsService:
    """Deep, on-demand data-plane health check that bridges the control plane
    and the data plane: it verifies not just that node ports are reachable, but
    that the Reality handshake actually succeeds and that the parameters the
    subscription hands out (uuid / shortId / SNI) still match what the data
    plane is really running. That last consistency check catches the silent
    "port open but node unusable" drift that plain TCP probes miss.
    """

    def __init__(self, panel):
        self._panel = panel

    def diagnose_data_plane(self):
        profile, profile_error = parse_xray_client_profile()
        node_host = profile["server"] if profile else ""
        server_name = profile["server_name"] if profile else ""

        ports = [port for port in self._panel.query_ports() if port.get("enabled")]
        port_results = self._diagnose_ports(node_host, server_name, ports)
        consistency = self._check_config_consistency(profile)

        status = self._panel.data_plane.status_summary()
        tcp_ok = sum(1 for item in port_results if item["tcp_reachable"])
        reality_ok = sum(
            1 for item in port_results if item.get("reality") and item["reality"]["ok"]
        )
        return {
            "generated_at": utc_iso_now(),
            "data_plane_mode": self._panel.data_plane.mode,
            "management_target": status.get("management_target", ""),
            "subscription_profile_available": profile is not None,
            "subscription_error": profile_error,
            "node_host": node_host,
            "server_name": server_name,
            "ports": port_results,
            "consistency": consistency,
            "summary": {
                "ports_total": len(port_results),
                "ports_tcp_ok": tcp_ok,
                "ports_reality_ok": reality_ok,
                "consistency_available": consistency["available"],
                "consistency_ok": consistency.get("all_match", False),
            },
        }

    def _diagnose_ports(self, node_host, server_name, ports):
        if not ports:
            return []
        timeout = max(float(PROBE_TIMEOUT), 4.0)
        workers = min(8, len(ports))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(
                pool.map(
                    lambda port: self._diagnose_single_port(node_host, server_name, port, timeout),
                    ports,
                )
            )
        results.sort(key=lambda item: item["listen_port"])
        return results

    def _diagnose_single_port(self, node_host, server_name, port, timeout):
        listen_port = int(port["listen_port"])
        entry = {
            "listen_port": listen_port,
            "note": port.get("note", ""),
            "tcp_reachable": False,
            "tcp_error": "",
            "reality": None,
        }
        if not node_host:
            entry["tcp_error"] = "订阅 profile 不可用，无法确定节点地址。"
            return entry
        try:
            with socket.create_connection((node_host, listen_port), timeout=timeout):
                entry["tcp_reachable"] = True
        except OSError as exc:
            entry["tcp_error"] = str(exc)[:200]
            return entry
        if server_name:
            entry["reality"] = reality_handshake_probe(
                node_host, listen_port, server_name, timeout=timeout
            )
        return entry

    def _check_config_consistency(self, profile):
        result = {"available": False, "source": "", "error": "", "fields": [], "all_match": False}
        if profile is None:
            result["error"] = "订阅 profile 不可用，无法比对。"
            return result

        live = self._panel.data_plane.read_live_server_config()
        if not live.get("available"):
            result["source"] = live.get("source", "")
            result["error"] = live.get("error", "无法读取数据面配置。")
            return result

        config = live.get("config") or {}
        uuids, short_ids, server_names = set(), set(), set()
        for inbound in config.get("inbounds", []) or []:
            if not isinstance(inbound, dict):
                continue
            reality = (inbound.get("streamSettings") or {}).get("realitySettings") or {}
            if not reality:
                continue
            for client in (inbound.get("settings") or {}).get("clients", []) or []:
                if isinstance(client, dict) and client.get("id"):
                    uuids.add(str(client["id"]).strip())
            for short_id in reality.get("shortIds", []) or []:
                short_ids.add(str(short_id).strip())
            for sni in reality.get("serverNames", []) or []:
                server_names.add(str(sni).strip())

        fields = [
            self._consistency_field("UUID", profile["uuid"], uuids),
            self._consistency_field("Reality shortId", profile["short_id"], short_ids),
            self._consistency_field("SNI / serverName", profile["server_name"], server_names),
        ]
        result.update(
            available=True,
            source=live.get("source", ""),
            fields=fields,
            all_match=all(field["match"] for field in fields),
        )
        return result

    @staticmethod
    def _consistency_field(label, subscription_value, data_plane_values):
        values = sorted(value for value in data_plane_values if value)
        return {
            "field": label,
            "subscription": subscription_value,
            "data_plane": values,
            "match": subscription_value in data_plane_values,
        }
