"""Upstream selection helpers exposed as a stable app.xray submodule."""

from app.xray.ai_routing.candidates import build_ai_upstream_candidates
from app.xray.ai_routing.candidates import build_template_upstream_candidate
from app.xray.ai_routing.candidates import dedupe_upstream_candidates
from app.xray.ai_routing.candidates import join_host_port
from app.xray.ai_routing.candidates import parse_upstream_endpoint
from app.xray.ai_routing.candidates import parse_upstream_list
from app.xray.ai_routing.candidates import parse_vless_fallback_url
from app.xray.ai_routing.candidates import probe_ai_upstream_candidate
from app.xray.ai_routing.selector import select_ai_target
from app.xray.ai_routing.candidates import summarize_ai_target_candidate
from app.xray.ai_routing.selector import summarize_ai_target_for_report

__all__ = [
    "build_ai_upstream_candidates",
    "build_template_upstream_candidate",
    "dedupe_upstream_candidates",
    "join_host_port",
    "parse_upstream_endpoint",
    "parse_upstream_list",
    "parse_vless_fallback_url",
    "probe_ai_upstream_candidate",
    "select_ai_target",
    "summarize_ai_target_candidate",
    "summarize_ai_target_for_report",
]
