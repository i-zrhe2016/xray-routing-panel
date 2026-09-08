"""Backends for managed Xray nodes.

``NodeController`` is the stable orchestration facade.  Concrete transport
and process details live in the backend modules and remain available through
the legacy :mod:`app.xray.node_control` compatibility module.
"""

from .backend import DataPlaneConfig, NodeBackend, UnmanagedBackend
from .controller import NodeController
from .docker import DockerBackend
from .local import LocalBackend
from .ssh import SSHBackend

DataPlaneController = NodeController
ManagedNodeConfig = DataPlaneConfig
ManagedNodeController = NodeController

__all__ = [
    "DataPlaneConfig",
    "DataPlaneController",
    "DockerBackend",
    "LocalBackend",
    "ManagedNodeConfig",
    "ManagedNodeController",
    "NodeBackend",
    "NodeController",
    "SSHBackend",
    "UnmanagedBackend",
]
