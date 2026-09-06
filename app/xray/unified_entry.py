"""Stable, private per-account credentials for the shared proxy entry."""

import hashlib
import hmac
from uuid import UUID


def unified_port(values):
    raw = str(values.get("XRAY_UNIFIED_PORT", "")).strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError("XRAY_UNIFIED_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("XRAY_UNIFIED_PORT must be in 1..65535")
    if len(str(values.get("XRAY_UNIFIED_UUID_SECRET", ""))) < 32:
        raise ValueError("XRAY_UNIFIED_UUID_SECRET must contain at least 32 characters")
    return port


def account_uuid(values, listen_port):
    # The legacy UUID is public to subscribers and MUST NOT be a derivation key.
    port = int(listen_port)
    if not 1 <= port <= 65535:
        raise ValueError("listen_port must be in 1..65535")
    secret = values["XRAY_UNIFIED_UUID_SECRET"].encode()
    digest = hmac.new(secret, f"xray-entry-v1:{port}".encode(), hashlib.sha256).digest()
    return str(UUID(bytes=digest[:16], version=4))


def socket_path(tag):
    return f"/var/log/xray/entry-{tag}.sock"
