"""Select the effective AI upstream after candidates have been probed."""

from .candidates import probe_ai_upstream_candidate, summarize_ai_target_candidate


def summarize_ai_target_for_report(ai_target):
    if ai_target.get("probe_status") == "manual_fallback":
        candidates = ai_target.get("candidates", [])
        if not isinstance(candidates, list):
            candidates = []
        return {
            "probe_status": "manual_fallback",
            "failure_reason": str(ai_target.get("failure_reason", "")).strip(),
            "is_reachable": False,
            "candidates": list(candidates),
        }
    summary = summarize_ai_target_candidate(ai_target)
    for key in (
        "selected_index",
        "selected_number",
        "candidate_count",
        "failover_active",
        "probe_status",
        "selection_mode",
        "probe_timeout_seconds",
    ):
        if key in ai_target:
            summary[key] = ai_target[key]
    failure_reason = str(ai_target.get("failure_reason", "")).strip()
    if failure_reason:
        summary["failure_reason"] = failure_reason
    candidates = ai_target.get("candidates", [])
    summary["candidates"] = list(candidates) if isinstance(candidates, list) else []
    return summary


def should_fallback_to_primary_route(ai_target):
    if not isinstance(ai_target, dict):
        return False
    return str(ai_target.get("probe_status", "")).strip().lower() == "all_unreachable"


def select_ai_target(candidates, timeout_seconds, probe_controller=None, preferred_index=None):
    probes = [
        probe_ai_upstream_candidate(candidate, timeout_seconds, probe_controller=probe_controller)
        for candidate in candidates
    ]
    if preferred_index is not None:
        try:
            selected_index = int(preferred_index)
        except (TypeError, ValueError):
            selected_index = 0
        if selected_index < 0 or selected_index >= len(probes):
            selected_index = 0
    else:
        selected_index = 0
        for index, candidate in enumerate(probes):
            if candidate["is_reachable"]:
                selected_index = index
                break

    selected = probes[selected_index]
    all_unreachable = not any(item["is_reachable"] for item in probes)
    selected_target = dict(selected)
    management_error = any(item.get("probe_management_error") for item in probes)
    selected_target.update(
        {
            "selected_index": selected_index,
            "selected_number": selected_index + 1,
            "candidate_count": len(probes),
            "failover_active": selected_index > 0,
            "probe_status": (
                "probe_error"
                if management_error
                else (
                    "manual_selected"
                    if preferred_index is not None and selected["is_reachable"]
                    else (
                        "manual_unreachable"
                        if preferred_index is not None
                        else ("all_unreachable" if all_unreachable else "reachable")
                    )
                )
            ),
            "selection_mode": "manual" if preferred_index is not None else "auto",
            "probe_timeout_seconds": timeout_seconds,
            "checked_at": selected["checked_at"],
            "failure_reason": (
                selected["failure_reason"]
                if (
                    all_unreachable
                    or management_error
                    or (preferred_index is not None and not selected["is_reachable"])
                )
                else ""
            ),
            "candidates": [summarize_ai_target_candidate(item) for item in probes],
        }
    )
    return selected_target


__all__ = [
    "select_ai_target",
    "should_fallback_to_primary_route",
    "summarize_ai_target_for_report",
]
