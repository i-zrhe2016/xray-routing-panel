"""Backends for managed Xray nodes.

``NodeController`` is the stable orchestration facade.  Concrete transport
and process details live in the backend modules; callers should import all
node types from this canonical package.
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
