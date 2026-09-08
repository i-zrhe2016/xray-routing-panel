"""Node controller facade and backend selection."""

from __future__ import annotations

from .backend import DataPlaneConfig, NodeBackend, UnmanagedBackend
from .docker import DockerBackend
from .local import LocalBackend, resolve_local_bin
from .ssh import SSHBackend


class NodeController:
    """Select one node backend and expose its stable management interface."""

    def __init__(self, config: DataPlaneConfig):
        self.config = config
        self.backend = self._build_backend(config)
        # The hooks preserve the legacy controller test seam while the
        # implementation of command construction remains in each backend.
        self.backend.bind_runners(
            run_subprocess=lambda *args, **kwargs: self._run_subprocess(*args, **kwargs),
            run_remote=lambda *args, **kwargs: self._run_remote(*args, **kwargs),
            test_config=lambda *args, **kwargs: self.test_config(*args, **kwargs),
        )

    @staticmethod
    def _build_backend(config: DataPlaneConfig) -> NodeBackend:
        if config.ssh_target:
            return SSHBackend(config)
        if resolve_local_bin(config.local_bin):
            return LocalBackend(config)
        if config.container_name:
            return DockerBackend(config)
        return UnmanagedBackend(config)

    @property
    def mode(self):
        return self.backend.mode

    @property
    def is_remote(self):
        return self.backend.is_remote

    def _run_subprocess(self, command, error_prefix, timeout=None, input_text=None):
        return self.backend.execute_subprocess(
            command,
            error_prefix,
            timeout=timeout,
            input_text=input_text,
        )

    def _run_remote(self, args, error_prefix, timeout=None, input_text=None):
        return self.backend.execute_remote(
            args,
            error_prefix,
            timeout=timeout,
            input_text=input_text,
        )

    def status_summary(self):
        """Return status through the facade so legacy monkeypatches still work."""

        xray_running = None
        error = ""
        configured = self.is_configured()
        if configured:
            try:
                xray_running = self.is_running()
            except Exception as exc:  # noqa: BLE001 - facade preserves status error reporting
                error = str(exc)
        return {
            "role": self.config.role,
            "label": self.config.label,
            "configured": configured,
            "reachable": bool(xray_running) if xray_running is not None else False,
            "xray_running": xray_running,
            "management_target": self.display_target(),
            "api_server": self.config.api_server,
            "config_path": self.config.config_path,
            "access_log_path": self.config.access_log_path,
            "supports_sync": self.supports_sync(),
            "supports_restart": self.supports_restart(),
            "last_error": error,
        }

    def __getattr__(self, name):
        """Delegate node operations without duplicating backend behavior."""

        backend = object.__getattribute__(self, "backend")
        return getattr(backend, name)


DataPlaneController = NodeController
ManagedNodeConfig = DataPlaneConfig
ManagedNodeController = NodeController

__all__ = [
    "DataPlaneConfig",
    "DataPlaneController",
    "ManagedNodeConfig",
    "ManagedNodeController",
    "NodeController",
]
