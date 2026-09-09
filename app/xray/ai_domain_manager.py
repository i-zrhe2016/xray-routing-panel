"""Legacy CLI entry point for the split AI routing package.

The implementation and canonical CLI live under
:mod:`app.xray.ai_routing`.  This module intentionally forwards only the
entry point so existing ``python -m`` invocations keep working without
re-exporting implementation helpers or constructing application state.
"""

from app.xray.ai_routing.runner import main

__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
