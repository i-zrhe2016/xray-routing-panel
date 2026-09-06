import importlib
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace


def load_state_module(temp_root, api_server):
    data_dir = temp_root / "data"
    xray_dir = temp_root / "xray"
    runtime_dir = xray_dir / "runtime"
    logs_dir = xray_dir / "logs"
    data_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "config.json").write_text("{}", encoding="utf-8")

    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["DB_PATH"] = str(data_dir / "panel.db")
    os.environ["XRAY_ENV_FILE_PATH"] = str(xray_dir / ".env")
    os.environ["XRAY_CONFIG_PATH"] = str(runtime_dir / "config.json")
    os.environ["XRAY_PANEL_PORTS_PATH"] = str(runtime_dir / "panel-ports.json")
    os.environ["XRAY_ACCESS_LOG_PATH"] = str(logs_dir / "access.log")
    os.environ["DATAPLANE_API_SERVER"] = api_server
    os.environ["DATAPLANE_LOCAL_BIN"] = shutil.which("true") or "/bin/true"
    os.environ["DATAPLANE_CONTAINER_NAME"] = ""

    if "flask" not in sys.modules:
        flask_stub = ModuleType("flask")
        flask_stub.request = SimpleNamespace(host="127.0.0.1")
        flask_stub.url_for = lambda *args, **kwargs: "/"
        sys.modules["flask"] = flask_stub

    for module_name in ["app.config", "app.state"]:
        if module_name in sys.modules:
            importlib.reload(sys.modules[module_name])
        else:
            importlib.import_module(module_name)
    return importlib.reload(importlib.import_module("app.state"))


class LocalXrayModeTest(unittest.TestCase):
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

    def test_data_plane_running_uses_local_api_socket(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        host, port = server.getsockname()
        state_module = load_state_module(self.root, f"{host}:{port}")
        def accept_once():
            try:
                connection, _address = server.accept()
            except OSError:
                return
            connection.close()

        accepted = threading.Thread(target=accept_once, daemon=True)
        accepted.start()
        try:
            self.assertTrue(state_module.PanelState().data_plane_running())
        finally:
            server.close()
            accepted.join(timeout=1)

    def test_xray_config_test_uses_local_binary(self):
        state_module = load_state_module(self.root, "127.0.0.1:10085")
        state = state_module.PanelState()
        calls = []

        def fake_test_config(config_path=None):
            calls.append((config_path,))
            return SimpleNamespace(stdout="")

        state.data_plane.test_config = fake_test_config
        state.xray_config_test()

        self.assertEqual(len(calls), 1)
        (config_path,) = calls[0]
        self.assertIsNone(config_path)
        self.assertEqual(state.data_plane.config.local_bin, os.environ["DATAPLANE_LOCAL_BIN"])

    def test_read_xray_traffic_stats_uses_local_binary(self):
        state_module = load_state_module(self.root, "127.0.0.1:10085")
        state = state_module.PanelState()
        commands = []
        state.data_plane_running = lambda: True

        def fake_run_statsquery(timeout_seconds, pattern):
            commands.append((timeout_seconds, pattern))
            return SimpleNamespace(stdout=json.dumps({"stat": []}))

        state.data_plane.run_statsquery = fake_run_statsquery
        self.assertEqual(state.read_xray_traffic_stats(), {})
        self.assertEqual(len(commands), 1)
        timeout_seconds, pattern = commands[0]
        self.assertEqual(timeout_seconds, int(os.environ.get("XRAY_STATS_QUERY_TIMEOUT", "5")))
        self.assertEqual(pattern, ">>>panel-")
        self.assertEqual(state.data_plane.config.local_bin, os.environ["DATAPLANE_LOCAL_BIN"])
