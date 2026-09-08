import json
from types import SimpleNamespace

from app.xray.stats import XrayStatsReader


def test_xray_stats_reader_normalizes_panel_counters():
    node = SimpleNamespace(
        run_statsquery=lambda timeout, pattern: SimpleNamespace(
            stdout=json.dumps(
                {
                    "stat": [
                        {"name": "inbound>>>panel-31098>>>traffic>>>uplink", "value": 30},
                        {"name": "inbound>>>panel-31098>>>traffic>>>downlink", "value": 40},
                        {"name": "user>>>panel-user-31098>>>traffic>>>uplink", "value": 5},
                        {"name": "other>>>panel-31098>>>traffic>>>uplink", "value": 100},
                    ]
                }
            )
        )
    )

    reader = XrayStatsReader(node, running_check=lambda: True)

    assert reader.read_xray_traffic_stats() == {31098: {"bytes_received": 35, "bytes_sent": 40}}


def test_xray_stats_reader_skips_query_when_node_is_not_running():
    calls = []
    node = SimpleNamespace(run_statsquery=lambda *args: calls.append(args))
    reader = XrayStatsReader(node, running_check=lambda: False)

    assert reader.read_xray_traffic_stats() == {}
    assert calls == []
