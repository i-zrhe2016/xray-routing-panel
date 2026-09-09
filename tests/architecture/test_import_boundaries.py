import ast
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = REPOSITORY_ROOT / "app"


def _module_info(path, source_root, package_name):
    relative = path.relative_to(source_root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    module_name = ".".join((package_name, *parts)) if parts else package_name
    package = module_name if path.name == "__init__.py" else module_name.rpartition(".")[0]
    return module_name, package


def _resolve_import(package_name, level, imported_name):
    if level == 0:
        return imported_name or ""
    package_parts = package_name.split(".")
    if level > len(package_parts):
        return imported_name or ""
    base = package_parts[: len(package_parts) - level + 1]
    if imported_name:
        base.append(imported_name)
    return ".".join(base)


def _iter_imports(path, source_root, package_name):
    module_name, current_package = _module_info(path, source_root, package_name)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node, module_name, alias.name
        elif isinstance(node, ast.ImportFrom):
            target = _resolve_import(
                current_package,
                node.level,
                node.module,
            )
            if target:
                yield node, module_name, target
                for alias in node.names:
                    if alias.name != "*":
                        yield node, module_name, f"{target}.{alias.name}"


def _find_forbidden_imports(source_root, package_name, forbidden_prefixes):
    violations = []
    reported = set()
    for path in sorted(source_root.rglob("*.py")):
        for node, module_name, imported_name in _iter_imports(path, source_root, package_name):
            matching_prefixes = tuple(
                prefix
                for prefix in forbidden_prefixes
                if imported_name == prefix or imported_name.startswith(f"{prefix}.")
            )
            for prefix in matching_prefixes:
                report_key = (path, node.lineno, module_name, prefix)
                if report_key in reported:
                    continue
                reported.add(report_key)
                relative = path.relative_to(source_root)
                violations.append(f"{relative}:{node.lineno}: {module_name} imports {imported_name}")
    return violations


def _find_constructor_calls(source_root, package_name, constructor_names):
    violations = []
    for path in sorted(source_root.rglob("*.py")):
        module_name, _ = _module_info(path, source_root, package_name)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            constructor_name = None
            if isinstance(node.func, ast.Name):
                constructor_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                constructor_name = node.func.attr
            if constructor_name in constructor_names:
                relative = path.relative_to(source_root)
                violations.append(f"{relative}:{node.lineno}: {module_name} constructs {constructor_name}")
    return violations


@pytest.mark.parametrize(
    ("relative_root", "package_name", "forbidden_prefixes"),
    [
        ("xray", "app.xray", ("app.web",)),
        ("storage", "app.storage", ("app.state", "app.web")),
        ("runtime", "app.runtime", ("app.web",)),
    ],
)
def test_lower_layers_do_not_import_forbidden_higher_layers(
    relative_root, package_name, forbidden_prefixes
):
    source_root = APP_ROOT / relative_root
    violations = _find_forbidden_imports(source_root, package_name, forbidden_prefixes)

    assert not violations, "\n".join(violations)


def test_web_does_not_construct_core_business_dependencies():
    violations = _find_constructor_calls(
        APP_ROOT / "web",
        "app.web",
        {
            "AiRoutingService",
            "ApplicationLifecycle",
            "NodeController",
            "PanelState",
            "SQLiteDatabase",
            "XrayApplyService",
        },
    )

    assert not violations, "\n".join(violations)


def test_composition_entrypoint_is_allowed_to_wire_bootstrap_and_web():
    panel_path = APP_ROOT / "panel.py"
    imports = {imported_name for _, _, imported_name in _iter_imports(panel_path, APP_ROOT, "app")}

    assert "app.bootstrap" in imports
    assert "app.web" in imports


@pytest.mark.parametrize("source", ["from app.web import state\n", "from app import web\n"])
def test_boundary_scanner_reports_a_synthetic_forbidden_import(tmp_path, source):
    source_root = tmp_path / "runtime"
    source_root.mkdir()
    bad_module = source_root / "bad.py"
    bad_module.write_text(source, encoding="utf-8")

    violations = _find_forbidden_imports(source_root, "app.runtime", ("app.web",))

    assert violations == ["bad.py:1: app.runtime.bad imports app.web"]
