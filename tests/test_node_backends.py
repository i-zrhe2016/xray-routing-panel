import sys
import unittest

from app.xray.node import (
    DataPlaneConfig,
    DockerBackend,
    LocalBackend,
    NodeController,
    SSHBackend,
    UnmanagedBackend,
)
from app.xray.node_control import DataPlaneController


class NodeBackendSelectionTest(unittest.TestCase):
    @staticmethod
    def config(**overrides):
        values = {
            "role": "data_plane",
            "label": "数据面",
        }
        values.update(overrides)
        return DataPlaneConfig(**values)

    def test_ssh_backend_has_priority_over_local_and_docker_settings(self):
        controller = NodeController(
            self.config(
                ssh_target="root@example.com",
                local_bin=sys.executable,
                container_name="xray",
            )
        )

        self.assertIsInstance(controller.backend, SSHBackend)
        self.assertEqual(controller.mode, "ssh")
        self.assertTrue(controller.is_remote)

    def test_local_backend_is_selected_when_a_local_binary_is_available(self):
        controller = NodeController(self.config(local_bin=sys.executable))

        self.assertIsInstance(controller.backend, LocalBackend)
        self.assertEqual(controller.mode, "local")
        self.assertFalse(controller.is_remote)

    def test_docker_backend_is_selected_when_no_local_binary_is_available(self):
        controller = NodeController(self.config(local_bin="/does/not/exist", container_name="xray"))

        self.assertIsInstance(controller.backend, DockerBackend)
        self.assertEqual(controller.mode, "docker")

    def test_unmanaged_backend_preserves_external_upstream_mode(self):
        controller = NodeController(
            self.config(
                local_bin="/does/not/exist",
                upstream_host="upstream.example.com",
                upstream_port=443,
            )
        )

        self.assertIsInstance(controller.backend, UnmanagedBackend)
        self.assertEqual(controller.mode, "unmanaged")
        self.assertTrue(controller.is_configured())
        self.assertEqual(controller.display_target(), "upstream.example.com:443")

    def test_legacy_controller_name_is_the_new_controller(self):
        self.assertIs(DataPlaneController, NodeController)


if __name__ == "__main__":
    unittest.main()
