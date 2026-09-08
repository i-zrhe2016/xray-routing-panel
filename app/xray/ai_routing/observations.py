"""Read Xray access logs and maintain the current observation window."""

from datetime import datetime, timedelta, timezone

from .common import DOMAIN_RE, TARGET_RE, TIMESTAMP_RE, format_timestamp, load_json, save_json, utc_now


def split_target_host(target):
    if target.startswith("[") and "]:" in target:
        host, _, _ = target[1:].partition("]:")
        return host.strip().lower()
    if ":" not in target:
        return target.strip().lower()
    host, _, _ = target.rpartition(":")
    return host.strip().lower()


def parse_log_line(line):
    ts_match = TIMESTAMP_RE.match(line)
    target_match = TARGET_RE.search(line)
    if ts_match is None or target_match is None:
        return None
    try:
        seen_at = datetime.strptime(
            f"{ts_match.group('date')} {ts_match.group('time')}",
            "%Y/%m/%d %H:%M:%S.%f" if "." in ts_match.group("time") else "%Y/%m/%d %H:%M:%S",
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None

    host = split_target_host(target_match.group("target"))
    if not DOMAIN_RE.fullmatch(host):
        return None

    return {
        "seen_at": seen_at,
        "protocol": target_match.group("proto"),
        "domain": host,
    }


def normalize_log_state(state):
    events = []
    for item in state.get("events", []):
        try:
            events.append(
                {
                    "seen_at": datetime.fromisoformat(item["seen_at"]).astimezone(timezone.utc),
                    "protocol": str(item["protocol"]).strip().lower(),
                    "domain": str(item["domain"]).strip().lower(),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    state["events"] = events
    state["log_inode"] = str(state.get("log_inode", ""))
    try:
        state["log_offset"] = int(state.get("log_offset", 0))
    except (TypeError, ValueError):
        state["log_offset"] = 0
    return state


def load_log_state(path):
    return normalize_log_state(load_json(path, {"log_inode": "", "log_offset": 0, "events": []}))


def save_log_state(path, state):
    serializable = {
        "log_inode": state["log_inode"],
        "log_offset": state["log_offset"],
        "events": [
            {
                "seen_at": format_timestamp(item["seen_at"]),
                "protocol": item["protocol"],
                "domain": item["domain"],
            }
            for item in state["events"]
        ],
    }
    save_json(path, serializable)


def sync_log(log_path, state, data_plane_controller=None, lookback_seconds=3600, now=None):
    if data_plane_controller is not None and data_plane_controller.supports_logs():
        current_time = now or utc_now()
        cutoff = current_time - timedelta(seconds=lookback_seconds)
        payload = data_plane_controller.read_access_log_delta(
            state["log_inode"],
            state["log_offset"],
            since_epoch=cutoff.timestamp(),
        )
        if not payload["exists"]:
            return
        for line in str(payload["data"]).splitlines():
            parsed = parse_log_line(line)
            if parsed is not None:
                state["events"].append(parsed)
        state["log_inode"] = payload["inode"]
        state["log_offset"] = int(payload["offset"])
        return

    if not log_path.exists():
        return

    stat = log_path.stat()
    current_inode = str(stat.st_ino)
    current_offset = int(state["log_offset"])
    if state["log_inode"] != current_inode or stat.st_size < current_offset:
        current_offset = 0

    with log_path.open("r", encoding="utf-8", errors="ignore") as handle:
        handle.seek(current_offset)
        for line in handle:
            parsed = parse_log_line(line)
            if parsed is not None:
                state["events"].append(parsed)
        state["log_offset"] = handle.tell()
    state["log_inode"] = current_inode


def purge_old_events(state, lookback_seconds, now):
    cutoff = now - timedelta(seconds=lookback_seconds)
    state["events"] = [item for item in state["events"] if item["seen_at"] >= cutoff]
    return cutoff


__all__ = [
    "load_json",
    "load_log_state",
    "normalize_log_state",
    "parse_log_line",
    "purge_old_events",
    "save_json",
    "save_log_state",
    "split_target_host",
    "sync_log",
    "utc_now",
]
