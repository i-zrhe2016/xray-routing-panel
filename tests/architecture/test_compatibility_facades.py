from pathlib import Path

import pytest

from app.xray import ai_domain_manager
from app.xray.ai_routing import runner
from app.xray.node import DockerBackend, LocalBackend, NodeController, SSHBackend, UnmanagedBackend


def test_application_declares_explicit_domain_components():
    from app.bootstrap import build_application

    expected_names = (
        "ports",
        "traffic",
        "probes",
        "nodes",
        "ai_routing",
        "dns_failover",
        "commerce",
        "diagnostics",
        "lifecycle",
    )

    application = build_application()

    assert application.PUBLIC_COMPONENT_NAMES == expected_names
    for name in expected_names:
        assert getattr(application, name) is not None
    assert application.nodes.data_plane is application.data_plane
    assert application.nodes.ai_nodes is application.ai_nodes
    assert application.commerce.ports is application.ports
    assert application.commerce.traffic is application.traffic
    assert application.diagnostics.ports is application.ports


def test_panel_state_keeps_concrete_legacy_delegates_without_catch_all():
    from app.bootstrap import build_application

    application = build_application()

    assert callable(application.query_ports)
    assert callable(application.ai_node_status)
    assert not hasattr(type(application), "__getattr__")
    with pytest.raises(AttributeError):
        application.__getattribute__("unsupported_legacy_attribute")


def test_node_package_exposes_canonical_node_symbols():
    from app.xray import node

    assert node.NodeController is NodeController
    assert node.DockerBackend is DockerBackend
    assert node.LocalBackend is LocalBackend
    assert node.SSHBackend is SSHBackend
    assert node.UnmanagedBackend is UnmanagedBackend


def test_node_control_legacy_module_is_removed():
    repository_root = Path(__file__).resolve().parents[2]

    assert not (repository_root / "app" / "xray" / "node_control.py").exists()


def test_internal_code_does_not_import_removed_node_control_module():
    from .test_import_boundaries import _find_forbidden_imports

    repository_root = Path(__file__).resolve().parents[2]
    violations = []
    for source_root, package_name in (
        (repository_root / "app", "app"),
        (repository_root / "scripts", "scripts"),
        (repository_root / "tests", "tests"),
    ):
        violations.extend(
            _find_forbidden_imports(
                source_root,
                package_name,
                ("app.xray.node_control",),
            )
        )

    assert not violations, "\n".join(violations)


def test_ai_domain_manager_facade_is_cli_only():
    assert ai_domain_manager.main is runner.main
    assert ai_domain_manager.__all__ == ["main"]
    for helper_name in (
        "LOCK_BUSY_EXIT_CODE",
        "build_domain_report",
        "write_routing_fragment",
        "run_once",
    ):
        assert not hasattr(ai_domain_manager, helper_name)


def test_internal_code_does_not_import_ai_domain_manager_facade():
    from .test_import_boundaries import _find_forbidden_imports

    repository_root = Path(__file__).resolve().parents[2]
    violations = []
    for source_root, package_name in (
        (repository_root / "app", "app"),
        (repository_root / "scripts", "scripts"),
    ):
        violations.extend(
            _find_forbidden_imports(
                source_root,
                package_name,
                ("app.xray.ai_domain_manager",),
            )
        )

    assert not violations, "\n".join(violations)


def test_canonical_packages_do_not_depend_on_compatibility_facades():
    from .test_import_boundaries import _find_forbidden_imports

    repository_root = Path(__file__).resolve().parents[2]
    xray_root = repository_root / "app" / "xray"
    violations = []
    for source_root, package_name, forbidden in (
        (xray_root / "node", "app.xray.node", ("app.xray.node_control",)),
        (xray_root / "ai_routing", "app.xray.ai_routing", ("app.xray.ai_domain_manager",)),
    ):
        violations.extend(_find_forbidden_imports(source_root, package_name, forbidden))

    assert not violations, "\n".join(violations)
