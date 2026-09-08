from types import SimpleNamespace

from app.xray.node.fleet import aggregate_node_status, node_statuses, restart_node_or_raise


def test_node_fleet_aggregates_multiple_controller_statuses():
    nodes = {
        "hawaii": SimpleNamespace(
            status_summary=lambda: {
                "label": "AI 夏威夷",
                "configured": True,
                "reachable": True,
                "xray_running": True,
                "supports_sync": False,
                "supports_restart": True,
                "last_error": "",
            }
        ),
        "taiwan": SimpleNamespace(
            status_summary=lambda: {
                "label": "AI 台湾",
                "configured": True,
                "reachable": False,
                "xray_running": False,
                "supports_sync": False,
                "supports_restart": True,
                "last_error": "offline",
            }
        ),
    }

    statuses = node_statuses(nodes)
    aggregate = aggregate_node_status(statuses)

    assert statuses[0]["node_id"] == "hawaii"
    assert aggregate["reachable"] is True
    assert aggregate["all_reachable"] is False
    assert aggregate["xray_running"] is None
    assert "AI 台湾: offline" in aggregate["last_error"]


def test_restart_node_or_raise_uses_explicit_node_controller():
    calls = []
    controller = SimpleNamespace(
        supports_restart=lambda: True,
        restart=lambda: calls.append("restart") or True,
        status_summary=lambda: {"label": "AI 台湾", "xray_running": True},
    )

    result = restart_node_or_raise({"taiwan": controller}, controller, node_id="taiwan")

    assert calls == ["restart"]
    assert result["node_id"] == "taiwan"
