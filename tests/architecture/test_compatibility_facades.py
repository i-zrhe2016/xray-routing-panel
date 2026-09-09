from pathlib import Path

from app.xray import ai_domain_manager, node_control
from app.xray.ai_routing import artifact, manager, runner
from app.xray.node import DockerBackend, LocalBackend, NodeController, SSHBackend, UnmanagedBackend


def test_node_control_facade_reexports_canonical_node_symbols():
    assert node_control.NodeController is NodeController
    assert node_control.DockerBackend is DockerBackend
    assert node_control.LocalBackend is LocalBackend
    assert node_control.SSHBackend is SSHBackend
    assert node_control.UnmanagedBackend is UnmanagedBackend


def test_ai_domain_manager_facade_keeps_canonical_cli_and_helper_exports():
    assert ai_domain_manager.main is runner.main
    assert ai_domain_manager.LOCK_BUSY_EXIT_CODE == manager.LOCK_BUSY_EXIT_CODE
    assert ai_domain_manager.build_domain_report is artifact.build_domain_report
    assert ai_domain_manager.write_routing_fragment is artifact.write_routing_fragment


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
