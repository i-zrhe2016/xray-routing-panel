"""Domain classification helpers exposed as a stable app.xray submodule."""

from app.xray.ai_routing.classifier import FORCED_AI_ROUTE_DOMAIN_SUFFIXES
from app.xray.ai_routing.classifier import KNOWN_AI_DOMAIN_SUFFIXES
from app.xray.ai_routing.classifier import classify_domains_via_codex
from app.xray.ai_routing.classifier import classify_domains_via_openai
from app.xray.ai_routing.classifier import classify_pending_domains
from app.xray.ai_routing.classifier import load_decisions
from app.xray.ai_routing.classifier import matches_forced_ai_route_domain
from app.xray.ai_routing.classifier import matches_known_ai_domain
from app.xray.ai_routing.classifier import normalize_classification
from app.xray.ai_routing.classifier import sync_builtin_domain_decisions

__all__ = [
    "FORCED_AI_ROUTE_DOMAIN_SUFFIXES",
    "KNOWN_AI_DOMAIN_SUFFIXES",
    "classify_domains_via_codex",
    "classify_domains_via_openai",
    "classify_pending_domains",
    "load_decisions",
    "matches_forced_ai_route_domain",
    "matches_known_ai_domain",
    "normalize_classification",
    "sync_builtin_domain_decisions",
]
