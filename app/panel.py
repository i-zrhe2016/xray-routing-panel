#!/usr/bin/env python3
import importlib
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for module_name in [
    "app.config",
    "app.helpers",
    "app.auth",
    "app.subscriptions",
    "app.state",
    "app.bootstrap",
    "app.web",
]:
    if module_name in sys.modules:
        importlib.reload(sys.modules[module_name])
    else:
        importlib.import_module(module_name)

from app.bootstrap import build_application
from app.errors import ValidationError
from app.state import PanelState
from app.web import create_app
from app.web import main as _serve

application = build_application()
state = application
app = create_app(application)


def main():
    """Run the Web server with the Application assembled by this module."""

    return _serve(application)


__all__ = ["PanelState", "ValidationError", "app", "application", "main", "state"]


if __name__ == "__main__":
    main()
