import unittest
from types import SimpleNamespace

from app.xray.node import DataPlaneConfig, DataPlaneController


class PublicIpResolutionTest(unittest.TestCase):
    def test_local_public_ip_resolution_uses_local_runner(self):
        controller = DataPlaneController(DataPlaneConfig(role="data_plane", label="数据面"))
        calls = []

        def fake_run(command, error_prefix, timeout=None, input_text=None):
            calls.append((command, error_prefix, timeout, input_text))
            return SimpleNamespace(stdout="1.1.1.1\n")

        controller._run_subprocess = fake_run

        result = controller.resolve_public_ip(timeout_seconds=4)

        self.assertEqual(result, "1.1.1.1")
        self.assertEqual(calls[0][2], 6)

    def test_remote_public_ip_resolution_uses_remote_runner(self):
        controller = DataPlaneController(
            DataPlaneConfig(role="data_plane", label="数据面", ssh_target="root@example.com")
        )
        calls = []

        def fake_run(command, error_prefix, timeout=None, input_text=None):
            calls.append((command, error_prefix, timeout, input_text))
            return SimpleNamespace(stdout="2.2.2.2\n")

        controller._run_remote = fake_run

        result = controller.resolve_public_ip(timeout_seconds=3)

        self.assertEqual(result, "2.2.2.2")
        self.assertEqual(calls[0][2], 5)


if __name__ == "__main__":
    unittest.main()
