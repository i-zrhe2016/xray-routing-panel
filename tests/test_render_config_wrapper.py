import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "scripts" / "render_config.py"

ENV_CONTENT = """\
XRAY_LISTEN_HOST=0.0.0.0
XRAY_LISTEN_PORT=443
XRAY_PUBLIC_HOST=panel.example.com
XRAY_PUBLIC_PORT=31098
XRAY_CLIENT_UUID=11111111-1111-1111-1111-111111111111
XRAY_FLOW=xtls-rprx-vision
XRAY_REALITY_PRIVATE_KEY=private-key-example
XRAY_REALITY_PUBLIC_KEY=public-key-example
XRAY_REALITY_SHORT_ID=0123456789abcdef
XRAY_SERVER_NAME=www.microsoft.com
XRAY_DEST=www.microsoft.com:443
XRAY_FINGERPRINT=chrome
XRAY_LOGLEVEL=warning
XRAY_NODE_TAG=test-node
AI_NODE_CLIENT_UUID=22222222-2222-2222-2222-222222222222
AI_NODE_FLOW=xtls-rprx-vision
AI_NODE_REALITY_PRIVATE_KEY=ai-private-key-example
AI_NODE_REALITY_PUBLIC_KEY=ai-public-key-example
AI_NODE_REALITY_SHORT_ID=fedcba9876543210
AI_NODE_SERVER_NAME=www.amazon.com
AI_NODE_DEST=www.amazon.com:443
AI_NODE_FINGERPRINT=chrome
"""


class RenderConfigWrapperTest(unittest.TestCase):
    def test_wrapper_matches_module_entrypoint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_file = root / "xray.env"
            env_file.write_text(ENV_CONTENT, encoding="utf-8")

            wrapper_dir = root / "wrapper"
            module_dir = root / "module"
            wrapper_dir.mkdir()
            module_dir.mkdir()

            wrapper_outputs = self.run_render_command(
                [sys.executable, str(WRAPPER)],
                env_file,
                wrapper_dir,
            )
            module_outputs = self.run_render_command(
                [sys.executable, "-m", "app.xray.render_config"],
                env_file,
                module_dir,
            )

            self.assertEqual(wrapper_outputs, module_outputs)
            client = json.loads(wrapper_outputs["client"])
            self.assertEqual(client["outbounds"][0]["settings"]["vnext"][0]["address"], "panel.example.com")
            self.assertEqual(client["outbounds"][0]["settings"]["vnext"][0]["port"], 31098)
            server = json.loads(wrapper_outputs["config"])
            self.assertEqual(server["api"]["listen"], "127.0.0.1:10085")
            self.assertTrue(server["policy"]["system"]["statsInboundUplink"])
            self.assertEqual(server["inbounds"][0]["tag"], "panel-443")

    def test_ai_domain_manager_wrapper_exposes_cli(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "ai_domain_manager.py"), "--help"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("Classify Xray destination domains", completed.stdout)

    def test_canonical_ai_domain_manager_module_exposes_cli(self):
        completed = subprocess.run(
            [sys.executable, "-m", "app.xray.ai_routing.runner", "--help"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("Classify Xray destination domains", completed.stdout)

    def test_legacy_ai_domain_manager_module_forwards_to_cli(self):
        completed = subprocess.run(
            [sys.executable, "-m", "app.xray.ai_domain_manager", "--help"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("Classify Xray destination domains", completed.stdout)

    def test_panel_ports_file_overrides_server_inbounds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_file = root / "xray.env"
            env_file.write_text(ENV_CONTENT, encoding="utf-8")

            outputs = self.run_render_command(
                [sys.executable, "-m", "app.xray.render_config"],
                env_file,
                root,
                panel_ports=[31098, 32001],
            )

            server = json.loads(outputs["config"])
            self.assertEqual([inbound["port"] for inbound in server["inbounds"]], [31098, 32001])
            self.assertEqual([inbound["tag"] for inbound in server["inbounds"]], ["panel-31098", "panel-32001"])

    def run_render_command(self, base_cmd, env_file, output_dir, panel_ports=None):
        config_out = output_dir / "config.json"
        client_out = output_dir / "client.json"
        share_out = output_dir / "share.txt"
        dynamic_out = output_dir / "dynamic.json"
        panel_ports_file = output_dir / "panel-ports.json"
        panel_ports_file.write_text(json.dumps({"ports": panel_ports or []}), encoding="utf-8")

        command = base_cmd + [
            "--env-file",
            str(env_file),
            "--config-out",
            str(config_out),
            "--client-out",
            str(client_out),
            "--share-out",
            str(share_out),
            "--dynamic-routing-file",
            str(dynamic_out),
            "--panel-ports-file",
            str(panel_ports_file),
        ]

        completed = subprocess.run(
            command,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        return {
            "config": config_out.read_text(encoding="utf-8"),
            "client": client_out.read_text(encoding="utf-8"),
            "share": share_out.read_text(encoding="utf-8"),
        }


class BackupRelayConfigTest(unittest.TestCase):
    def _values(self):
        return {
            "XRAY_LISTEN_HOST": "0.0.0.0",
            "XRAY_LISTEN_PORT": "443",
            "XRAY_PUBLIC_HOST": "panel.example.com",
            "XRAY_PUBLIC_PORT": "443",
            "XRAY_CLIENT_UUID": "11111111-1111-1111-1111-111111111111",
            "XRAY_FLOW": "xtls-rprx-vision",
            "XRAY_REALITY_PRIVATE_KEY": "private-key-example",
            "XRAY_REALITY_PUBLIC_KEY": "public-key-example",
            "XRAY_REALITY_SHORT_ID": "0123456789abcdef",
            "XRAY_SERVER_NAME": "www.microsoft.com",
            "XRAY_DEST": "www.microsoft.com:443",
            "XRAY_FINGERPRINT": "chrome",
            "XRAY_LOGLEVEL": "warning",
            "XRAY_NODE_TAG": "test-node",
        }

    def test_backup_config_relays_to_upstream_keeping_inbound(self):
        from app.xray.render_config import build_backup_relay_outbound, build_server_config

        url = (
            "vless://relay-uuid@nat.qq.pw:443?encryption=none&security=reality"
            "&sni=www.cloudflare.com&fp=chrome&pbk=PBKEY&sid=0123456789abcdef"
            "&type=tcp&flow=xtls-rprx-vision"
        )
        relay = build_backup_relay_outbound(url)
        values = self._values()
        primary = build_server_config(values)
        backup = build_server_config(values, relay_outbound=relay)

        # Inbound is identical so failover clients connect with the same profile.
        self.assertEqual(backup["inbounds"], primary["inbounds"])

        # The default route (first outbound) forwards every connection to the upstream.
        default_outbound = backup["outbounds"][0]
        self.assertEqual(default_outbound["tag"], "direct")
        self.assertEqual(default_outbound["protocol"], "vless")
        vnext = default_outbound["settings"]["vnext"][0]
        self.assertEqual(vnext["address"], "nat.qq.pw")
        self.assertEqual(vnext["port"], 443)
        self.assertEqual(vnext["users"][0]["id"], "relay-uuid")
        self.assertEqual(
            default_outbound["streamSettings"]["realitySettings"]["publicKey"], "PBKEY"
        )

        # The QUIC blackhole guard is preserved on the backup node too.
        self.assertIn("block", [o.get("tag") for o in backup["outbounds"]])
        self.assertEqual(backup["routing"]["rules"][0]["outboundTag"], "block")

    def test_backup_relay_rejects_non_vless_url(self):
        from app.xray.render_config import build_backup_relay_outbound

        with self.assertRaises(ValueError):
            build_backup_relay_outbound("https://nat.qq.pw:443")

    def test_backup_relay_requires_reality_params(self):
        from app.xray.render_config import build_backup_relay_outbound

        with self.assertRaises(ValueError):
            build_backup_relay_outbound("vless://u@nat.qq.pw:443?security=reality&sni=x")

    def test_backup_relay_accepts_plain_security_none(self):
        from app.xray.render_config import build_backup_relay_outbound

        outbound = build_backup_relay_outbound("vless://u@nat.qq.pw:443?type=tcp")
        self.assertEqual(outbound["streamSettings"]["security"], "none")
        self.assertNotIn("realitySettings", outbound["streamSettings"])


class AiNodeConfigTest(unittest.TestCase):
    def _values(self):
        return {
            "XRAY_LISTEN_HOST": "0.0.0.0",
            "XRAY_LISTEN_PORT": "443",
            "XRAY_PUBLIC_HOST": "panel.example.com",
            "XRAY_PUBLIC_PORT": "443",
            "XRAY_CLIENT_UUID": "11111111-1111-1111-1111-111111111111",
            "XRAY_FLOW": "xtls-rprx-vision",
            "XRAY_REALITY_PRIVATE_KEY": "private-key-example",
            "XRAY_REALITY_PUBLIC_KEY": "public-key-example",
            "XRAY_REALITY_SHORT_ID": "0123456789abcdef",
            "XRAY_SERVER_NAME": "www.microsoft.com",
            "XRAY_DEST": "www.microsoft.com:443",
            "XRAY_FINGERPRINT": "chrome",
            "XRAY_LOGLEVEL": "warning",
            "XRAY_NODE_TAG": "test-node",
            "XRAY_API_SERVER": "127.0.0.1:10085",
            "AI_NODE_CLIENT_UUID": "22222222-2222-2222-2222-222222222222",
            "AI_NODE_FLOW": "xtls-rprx-vision",
            "AI_NODE_REALITY_PRIVATE_KEY": "ai-private-key-example",
            "AI_NODE_REALITY_PUBLIC_KEY": "ai-public-key-example",
            "AI_NODE_REALITY_SHORT_ID": "fedcba9876543210",
            "AI_NODE_SERVER_NAME": "www.amazon.com",
            "AI_NODE_DEST": "www.amazon.com:443",
            "AI_NODE_FINGERPRINT": "chrome",
        }

    def test_ai_node_config_has_freedom_direct_only(self):
        from app.xray.render_config import build_ai_node_config

        values = self._values()
        values["AI_UPSTREAM_PORT"] = "27166"
        config = build_ai_node_config(values)

        self.assertEqual(config["outbounds"], [{"protocol": "freedom", "tag": "direct"}])
        self.assertNotIn("routing", config)
        self.assertEqual(
            config["log"],
            {
                "loglevel": "warning",
                "access": "/var/log/xray/ai-access.log",
                "error": "/var/log/xray/ai-error.log",
            },
        )
        self.assertEqual(
            config["metrics"],
            {"tag": "ai-metrics", "listen": "127.0.0.1:31097"},
        )
        self.assertEqual(config["stats"], {})
        self.assertTrue(config["policy"]["system"]["statsInboundUplink"])
        self.assertTrue(config["policy"]["system"]["statsInboundDownlink"])
        self.assertTrue(config["policy"]["system"]["statsOutboundUplink"])
        self.assertTrue(config["policy"]["system"]["statsOutboundDownlink"])
        self.assertEqual(len(config["inbounds"]), 1)
        self.assertEqual(config["inbounds"][0]["protocol"], "vless")
        self.assertEqual(config["inbounds"][0]["port"], 27166)

    def test_ai_node_reuses_reality_params_from_data_plane(self):
        from app.xray.render_config import build_ai_node_config, build_reality_inbound

        values = self._values()
        values["AI_UPSTREAM_PORT"] = "27166"
        values.update(
            {
                "AI_NODE_CLIENT_UUID": "22222222-2222-2222-2222-222222222222",
                "AI_NODE_FLOW": "xtls-rprx-vision",
                "AI_NODE_REALITY_PRIVATE_KEY": "ai-private-key-example",
                "AI_NODE_REALITY_PUBLIC_KEY": "ai-public-key-example",
                "AI_NODE_REALITY_SHORT_ID": "fedcba9876543210",
                "AI_NODE_SERVER_NAME": "www.amazon.com",
                "AI_NODE_DEST": "www.amazon.com:443",
                "AI_NODE_FINGERPRINT": "chrome",
            }
        )
        config = build_ai_node_config(values)
        ai_values = dict(values)
        ai_values.update(
            {
                "XRAY_CLIENT_UUID": values["AI_NODE_CLIENT_UUID"],
                "XRAY_FLOW": values["AI_NODE_FLOW"],
                "XRAY_REALITY_PRIVATE_KEY": values["AI_NODE_REALITY_PRIVATE_KEY"],
                "XRAY_REALITY_PUBLIC_KEY": values["AI_NODE_REALITY_PUBLIC_KEY"],
                "XRAY_REALITY_SHORT_ID": values["AI_NODE_REALITY_SHORT_ID"],
                "XRAY_SERVER_NAME": values["AI_NODE_SERVER_NAME"],
                "XRAY_DEST": values["AI_NODE_DEST"],
                "XRAY_FINGERPRINT": values["AI_NODE_FINGERPRINT"],
            }
        )
        expected_inbound = build_reality_inbound(ai_values, 27166)

        self.assertEqual(config["inbounds"][0], expected_inbound)
        self.assertNotEqual(
            config["inbounds"][0]["settings"]["clients"][0]["id"],
            values["XRAY_CLIENT_UUID"],
        )

    def test_ai_node_requires_independent_credentials(self):
        from app.xray.render_config import build_ai_node_config

        values = self._values()
        for key in tuple(values):
            if key.startswith("AI_NODE_"):
                values.pop(key)
        with self.assertRaisesRegex(ValueError, "missing required AI node values"):
            build_ai_node_config(values)

    def test_backup_config_without_upstream_url_uses_freedom_direct(self):
        from app.xray.render_config import build_server_config

        values = self._values()
        backup = build_server_config(values, None, None, relay_outbound=None)

        self.assertEqual(backup["outbounds"][0], {"protocol": "freedom", "tag": "direct"})

    def test_cli_renders_ai_node_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_file = root / "xray.env"
            env_file.write_text(ENV_CONTENT, encoding="utf-8")
            config_out = root / "config.json"
            client_out = root / "client.json"
            share_out = root / "share.txt"
            ai_node_out = root / "config-ai-node.json"
            panel_ports_file = root / "panel-ports.json"
            panel_ports_file.write_text(json.dumps({"ports": []}), encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable, "-m", "app.xray.render_config",
                    "--env-file", str(env_file),
                    "--config-out", str(config_out),
                    "--client-out", str(client_out),
                    "--share-out", str(share_out),
                    "--panel-ports-file", str(panel_ports_file),
                    "--ai-node-config-out", str(ai_node_out),
                ],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            self.assertTrue(ai_node_out.is_file())

            ai_config = json.loads(ai_node_out.read_text(encoding="utf-8"))
            self.assertEqual(ai_config["outbounds"], [{"protocol": "freedom", "tag": "direct"}])
            self.assertNotIn("routing", ai_config)
            self.assertEqual(ai_config["metrics"]["listen"], "127.0.0.1:31097")

    def test_cli_renders_backup_without_upstream_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_file = root / "xray.env"
            env_file.write_text(ENV_CONTENT, encoding="utf-8")
            config_out = root / "config.json"
            client_out = root / "client.json"
            share_out = root / "share.txt"
            backup_out = root / "config-backup.json"
            panel_ports_file = root / "panel-ports.json"
            panel_ports_file.write_text(json.dumps({"ports": []}), encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable, "-m", "app.xray.render_config",
                    "--env-file", str(env_file),
                    "--config-out", str(config_out),
                    "--client-out", str(client_out),
                    "--share-out", str(share_out),
                    "--panel-ports-file", str(panel_ports_file),
                    "--backup-config-out", str(backup_out),
                ],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            self.assertTrue(backup_out.is_file())

            backup_config = json.loads(backup_out.read_text(encoding="utf-8"))
            self.assertEqual(backup_config["outbounds"][0], {"protocol": "freedom", "tag": "direct"})


if __name__ == "__main__":
    unittest.main()
