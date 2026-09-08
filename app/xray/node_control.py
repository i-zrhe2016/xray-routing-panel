"""Backward-compatible facade for the managed-node package.

New code should import from :mod:`app.xray.node`.  This module keeps the
historical import path stable for extensions and older deployments.
"""

import subprocess  # noqa: F401 - retained for legacy monkeypatch paths

from .node.backend import DataPlaneConfig, NodeBackend, UnmanagedBackend
from .node.controller import (
    DataPlaneController,
    ManagedNodeConfig,
    ManagedNodeController,
    NodeController,
)
from .node.docker import DockerBackend
from .node.files import (
    REMOTE_AI_DOMAINS_SNAPSHOT_SCRIPT,
    REMOTE_DELETE_FILE_SCRIPT,
    REMOTE_FILE_DELTA_SCRIPT,
    REMOTE_READ_FILE_SCRIPT,
    REMOTE_REPLACE_FILE_SCRIPT,
    REMOTE_WRITE_FILE_SCRIPT,
    build_temp_target_path,
    is_after_cutoff,
    join_shell_args,
)
from .node.local import LocalBackend, resolve_local_bin
from .node.probes import (
    PUBLIC_IP_DISCOVERY_SCRIPT,
    REMOTE_REALITY_PROBE_SCRIPT,
    REMOTE_SOCKET_CHECK_SCRIPT,
    REMOTE_SOCKET_PROBE_SCRIPT,
    _apply_cert_fields,
    _cert_common_name,
    parse_api_endpoint,
    reality_handshake_probe,
)
from .node.ssh import SSHBackend

__all__ = [
    "PUBLIC_IP_DISCOVERY_SCRIPT",
    "REMOTE_AI_DOMAINS_SNAPSHOT_SCRIPT",
    "REMOTE_DELETE_FILE_SCRIPT",
    "REMOTE_FILE_DELTA_SCRIPT",
    "REMOTE_READ_FILE_SCRIPT",
    "REMOTE_REALITY_PROBE_SCRIPT",
    "REMOTE_REPLACE_FILE_SCRIPT",
    "REMOTE_SOCKET_CHECK_SCRIPT",
    "REMOTE_SOCKET_PROBE_SCRIPT",
    "REMOTE_WRITE_FILE_SCRIPT",
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
    "_apply_cert_fields",
    "_cert_common_name",
    "build_temp_target_path",
    "is_after_cutoff",
    "join_shell_args",
    "parse_api_endpoint",
    "reality_handshake_probe",
    "resolve_local_bin",
]
