import importlib
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace


def load_state_module(temp_root, extra_env=None):
    data_dir = temp_root / "data"
    xray_dir = temp_root / "xray"
    runtime_dir = xray_dir / "runtime"
    logs_dir = xray_dir / "logs"
    data_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "config.json").write_text("{}", encoding="utf-8")
    (runtime_dir / "panel-ports.json").write_text("{\"ports\": []}\n", encoding="utf-8")
    (xray_dir / ".env").write_text("XRAY_PUBLIC_HOST=panel.example.com\nXRAY_CLIENT_UUID=test\n", encoding="utf-8")

    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["DB_PATH"] = str(data_dir / "panel.db")
    os.environ["XRAY_ENV_FILE_PATH"] = str(xray_dir / ".env")
    os.environ["XRAY_CONFIG_PATH"] = str(runtime_dir / "config.json")
    os.environ["XRAY_PANEL_PORTS_PATH"] = str(runtime_dir / "panel-ports.json")
    os.environ["XRAY_ACCESS_LOG_PATH"] = str(logs_dir / "access.log")
    os.environ["SEED_LISTEN_PORT"] = ""
    os.environ["DATAPLANE_LOCAL_BIN"] = ""
    os.environ["DATAPLANE_CONTAINER_NAME"] = ""
    os.environ["DNS_FAILOVER_ENABLED"] = "1"
    os.environ["DNS_FAILOVER_INTERVAL"] = "15"
    os.environ["DNS_FAILOVER_TIMEOUT"] = "1"
    os.environ["DNS_FAILOVER_FAILURE_THRESHOLD"] = "2"
    os.environ["DNS_FAILOVER_RECOVERY_THRESHOLD"] = "2"
    os.environ["DNS_FAILOVER_PROBE_HOST"] = "edge.example.com"
    os.environ["DNS_FAILOVER_PROBE_PORT"] = "443"
    os.environ["CF_API_TOKEN"] = "test-token"
    os.environ["CF_ZONE_ID"] = "zone-id"
    os.environ["CF_DNS_RECORD_ID"] = "record-id"
    os.environ["CF_DNS_RECORD_TYPE"] = "A"
    os.environ["CF_DNS_RECORD_NAME"] = "edge.example.com"
    os.environ["CF_DNS_RECORD_PROXIED"] = "0"
    os.environ["CF_DNS_RECORD_TTL"] = "60"
    os.environ["DNS_FAILOVER_PRIMARY_CONTENT"] = "1.1.1.1"
    os.environ["DNS_FAILOVER_BACKUP_CONTENT"] = "2.2.2.2"
    os.environ["DNS_FAILOVER_BACKUP_LABEL"] = "控制面备用节点"
    if extra_env:
        for key, value in extra_env.items():
            os.environ[key] = value

    flask_stub = ModuleType("flask")
    flask_stub.request = SimpleNamespace(host="127.0.0.1")
    flask_stub.url_for = lambda *args, **kwargs: "/"
    sys.modules["flask"] = flask_stub

    werkzeug_stub = ModuleType("werkzeug")
    werkzeug_security_stub = ModuleType("werkzeug.security")
    werkzeug_security_stub.generate_password_hash = lambda value, method="scrypt", salt_length=16: f"hash:{value}"
    werkzeug_security_stub.check_password_hash = lambda hashed, value: hashed == f"hash:{value}"
    werkzeug_stub.security = werkzeug_security_stub
    sys.modules["werkzeug"] = werkzeug_stub
    sys.modules["werkzeug.security"] = werkzeug_security_stub

    for module_name in list(sys.modules):
        if (
            module_name == "app.config"
            or module_name.startswith("app.config.")
            or module_name == "app.dns_failover"
            or module_name.startswith("app.state")
        ):
            sys.modules.pop(module_name, None)
    state_module = importlib.import_module("app.state")
    return importlib.reload(state_module)


class DnsFailoverTest(unittest.TestCase):
    def setUp(self):
        self.original_environ = os.environ.copy()
        self.original_flask = sys.modules.get("flask")
        self.original_werkzeug = sys.modules.get("werkzeug")
        self.original_werkzeug_security = sys.modules.get("werkzeug.security")
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.original_environ)
        if self.original_flask is None:
            sys.modules.pop("flask", None)
        else:
            sys.modules["flask"] = self.original_flask
        if self.original_werkzeug is None:
            sys.modules.pop("werkzeug", None)
        else:
            sys.modules["werkzeug"] = self.original_werkzeug
        if self.original_werkzeug_security is None:
            sys.modules.pop("werkzeug.security", None)
        else:
            sys.modules["werkzeug.security"] = self.original_werkzeug_security
        self.tempdir.cleanup()

    def build_state(self, extra_env=None):
        state_module = load_state_module(self.root, extra_env=extra_env)
        state = state_module.PanelState()
        state.render_xray_config = lambda: None
        state.xray_config_test = lambda: None
        state.restart_data_plane = lambda: True
        state.init_db()
        return state, state_module

    def seed_record(self, state, content):
        state.dns_failover_manager.get_record = lambda: {
            "content": content,
            "ttl": 60,
            "proxied": False,
        }
        state.refresh_dns_failover_record_snapshot()

    def test_dns_failover_status_disabled(self):
        state, _state_module = self.build_state({"DNS_FAILOVER_ENABLED": "0"})

        status = state.dns_failover_status()

        self.assertFalse(status["enabled"])
        self.assertFalse(status["configured"])

    def test_dns_failover_incomplete_config_records_error_on_check(self):
        state, _state_module = self.build_state({"CF_API_TOKEN": ""})

        status = state.run_dns_failover_check()

        self.assertFalse(status["configured"])
        self.assertIn("CF_API_TOKEN", status["config_error"])
        self.assertIn("CF_API_TOKEN", status["last_error"])

    def test_dns_failover_auto_resolves_primary_and_backup_contents(self):
        state, _state_module = self.build_state(
            {
                "CONTROL_PLANE_BACKUP_XRAY_ENABLED": "1",
                "DNS_FAILOVER_PRIMARY_CONTENT": "",
                "DNS_FAILOVER_BACKUP_CONTENT": "",
            }
        )
        state.data_plane.resolve_public_ip = lambda timeout_seconds=5: "1.1.1.1"
        state.resolve_dns_failover_contents.__globals__["resolve_public_ip"] = lambda timeout=5.0: "2.2.2.2"

        status = state.dns_failover_status()

        self.assertTrue(status["configured"])
        self.assertEqual(status["primary_content"], "1.1.1.1")
        self.assertEqual(status["backup_content"], "2.2.2.2")

    def test_dns_failover_requires_explicit_backup_when_control_plane_backup_disabled(self):
        state, _state_module = self.build_state(
            {
                "CONTROL_PLANE_BACKUP_XRAY_ENABLED": "0",
                "DNS_FAILOVER_BACKUP_CONTENT": "",
            }
        )

        status = state.dns_failover_status()

        self.assertFalse(status["configured"])
        self.assertIn("CONTROL_PLANE_BACKUP_XRAY_ENABLED", status["config_error"])

    def test_dns_failover_switches_to_backup_after_threshold(self):
        state, _state_module = self.build_state()
        self.seed_record(state, "1.1.1.1")
        state.dns_failover_manager.probe_once = lambda: {"ok": False, "error": "timeout"}
        switch_calls = []
        state.dns_failover_manager.sync_target = lambda target, primary_content=None, backup_content=None: switch_calls.append(target) or {
            "content": "2.2.2.2",
            "ttl": 60,
            "proxied": False,
        }

        first = state.run_dns_failover_check()
        second = state.run_dns_failover_check()

        self.assertEqual(first["current_target"], "primary")
        self.assertEqual(second["current_target"], "backup")
        self.assertEqual(second["last_switch_reason"], "auto_failover")
        self.assertEqual(switch_calls, ["backup"])

    def test_dns_failover_does_not_repeat_backup_switch(self):
        state, _state_module = self.build_state()
        self.seed_record(state, "2.2.2.2")
        state.dns_failover_manager.probe_once = lambda: {"ok": False, "error": "timeout"}
        switch_calls = []
        state.dns_failover_manager.sync_target = lambda target, primary_content=None, backup_content=None: switch_calls.append(target) or {
            "content": "2.2.2.2",
            "ttl": 60,
            "proxied": False,
        }

        state.run_dns_failover_check()
        state.run_dns_failover_check()

        self.assertEqual(switch_calls, [])
        self.assertEqual(state.dns_failover_status()["current_target"], "backup")

    def test_dns_failover_can_fail_over_again_after_recovery(self):
        state, _state_module = self.build_state()
        self.seed_record(state, "1.1.1.1")
        probe_results = iter(
            [
                {"ok": False, "error": "first outage"},
                {"ok": False, "error": "first outage"},
                {"ok": True, "error": ""},
                {"ok": True, "error": ""},
                {"ok": False, "error": "second outage"},
                {"ok": False, "error": "second outage"},
            ]
        )
        switch_calls = []
        current_record = {"content": "1.1.1.1"}

        state.dns_failover_manager.probe_once = lambda: next(probe_results)
        state.dns_failover_manager.get_record = lambda: {
            "content": current_record["content"],
            "ttl": 60,
            "proxied": False,
        }

        def sync_target(target, primary_content=None, backup_content=None):
            switch_calls.append(target)
            current_record["content"] = "1.1.1.1" if target == "primary" else "2.2.2.2"
            return {
                "content": current_record["content"],
                "ttl": 60,
                "proxied": False,
            }

        state.dns_failover_manager.sync_target = sync_target

        results = [state.run_dns_failover_check() for _ in range(6)]

        self.assertEqual(switch_calls, ["backup", "primary", "backup"])
        self.assertEqual(results[-1]["current_target"], "backup")
        self.assertEqual(results[-1]["last_switch_reason"], "auto_failover")

    def test_explicit_failover_contents_do_not_query_data_plane_ip(self):
        state, _state_module = self.build_state()
        state.data_plane.resolve_public_ip = lambda timeout_seconds=5.0: self.fail(
            "DNS failover must not query the data plane when contents are explicit"
        )

        status = state.dns_failover_status()

        self.assertTrue(status["configured"])

    def test_remote_failover_requires_explicit_primary_content(self):
        state, _state_module = self.build_state(
            {
                "DATAPLANE_SSH_TARGET": "root@data-plane",
                "DNS_FAILOVER_PRIMARY_CONTENT": "",
            }
        )
        state.data_plane.resolve_public_ip = lambda timeout_seconds=5.0: self.fail(
            "remote failover must not resolve primary IP through the data plane"
        )

        status = state.dns_failover_status()

        self.assertFalse(status["configured"])
        self.assertIn("DNS_FAILOVER_PRIMARY_CONTENT", status["config_error"])

    def test_dns_worker_runs_when_maintenance_task_blocks(self):
        state, _state_module = self.build_state(
            {
                "DNS_FAILOVER_INTERVAL": "1",
                "MAINTENANCE_INTERVAL": "1",
            }
        )
        worker_ran = threading.Event()

        state.sync_traffic_state = lambda: threading.Event().wait(3)
        state.run_dns_failover_check = lambda: worker_ran.set()

        maintenance_thread = threading.Thread(target=state.maintenance_loop, daemon=True)
        failover_thread = threading.Thread(target=state.dns_failover_loop, daemon=True)
        maintenance_thread.start()
        failover_thread.start()

        self.assertTrue(worker_ran.wait(2))

        state.stop_event.set()
        maintenance_thread.join(timeout=1)
        failover_thread.join(timeout=1)

    def test_dns_failover_recovers_to_primary_after_threshold(self):
        state, _state_module = self.build_state()
        self.seed_record(state, "2.2.2.2")
        state.dns_failover_manager.probe_once = lambda: {"ok": True, "error": ""}
        switch_calls = []
        state.dns_failover_manager.sync_target = lambda target, primary_content=None, backup_content=None: switch_calls.append(target) or {
            "content": "1.1.1.1",
            "ttl": 60,
            "proxied": False,
        }

        first = state.run_dns_failover_check()
        second = state.run_dns_failover_check()

        self.assertEqual(first["current_target"], "backup")
        self.assertEqual(second["current_target"], "primary")
        self.assertEqual(second["last_switch_reason"], "auto_recovery")
        self.assertEqual(switch_calls, ["primary"])

    def test_dns_failover_api_failure_records_error(self):
        state, state_module = self.build_state()
        self.seed_record(state, "1.1.1.1")
        state.dns_failover_manager.probe_once = lambda: {"ok": False, "error": "timeout"}

        def fail_switch(_target, primary_content=None, backup_content=None):
            raise state_module.CloudflareApiError("api denied")

        state.dns_failover_manager.sync_target = fail_switch

        state.run_dns_failover_check()
        with self.assertRaises(state_module.ValidationError):
            state.run_dns_failover_check()

        status = state.dns_failover_status()
        self.assertEqual(status["current_target"], "primary")
        self.assertEqual(status["last_error"], "api denied")

    def test_manual_switch_updates_state(self):
        state, _state_module = self.build_state()
        self.seed_record(state, "1.1.1.1")
        state.dns_failover_manager.sync_target = lambda _target, primary_content=None, backup_content=None: {
            "content": "2.2.2.2",
            "ttl": 60,
            "proxied": False,
        }

        status = state.switch_dns_target("backup")

        self.assertEqual(status["current_target"], "backup")
        self.assertEqual(status["last_switch_reason"], "manual_switch")
        self.assertEqual(status["record_content"], "2.2.2.2")

    def test_peak_window_prefers_backup_during_configured_hours(self):
        state, _state_module = self.build_state(
            {
                "DNS_FAILOVER_PEAK_ENABLED": "1",
                "DNS_FAILOVER_PEAK_START": "19:00",
                "DNS_FAILOVER_PEAK_END": "23:00",
                "DNS_FAILOVER_PEAK_TIMEZONE": "+08:00",
            }
        )

        active = state.dns_failover_peak_window_status(datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc))
        inactive = state.dns_failover_peak_window_status(datetime(2026, 6, 22, 16, 0, tzinfo=timezone.utc))

        self.assertTrue(active["configured"])
        self.assertTrue(active["active"])
        self.assertEqual(active["preferred_target"], "backup")
        self.assertFalse(inactive["active"])
        self.assertEqual(inactive["preferred_target"], "primary")

    def test_peak_window_reports_next_transition(self):
        state, _state_module = self.build_state(
            {
                "DNS_FAILOVER_PEAK_ENABLED": "1",
                "DNS_FAILOVER_PEAK_START": "19:00",
                "DNS_FAILOVER_PEAK_END": "23:00",
                "DNS_FAILOVER_PEAK_TIMEZONE": "+08:00",
            }
        )

        # 20:00 +08:00 == 12:00 UTC: inside the window, flips back to primary at 23:00.
        active = state.dns_failover_peak_window_status(datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc))
        self.assertTrue(active["active"])
        self.assertEqual(active["next_preferred_target"], "primary")
        self.assertEqual(active["next_transition_at"][-5:], "23:00")
        self.assertEqual(active["seconds_to_next_transition"], 3 * 3600)

        # 16:00 +08:00 == 08:00 UTC: outside the window, flips to backup at 19:00.
        inactive = state.dns_failover_peak_window_status(datetime(2026, 6, 22, 8, 0, tzinfo=timezone.utc))
        self.assertFalse(inactive["active"])
        self.assertEqual(inactive["next_preferred_target"], "backup")
        self.assertEqual(inactive["next_transition_at"][-5:], "19:00")
        self.assertEqual(inactive["seconds_to_next_transition"], 3 * 3600)

    def test_run_dns_failover_check_switches_to_backup_during_peak_window(self):
        state, _state_module = self.build_state(
            {
                "DNS_FAILOVER_PEAK_ENABLED": "1",
                "DNS_FAILOVER_PEAK_START": "19:00",
                "DNS_FAILOVER_PEAK_END": "23:00",
                "DNS_FAILOVER_PEAK_TIMEZONE": "+00:00",
            }
        )
        self.seed_record(state, "1.1.1.1")
        state.dns_failover_peak_window_status = lambda now=None: {
            "enabled": True,
            "configured": True,
            "active": True,
            "preferred_target": "backup",
            "preferred_target_label": "控制面备用节点",
            "config_error": "",
        }
        state.dns_failover_manager.probe_once = lambda: {"ok": True, "error": ""}
        switch_calls = []
        state.dns_failover_manager.sync_target = lambda target, primary_content=None, backup_content=None: switch_calls.append(target) or {
            "content": "2.2.2.2",
            "ttl": 60,
            "proxied": False,
        }

        first = state.run_dns_failover_check()
        second = state.run_dns_failover_check()

        self.assertEqual(first["current_target"], "primary")
        self.assertEqual(second["current_target"], "backup")
        self.assertEqual(second["last_switch_reason"], "peak_recovery")
        self.assertEqual(switch_calls, ["backup"])

    def test_peak_window_missing_hours_marks_status_unconfigured(self):
        state, _state_module = self.build_state(
            {
                "DNS_FAILOVER_PEAK_ENABLED": "1",
                "DNS_FAILOVER_PEAK_START": "",
                "DNS_FAILOVER_PEAK_END": "",
            }
        )

        status = state.dns_failover_status()

        self.assertFalse(status["configured"])
        self.assertIn("DNS_FAILOVER_PEAK_START", status["config_error"])


if __name__ == "__main__":
    unittest.main()
