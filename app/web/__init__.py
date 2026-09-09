"""Web package and explicit Flask application factory.

The package exposes the route collector and factory without constructing an
application at import time. Callers provide the application facade to
``create_app(application)``; the factory then loads the view modules, registers
their routes and stores the same object in ``flask_app.extensions``.
"""

from .core import (
    before_request,
    create_app,
    handle_shutdown,
    main,
    route,
    template_filter,
)

# Filled by create_app(application). Keeping these names available preserves
# the existing public import surface after the composition root has run.
app = None
state = None

__all__ = [
    "app",
    "before_request",
    "create_app",
    "handle_shutdown",
    "main",
    "route",
    "state",
    "template_filter",
]
