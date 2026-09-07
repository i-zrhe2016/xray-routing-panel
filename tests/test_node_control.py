import importlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

from app.xray.node_control import (
    REMOTE_FILE_DELTA_SCRIPT,
    DataPlaneConfig,
    DataPlaneController,
    build_temp_target_path,
)


def load_state_module(temp_root):
    data_dir = temp_root / "data"
    xray_dir = temp_root / "xray"
    runtime_dir = xray_dir / "runtime"
    logs_dir = xray_dir / "logs"
    reports_dir = xray_dir / "reports" / "hourly-domains"
    data_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "config.json").write_text("{}", encoding="utf-8")
    (runtime_dir / "dynamic-routing.json").write_text("{}", encoding="utf-8")
    (runtime_dir / "panel-ports.json").write_text("{\"ports\": []}\n", encoding="utf-8")

    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["DB_PATH"] = str(data_dir / "panel.db")
    os.environ["XRAY_ENV_FILE_PATH"] = str(xray_dir / ".env")
    os.environ["XRAY_CONFIG_PATH"] = str(runtime_dir / "config.json")
    os.environ["XRAY_PANEL_PORTS_PATH"] = str(runtime_dir / "panel-ports.json")
    os.environ["XRAY_ACCESS_LOG_PATH"] = str(logs_dir / "access.log")
    os.environ["DATAPLANE_LOCAL_BIN"] = ""
    os.environ["DATAPLANE_CONTAINER_NAME"] = ""

    if "flask" not in sys.modules:
        flask_stub = ModuleType("flask")
        flask_stub.request = SimpleNamespace(host="127.0.0.1")
        flask_stub.url_for = lambda *args, **kwargs: "/"
        sys.modules["flask"] = flask_stub

    if "app.config" in sys.modules:
        importlib.reload(sys.modules["app.config"])
    else:
        importlib.import_module("app.config")

    # PanelState is composed from app.state.* modules that import configuration
    # constants at module load time. Remove the whole package before each test
    # environment reload so stale node paths cannot leak between cases.
    for module_name in sorted(
        (name for name in sys.modules if name == "app.state" or name.startswith("app.state.")),
        key=len,
        reverse=True,
    ):
        sys.modules.pop(module_name, None)
    return importlib.import_module("app.state")


class NodeControlTest(unittest.TestCase):
    def setUp(self):
        self.original_environ = os.environ.copy()
        self.original_flask_module = sys.modules.get("flask")
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.original_environ)
        if self.original_flask_module is None:
            sys.modules.pop("flask", None)
        else:
            sys.modules["flask"] = self.original_flask_module
        self.tempdir.cleanup()

    def test_remote_data_plane_syncs_before_validation(self):
        os.environ["DATAPLANE_SSH_TARGET"] = "root@default-node"
        os.environ["DATAPLANE_CONFIG_PATH"] = "/etc/xray/config.json"
        os.environ["DATAPLANE_PANEL_PORTS_PATH"] = "/etc/xray/panel-ports.json"
        state_module = load_state_module(self.root)
        state = state_module.PanelState()
        calls = []

        def fake_sync(validate_config=False):
            calls.append(validate_config)
            return ["/etc/xray/config.json"]

        state.data_plane.sync_generated_files = fake_sync
        uploaded = state.xray_config_test()

        self.assertEqual(calls, [True])
        self.assertEqual(uploaded, ["/etc/xray/config.json"])

    def test_panel_builds_multiple_ai_node_controllers_and_aggregates_status(self):
        os.environ["AI_NODE_SSH_TARGETS"] = "root@hawaii,root@taiwan"
        os.environ["AI_NODE_IDS"] = "hawaii,taiwan"
        os.environ["AI_NODE_LABELS"] = "AI 夏威夷,AI 台湾"
        os.environ["AI_NODE_CONTAINER_NAMES"] = "xray,xray-ai-node"
        os.environ["AI_NODE_API_SERVERS"] = "127.0.0.1:27166,127.0.0.1:27166"
        os.environ["AI_NODE_CONFIG_PATHS"] = ""
        state_module = load_state_module(self.root)
        state = state_module.PanelState()

        self.assertEqual(list(state.ai_nodes), ["hawaii", "taiwan"])
        self.assertEqual(state.ai_nodes["hawaii"].config.container_name, "xray")
        self.assertEqual(state.ai_nodes["taiwan"].config.container_name, "xray-ai-node")
        self.assertFalse(any(node.supports_sync() for node in state.ai_nodes.values()))

        state.ai_nodes["hawaii"].is_running = lambda: False
        state.ai_nodes["taiwan"].is_running = lambda: True
        status = state.ai_node_status()

        self.assertTrue(status["any_reachable"])
        self.assertFalse(status["all_reachable"])
        self.assertIsNone(status["xray_running"])
        self.assertEqual([node["node_id"] for node in status["nodes"]], ["hawaii", "taiwan"])

    def test_startup_restarts_remote_data_plane_when_synced_config_changed(self):
        os.environ["DATAPLANE_SSH_TARGET"] = "root@default-node"
        os.environ["DATAPLANE_CONFIG_PATH"] = "/etc/xray/config.json"
        state_module = load_state_module(self.root)
        state = state_module.PanelState()
        restarted = []

        state.connect = mock.MagicMock()
        state.render_panel_ports_payload = lambda conn: {"ports": []}
        state.write_json_file = lambda path, payload: None
        state.render_xray_config = lambda: None
        state.xray_config_test = lambda: ["/etc/xray/config.json"]
        state.data_plane.supports_restart = lambda: True
        state.restart_data_plane = lambda: restarted.append(True) or True

        state.write_current_config()

        self.assertEqual(restarted, [True])

    def test_remote_command_timeout_is_reported(self):
        controller = DataPlaneController(
            DataPlaneConfig(
                role="data_plane",
                label="数据面",
                ssh_target="root@example.com",
                remote_command_timeout=0.5,
            )
        )

        with mock.patch(
            "app.xray.node_control.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["ssh"], 0.5),
        ):
            with self.assertRaisesRegex(RuntimeError, "命令执行超时"):
                controller._run_remote(["true"], "数据面命令失败")

    def test_direct_ssh_ignores_identity_options_and_allows_password_auth(self):
        controller = DataPlaneController(
            DataPlaneConfig(
                role="data_plane",
                label="数据面",
                ssh_target="root@example.com",
                ssh_options=("-i", "/wrong/key", "-o", "IdentitiesOnly=no", "-o", "ConnectTimeout=5"),
                ssh_known_hosts_file="/root/.ssh/known_hosts",
            )
        )

        completed = subprocess.CompletedProcess(["ssh"], 0, stdout="", stderr="")
        with mock.patch("app.xray.node_control.subprocess.run", return_value=completed) as mocked_run:
            controller._run_remote(["true"], "数据面命令失败")

        command = mocked_run.call_args.args[0]
        self.assertNotIn("/wrong/key", command)
        self.assertNotIn("-i", command)
        self.assertIn("BatchMode=no", command)
        self.assertIn("PubkeyAuthentication=no", command)
        self.assertIn("PreferredAuthentications=password,keyboard-interactive", command)
        self.assertIn("PasswordAuthentication=yes", command)
        self.assertIn("KbdInteractiveAuthentication=yes", command)
        self.assertIn("ChallengeResponseAuthentication=yes", command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("UserKnownHostsFile=/root/.ssh/known_hosts", command)
        self.assertIn("ConnectTimeout=5", command)

    def test_remote_reality_probe_returns_remote_payload(self):
        controller = DataPlaneController(
            DataPlaneConfig(
                role="data_plane",
                label="数据面",
                ssh_target="root@example.com",
            )
        )
        controller._run_remote = mock.Mock(
            return_value=subprocess.CompletedProcess(
                ["ssh"],
                0,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "tls_handshake": True,
                        "cert_chain_valid": True,
                        "cert_matches_sni": True,
                    }
                ),
                stderr="",
            )
        )

        result = controller.probe_reality_endpoint("ai.example.com", 443, "www.example.com", 2)

        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "reality")
        controller._run_remote.assert_called_once()

    def test_remote_tcp_probe_returns_unreachable_payload_without_management_error(self):
        controller = DataPlaneController(
            DataPlaneConfig(
                role="data_plane",
                label="数据面",
                ssh_target="root@example.com",
            )
        )
        controller._run_remote = mock.Mock(
            return_value=subprocess.CompletedProcess(
                ["ssh"],
                0,
                stdout=json.dumps(
                    {
                        "ok": False,
                        "error": "timed out",
                        "method": "tcp",
                    }
                ),
                stderr="",
            )
        )

        result = controller.probe_tcp_endpoint("ai.example.com", 443, 2)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "timed out")
        self.assertFalse(result["management_error"])

    def test_restart_data_plane_returns_summary(self):
        os.environ["DATAPLANE_SSH_TARGET"] = "root@data-plane"
        state_module = load_state_module(self.root)
        state = state_module.PanelState()
        state.data_plane.is_configured = lambda: True
        state.data_plane.supports_restart = lambda: True
        state.data_plane.restart = lambda: True
        state.data_plane.status_summary = lambda: {"role": "data_plane", "label": "数据面", "configured": True}

        summary = state.restart_data_plane_or_raise()

        self.assertEqual(summary["role"], "data_plane")
        self.assertEqual(summary["label"], "数据面")

    def test_backup_restart_failure_aborts_port_artifact_commit(self):
        os.environ["CONTROL_PLANE_BACKUP_XRAY_ENABLED"] = "1"
        state_module = load_state_module(self.root)
        state = state_module.PanelState()
        state.init_db()
        panel_ports_path = self.root / "xray" / "runtime" / "panel-ports.json"
        config_path = self.root / "xray" / "runtime" / "config.json"
        backup_config_path = self.root / "xray" / "runtime" / "config-backup.json"
        previous_panel_ports = panel_ports_path.read_text(encoding="utf-8")
        previous_config = config_path.read_text(encoding="utf-8")
        previous_backup_config = '{"version":"previous-backup"}\n'
        backup_config_path.write_text(previous_backup_config, encoding="utf-8")

        state.render_xray_config = lambda: (
            config_path.write_text('{"version":"new-primary"}\n', encoding="utf-8"),
            backup_config_path.write_text('{"version":"new-backup"}\n', encoding="utf-8"),
        )
        state.xray_config_test = lambda: None
        state.restart_backup_xray = lambda: False

        with state.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(RuntimeError, "备用 Xray 重载失败"):
                try:
                    state.persist_and_reload(conn, reload_xray=False)
                except Exception:
                    conn.rollback()
                    raise

        self.assertEqual(panel_ports_path.read_text(encoding="utf-8"), previous_panel_ports)
        self.assertEqual(config_path.read_text(encoding="utf-8"), previous_config)
        self.assertEqual(backup_config_path.read_text(encoding="utf-8"), previous_backup_config)

    def test_config_failure_restores_database_files_and_running_nodes(self):
        for failure in ("restart_false", "restart_timeout", "commit"):
            for remote in (False, True):
                with self.subTest(failure=failure, remote=remote):
                    os.environ["CONTROL_PLANE_BACKUP_XRAY_ENABLED"] = "1"
                    os.environ["XRAY_CLIENT_CONFIG_PATH"] = str(self.root / "client.json")
                    state = load_state_module(self.root).PanelState()
                    state.init_db()
                    runtime = self.root / "xray" / "runtime"
                    paths = [
                        runtime / name
                        for name in ("config.json", "panel-ports.json", "config-backup.json", "client-share.txt")
                    ] + [self.root / "client.json"]
                    for path in paths:
                        path.write_text("old", encoding="utf-8")
                    running = {"primary": "old", "backup": "old"}
                    syncs = []

                    def render(paths=paths):
                        for path in paths:
                            path.write_text("new", encoding="utf-8")

                    def restart(running=running, paths=paths, failure=failure):
                        running["primary"] = paths[0].read_text(encoding="utf-8")
                        if running["primary"] == "new":
                            if failure == "restart_false":
                                return False
                            if failure == "restart_timeout":
                                raise RuntimeError("synthetic restart timeout")
                        return True

                    def restart_backup(running=running, paths=paths):
                        running["backup"] = paths[2].read_text(encoding="utf-8")
                        return True

                    state.render_xray_config = render
                    state.xray_config_test = lambda: None
                    state.restart_data_plane = mock.Mock(side_effect=restart)
                    state.restart_backup_xray = restart_backup
                    state.data_plane.supports_sync = lambda remote=remote: remote
                    state.data_plane.sync_generated_files = lambda syncs=syncs, paths=paths, **kw: syncs.append(
                        paths[0].read_text(encoding="utf-8")
                    )
                    with state.connect() as conn:
                        state.set_state(conn, "review_marker", "old")
                        conn.commit()
                        conn.execute("BEGIN IMMEDIATE")
                        state.set_state(conn, "review_marker", "new")
                        wrapped = mock.Mock(wraps=conn)
                        if failure == "commit":
                            wrapped.commit.side_effect = sqlite3.OperationalError("synthetic commit failure")
                        with self.assertRaises((RuntimeError, sqlite3.OperationalError)):
                            state.persist_and_reload(wrapped, reload_xray=True)
                        self.assertFalse(conn.in_transaction)
                    with state.connect() as conn:
                        self.assertEqual(state.get_state(conn, "review_marker"), "old")
                    self.assertTrue(all(path.read_text(encoding="utf-8") == "old" for path in paths))
                    self.assertEqual(running, {"primary": "old", "backup": "old"})
                    self.assertEqual(state.restart_data_plane.call_count, 2)
                    self.assertEqual(syncs, ["old"] if remote else [])

    def test_remote_sync_uses_unique_temp_config_with_json_suffix(self):
        source_config = self.root / "config.json"
        source_config.write_text("{}", encoding="utf-8")
        controller = DataPlaneController(
            DataPlaneConfig(
                role="data_plane",
                label="数据面",
                ssh_target="root@example.com",
                config_path="/etc/xray/config.json",
                source_config_path=source_config,
            )
        )
        remote_calls = []
        tested_paths = []

        def fake_run_remote(args, error_prefix, timeout=None, input_text=None):
            remote_calls.append((args, error_prefix, input_text))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        controller._run_remote = fake_run_remote
        controller.test_config = lambda config_path=None: tested_paths.append(config_path)

        uploaded = controller.sync_generated_files(validate_config=True)

        self.assertEqual(uploaded, ["/etc/xray/config.json"])
        self.assertEqual(len(tested_paths), 1)
        self.assertRegex(
            tested_paths[0],
            r"^/etc/xray/config\.codex-tmp-[0-9a-f]{32}\.json$",
        )
        self.assertEqual(remote_calls[0][0][-1], tested_paths[0])

    def test_temp_config_paths_are_unique(self):
        first = build_temp_target_path("/etc/xray/config.json")
        second = build_temp_target_path("/etc/xray/config.json")

        self.assertNotEqual(first, second)
        self.assertTrue(first.endswith(".json"))
        self.assertTrue(second.endswith(".json"))

    def test_remote_sync_does_not_report_unchanged_config(self):
        source_config = self.root / "config.json"
        source_config.write_text("{}", encoding="utf-8")
        controller = DataPlaneController(
            DataPlaneConfig(
                role="data_plane",
                label="数据面",
                ssh_target="root@example.com",
                config_path="/etc/xray/config.json",
                source_config_path=source_config,
            )
        )

        def fake_run_remote(args, error_prefix, timeout=None, input_text=None):
            if args[-1] == "/etc/xray/config.json":
                return SimpleNamespace(returncode=0, stdout='{"changed": false}', stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        controller._run_remote = fake_run_remote
        controller.test_config = lambda config_path=None: None

        uploaded = controller.sync_generated_files(validate_config=True)

        self.assertEqual(uploaded, [])

    def test_remote_access_log_script_filters_by_timestamp_and_keeps_final_offset(self):
        log_path = self.root / "access.log"
        log_path.write_text(
            "2026/08/31 10:59:59.000000 old accepted tcp:old.example.com:443 [direct]\n"
            "2026/08/31 11:30:00.000000 recent accepted tcp:api.openai.com:443 [direct]\n",
            encoding="utf-8",
        )
        script_path = self.root / "read-access-log.py"
        script_path.write_text(REMOTE_FILE_DELTA_SCRIPT, encoding="utf-8")
        cutoff = datetime(2026, 8, 31, 11, 0, tzinfo=timezone.utc).timestamp()

        completed = subprocess.run(
            [sys.executable, str(script_path), str(log_path), "", "0", str(cutoff)],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertNotIn("old.example.com", payload["data"])
        self.assertIn("api.openai.com", payload["data"])
        self.assertEqual(payload["offset"], log_path.stat().st_size)

    def test_remote_access_log_delta_passes_optional_timestamp_cutoff(self):
        controller = DataPlaneController(
            DataPlaneConfig(
                role="data_plane",
                label="数据面",
                ssh_target="root@example.com",
                access_log_path="/var/log/xray/access.log",
            )
        )
        calls = []

        def fake_run_remote(args, error_prefix, timeout=None, input_text=None):
            calls.append(args)
            return SimpleNamespace(
                returncode=0,
                stdout='{"exists": true, "inode": "1", "offset": 12, "data": ""}',
                stderr="",
            )

        controller._run_remote = fake_run_remote

        result = controller.read_access_log_delta("1", 8, since_epoch=123.5)

        self.assertTrue(result["exists"])
        self.assertEqual(calls[0][-1], "123.5")

    def test_remote_dynamic_routing_sync_updates_local_copy(self):
        local_path = self.root / "dynamic-routing.json"
        controller = DataPlaneController(
            DataPlaneConfig(
                role="data_plane",
                label="数据面",
                ssh_target="root@example.com",
                dynamic_routing_path="/etc/xray/dynamic-routing.json",
                source_dynamic_routing_path=local_path,
            )
        )

        controller._run_remote = lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"exists": true, "data": "{\\"routing\\": {\\"rules\\": []}}"}',
            stderr="",
        )

        changed = controller.sync_dynamic_routing_from_remote()

        self.assertTrue(changed)
        self.assertEqual(local_path.read_text(encoding="utf-8"), '{"routing": {"rules": []}}')

    def test_remote_generated_sync_uploads_dynamic_routing_fragment(self):
        source_config = self.root / "config.json"
        source_dynamic = self.root / "dynamic-routing.json"
        source_config.write_text("{}", encoding="utf-8")
        source_dynamic.write_text('{"outbounds": [{"tag": "ai_proxy"}]}', encoding="utf-8")
        controller = DataPlaneController(
            DataPlaneConfig(
                role="data_plane",
                label="数据面",
                ssh_target="root@example.com",
                config_path="/etc/xray/config.json",
                source_config_path=source_config,
                dynamic_routing_path="/etc/xray/dynamic-routing.json",
                source_dynamic_routing_path=source_dynamic,
            )
        )
        remote_calls = []

        def fake_run_remote(args, error_prefix, timeout=None, input_text=None):
            remote_calls.append((args, input_text))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        controller._run_remote = fake_run_remote

        uploaded = controller.sync_generated_files()

        self.assertEqual(uploaded, ["/etc/xray/config.json", "/etc/xray/dynamic-routing.json"])
        self.assertEqual(remote_calls[-1][0][-1], "/etc/xray/dynamic-routing.json")
        self.assertEqual(remote_calls[-1][1], source_dynamic.read_text(encoding="utf-8"))

    def test_remote_generated_sync_deletes_missing_dynamic_routing_fragment(self):
        source_config = self.root / "config.json"
        source_dynamic = self.root / "dynamic-routing.json"
        source_config.write_text("{}", encoding="utf-8")
        controller = DataPlaneController(
            DataPlaneConfig(
                role="data_plane",
                label="数据面",
                ssh_target="root@example.com",
                config_path="/etc/xray/config.json",
                source_config_path=source_config,
                dynamic_routing_path="/etc/xray/dynamic-routing.json",
                source_dynamic_routing_path=source_dynamic,
            )
        )
        remote_calls = []
        controller._run_remote = lambda args, error_prefix, timeout=None, input_text=None: (
            remote_calls.append((args, input_text))
            or SimpleNamespace(returncode=0, stdout="", stderr="")
        )

        controller.sync_generated_files()

        self.assertEqual(remote_calls[-1][0][-1], "/etc/xray/dynamic-routing.json")
        self.assertIsNone(remote_calls[-1][1])

    def test_remote_ai_report_sync_updates_local_copy(self):
        local_path = self.root / "reports" / "hourly-domains" / "latest.json"
        controller = DataPlaneController(
            DataPlaneConfig(
                role="data_plane",
                label="数据面",
                ssh_target="root@example.com",
                ai_report_path="/srv/xray/reports/hourly-domains/latest.json",
                source_ai_report_path=local_path,
            )
        )

        controller._run_remote = lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"exists": true, "data": "{\\"generated_at\\": \\"2026-06-18T00:00:00+00:00\\"}"}',
            stderr="",
        )

        changed = controller.sync_ai_report_from_remote()

        self.assertTrue(changed)
        self.assertEqual(
            local_path.read_text(encoding="utf-8"),
            '{"generated_at": "2026-06-18T00:00:00+00:00"}',
        )

    def test_remote_ai_report_sync_keeps_local_copy_when_remote_missing(self):
        local_path = self.root / "reports" / "hourly-domains" / "latest.json"
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(
            '{"generated_at": "2026-06-18T00:00:00+00:00"}',
            encoding="utf-8",
        )
        controller = DataPlaneController(
            DataPlaneConfig(
                role="data_plane",
                label="数据面",
                ssh_target="root@example.com",
                ai_report_path="/srv/xray/reports/hourly-domains/latest.json",
                source_ai_report_path=local_path,
            )
        )

        controller._run_remote = lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"exists": false, "data": ""}',
            stderr="",
        )

        changed = controller.sync_ai_report_from_remote()

        self.assertFalse(changed)
        self.assertTrue(local_path.is_file())
        self.assertEqual(
            local_path.read_text(encoding="utf-8"),
            '{"generated_at": "2026-06-18T00:00:00+00:00"}',
        )

    def test_sync_data_plane_ai_state_replaces_local_snapshot(self):
        os.environ["DATAPLANE_SSH_TARGET"] = "root@default-node"
        os.environ["DATAPLANE_AI_REPORT_PATH"] = "/srv/xray/reports/hourly-domains/latest.json"
        os.environ["DATAPLANE_PANEL_DB_PATH"] = "/srv/xray/data/panel.db"
        state_module = load_state_module(self.root)
        state = state_module.PanelState()
        report_path = state.data_plane.config.source_ai_report_path

        report = {
            "generated_at": "2026-06-18T00:00:00+00:00",
            "window_start": "2026-06-17T23:00:00+00:00",
            "window_end": "2026-06-18T00:00:00+00:00",
            "unique_domains": 2,
            "domains": [
                {
                    "domain": "openai.com",
                    "hits": 4,
                    "first_seen": "2026-06-17T23:10:00+00:00",
                    "last_seen": "2026-06-17T23:58:00+00:00",
                    "protocols": ["tcp"],
                    "classification": "ai",
                    "reason": "known ai",
                }
            ],
            "protocols": [{"protocol": "tcp", "hits": 4}],
            "ai_target": {"upstream_host": "ai.example.com", "upstream_port": 443},
            "panel_target": {"listen_port": 31001, "upstream_host": "panel.example.com", "upstream_port": 443},
            "route_status": {"status": "applied", "reason": ""},
        }

        def fake_sync_report():
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report), encoding="utf-8")
            return True

        state.data_plane.sync_ai_report_from_remote = fake_sync_report
        state.data_plane.read_ai_domains_snapshot_from_remote = lambda: {
            "exists": True,
            "ai_domains": [
                {
                    "domain": "openai.com",
                    "classification": "ai",
                    "reason": "known ai",
                    "source": "codex",
                    "model": "gpt-5.5",
                    "first_seen": "2026-06-17T23:10:00+00:00",
                    "last_seen": "2026-06-17T23:58:00+00:00",
                    "total_hits": 9,
                    "last_protocols": "[\"tcp\"]",
                    "last_report_window_start": "2026-06-17T23:00:00+00:00",
                    "last_report_window_end": "2026-06-18T00:00:00+00:00",
                    "updated_at": "2026-06-18T00:00:00+00:00",
                }
            ],
        }

        result = state.sync_data_plane_ai_state()

        self.assertTrue(result["report_synced"])
        self.assertTrue(result["snapshot_synced"])
        self.assertEqual(state.read_ai_domain_report()["ai_domain_count"], 1)
        with state.connect() as conn:
            row = conn.execute(
                "SELECT domain, total_hits, source FROM ai_domains WHERE domain = ?",
                ("openai.com",),
            ).fetchone()
            observations = conn.execute("SELECT COUNT(*) FROM ai_domain_observations").fetchone()[0]
        self.assertEqual(dict(row), {"domain": "openai.com", "total_hits": 9, "source": "codex"})
        self.assertEqual(observations, 0)

    def test_read_ai_domain_report_accepts_pending_domain_list(self):
        state_module = load_state_module(self.root)
        state = state_module.PanelState()
        report_path = state.data_plane.config.source_ai_report_path
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(
                {
                    "generated_at": "2026-06-19T06:00:00+00:00",
                    "window_start": "2026-06-19T05:00:00+00:00",
                    "window_end": "2026-06-19T06:00:00+00:00",
                    "unique_domains": 1,
                    "domains": [],
                    "protocols": [],
                    "route_status": {
                        "status": "pending",
                        "reason": "classifier_disabled",
                        "pending_domains_without_classifier": [
                            "api.example.com",
                            "cdn.example.com",
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )

        report = state.read_ai_domain_report()

        self.assertEqual(report["route_status"], "pending")
        self.assertEqual(report["pending_domains_without_classifier"], 2)

    def test_render_xray_config_pulls_remote_dynamic_routing_first(self):
        os.environ["DATAPLANE_SSH_TARGET"] = "root@default-node"
        os.environ["DATAPLANE_CONFIG_PATH"] = "/etc/xray/config.json"
        os.environ["DATAPLANE_DYNAMIC_ROUTING_PATH"] = "/etc/xray/dynamic-routing.json"
        state_module = load_state_module(self.root)
        state = state_module.PanelState()
        calls = []

        state.data_plane.sync_dynamic_routing_from_remote = lambda: calls.append("pull")
        state.run_command = lambda command, error_prefix, timeout=None: calls.append("render")

        state.render_xray_config()

        self.assertEqual(calls, ["pull", "render"])


if __name__ == "__main__":
    unittest.main()
