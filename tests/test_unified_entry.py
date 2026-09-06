import base64
import json
from types import SimpleNamespace

import pytest

from app.subscriptions import build_clash_subscription_content, build_v2ray_subscription_content
from app.xray.render_config import build_client_config, build_server_config
from app.xray.unified_entry import account_uuid
from scripts.legacy_entry_forwarder import proxy_header
from scripts.render_entry_gateway import render_gateway
from tests.test_render_config_wrapper import ENV_CONTENT


def values():
    result = dict(line.split("=", 1) for line in ENV_CONTENT.splitlines() if "=" in line)
    result.update(XRAY_UNIFIED_PORT="443", XRAY_UNIFIED_UUID_SECRET="private-test-secret-" * 3)
    return result


def test_entry_preserves_legacy_auth_and_uses_private_account_credentials():
    env = values()
    config = build_server_config(env, panel_ports=[31098, 31333])
    legacy, _other, unified = config["inbounds"]
    assert legacy["settings"]["clients"][0]["id"] == env["XRAY_CLIENT_UUID"]
    assert "port" not in legacy
    assert legacy["listen"] == "/var/log/xray/entry-panel-31098.sock"
    assert all(i["streamSettings"]["sockopt"]["acceptProxyProtocol"] for i in config["inbounds"])
    users = unified["settings"]["clients"]
    assert users[0]["id"] == account_uuid(env, 31098)
    assert users[0]["id"] != users[1]["id"] != env["XRAY_CLIENT_UUID"]
    assert users[0]["email"] == "panel-user-31098"
    assert config["policy"]["levels"]["1"]["statsUserUplink"]
    assert build_server_config(env, panel_ports=[])["inbounds"][0]["settings"]["clients"] == []
    disabled = build_server_config(env, panel_ports=[31333])
    assert account_uuid(env, 31098) not in json.dumps(disabled)
    assert "panel-31098" not in json.dumps(disabled)


def test_unified_subscriptions_share_port_but_not_identity():
    env = values()
    client = build_client_config(env, [31098, 31333])
    profile = {
        "server": "example.com",
        "uuid": env["XRAY_CLIENT_UUID"],
        "flow": env["XRAY_FLOW"],
        "server_name": env["XRAY_SERVER_NAME"],
        "public_key": "public",
        "short_id": "abcd",
        "fingerprint": "chrome",
        "unified_port": 443,
        "user_uuids": client["panelSubscription"]["users"],
    }
    for port in [31098, 31333]:
        clash = build_clash_subscription_content(profile, port, "test")
        assert "    port: 443\n" in clash
        assert account_uuid(env, port) in clash
        assert env["XRAY_CLIENT_UUID"] not in clash
        uri = base64.b64decode(build_v2ray_subscription_content(profile, port, "test")).decode()
        assert f"{account_uuid(env, port)}@example.com:443?" in uri
    with pytest.raises(KeyError):
        build_clash_subscription_content(profile, 31340, "disabled")


def test_gateway_forwards_old_ports_through_443_with_original_destination():
    config = build_server_config(values(), panel_ports=[31098, 31333])
    gateway = render_gateway(config, "/srv/xray/logs")
    assert "expect-proxy layer4 if { src 127.0.0.2 }" in gateway
    assert "use_backend legacy_31098 if { dst_port 31098 }" in gateway
    assert "default_backend subscription_https" in gateway
    assert "server https 127.0.0.1:18443" in gateway
    assert "/srv/xray/logs/entry-unified-443.sock send-proxy-v2" in gateway
    assert "frontend legacy_aliases" not in gateway
    header = proxy_header(("198.51.100.2", 50000), ("203.0.113.4", 31098), 2)
    assert header[-4:] == bytes.fromhex("c350797a")
    with pytest.raises(ValueError):
        render_gateway(config, "/srv/path\nwith injection")


def test_gateway_empty_accounts_and_conflicting_port_fail_closed():
    config = build_server_config(values(), panel_ports=[])
    assert "frontend legacy_aliases" not in render_gateway(config, "/srv/xray/logs")
    with pytest.raises(ValueError):
        build_server_config(values(), panel_ports=[443])
    env = values()
    env.pop("XRAY_UNIFIED_UUID_SECRET")
    with pytest.raises(ValueError):
        build_server_config(env, panel_ports=[31098])


def test_stats_add_legacy_and_user_counters_without_counting_unified_total():
    from app.state.base import CoreService

    stats = [
        {"name": "inbound>>>panel-31098>>>traffic>>>uplink", "value": 10},
        {"name": "user>>>panel-user-31098>>>traffic>>>uplink", "value": 20},
        {"name": "user>>>panel-user-31098>>>traffic>>>downlink", "value": 40},
        {"name": "inbound>>>unified-443>>>traffic>>>uplink", "value": 200},
    ]
    panel = SimpleNamespace(
        data_plane_running=lambda: True,
        data_plane=SimpleNamespace(run_statsquery=lambda *args: SimpleNamespace(stdout=json.dumps({"stat": stats}))),
    )
    assert CoreService(panel).read_xray_traffic_stats() == {31098: {"bytes_received": 30, "bytes_sent": 40}}


def test_access_logs_attribute_unified_users_and_keep_legacy_ports():
    from app.state.traffic import TrafficService

    traffic = TrafficService(None)
    prefix = "2026/09/06 12:00:00.000 from 192.0.2.1:123 accepted tcp:example.com:443 "
    assert traffic.parse_xray_access_log_line(prefix + "[panel-31098 >> direct]")[0] == 31098
    assert traffic.parse_xray_access_log_line(prefix + "[unified-443 >> direct] email: panel-user-31333")[0] == 31333
    assert traffic.parse_xray_access_log_line(prefix + "[unified-443 >> direct]") is None
