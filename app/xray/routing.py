"""Dynamic routing helpers exposed as a stable app.xray submodule."""

from app.xray.ai_routing.artifact import apply_default_proxy_sockopt
from app.xray.ai_routing.artifact import build_default_proxy_payload
from app.xray.ai_routing.artifact import build_domain_report
from app.xray.ai_routing.artifact import build_proxy_sockopt_payload
from app.xray.ai_routing.artifact import render_proxy_template
from app.xray.ai_routing.artifact import rerender_config
from app.xray.ai_routing.artifact import restart_xray_command
from app.xray.ai_routing.artifact import restart_xray_container
from app.xray.ai_routing.artifact import write_domain_report
from app.xray.ai_routing.artifact import write_routing_fragment

__all__ = [
    "apply_default_proxy_sockopt",
    "build_default_proxy_payload",
    "build_domain_report",
    "build_proxy_sockopt_payload",
    "render_proxy_template",
    "rerender_config",
    "restart_xray_command",
    "restart_xray_container",
    "write_domain_report",
    "write_routing_fragment",
]
