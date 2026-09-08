"""Xray statsquery reader for traffic accounting."""

import json

from ..config import XRAY_STATS_QUERY_TIMEOUT


class XrayStatsReader:
    """Read and normalize panel traffic counters from a node controller."""

    def __init__(self, node_controller, running_check=None):
        self.node_controller = node_controller
        self.running_check = running_check if running_check is not None else node_controller.is_running

    def read_xray_traffic_stats(self):
        if not self.running_check():
            return {}
        try:
            completed = self.node_controller.run_statsquery(
                XRAY_STATS_QUERY_TIMEOUT,
                ">>>panel-",
            )
            if completed is None:
                return {}
            payload = json.loads(completed.stdout or "{}")
        except (RuntimeError, json.JSONDecodeError):
            return {}

        counters = {}
        for item in payload.get("stat", []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            parts = name.split(">>>")
            if len(parts) != 4 or parts[0] not in {"inbound", "user"} or parts[2] != "traffic":
                continue
            tag = parts[1]
            prefix = "panel-user-" if parts[0] == "user" else "panel-"
            if not tag.startswith(prefix):
                continue
            try:
                listen_port = int(tag.removeprefix(prefix))
                value = int(item.get("value", 0) or 0)
            except (TypeError, ValueError):
                continue
            counter = counters.setdefault(
                listen_port,
                {
                    "bytes_sent": 0,
                    "bytes_received": 0,
                },
            )
            if parts[3] == "uplink":
                counter["bytes_received"] += value
            elif parts[3] == "downlink":
                counter["bytes_sent"] += value
        return counters
