import json
import re
import secrets
import string
from datetime import datetime, timezone

from flask import request, url_for

from .config import LOCAL_TZ, PANEL_PUBLIC_URL, SUBSCRIPTION_NAME_PREFIX
from .errors import ValidationError


def utc_now():
    return datetime.now(timezone.utc)


def utc_iso_now():
    return utc_now().isoformat(timespec="seconds")


def parse_port(value, field_name):
    try:
        port = int(str(value).strip())
    except ValueError as exc:
        raise ValidationError(f"{field_name} 必须是数字。") from exc
    if port < 1 or port > 65535:
        raise ValidationError(f"{field_name} 必须在 1-65535 之间。")
    return port


def parse_note(value):
    note = str(value or "").strip()
    if len(note) > 200:
        raise ValidationError("备注不能超过 200 个字符。")
    return note


def normalize_customer_email(value):
    email = str(value or "").strip().lower()
    if not email or "@" not in email:
        raise ValidationError("邮箱格式不正确。")
    if len(email) > 200:
        raise ValidationError("邮箱不能超过 200 个字符。")
    local, _, domain = email.partition("@")
    if not local or not domain or "." not in domain:
        raise ValidationError("邮箱格式不正确。")
    return email


def parse_data_size(value, field_name):
    raw = str(value or "").strip()
    if not raw:
        return None

    match = re.fullmatch(r"(?i)(\d+(?:\.\d+)?)\s*([kmgtp]?i?b?)?", raw)
    if match is None:
        raise ValidationError(f"{field_name} 格式不正确，可填写 10G、500MB 或 1048576。")

    amount = float(match.group(1))
    unit = (match.group(2) or "b").lower()
    multipliers = {
        "b": 1,
        "k": 1024,
        "kb": 1024,
        "kib": 1024,
        "m": 1024**2,
        "mb": 1024**2,
        "mib": 1024**2,
        "g": 1024**3,
        "gb": 1024**3,
        "gib": 1024**3,
        "t": 1024**4,
        "tb": 1024**4,
        "tib": 1024**4,
        "p": 1024**5,
        "pb": 1024**5,
        "pib": 1024**5,
    }
    if unit not in multipliers:
        raise ValidationError(f"{field_name} 单位不支持。")
    if unit in {"b", ""} and not amount.is_integer():
        raise ValidationError(f"{field_name} 以字节为单位时必须是整数。")

    size = int(amount * multipliers[unit])
    if size <= 0:
        raise ValidationError(f"{field_name} 必须大于 0。")
    return size


def parse_expiry(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        local_dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError("到期时间格式不正确。") from exc
    if local_dt.tzinfo is None:
        local_dt = local_dt.replace(tzinfo=LOCAL_TZ)
    expires_at = local_dt.astimezone(timezone.utc)
    if expires_at <= utc_now():
        raise ValidationError("到期时间必须晚于当前时间。")
    return expires_at.isoformat(timespec="seconds")


def human_bytes(value):
    size = float(value or 0)
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return "0 B"


def format_display_time(value):
    if not value:
        return "永久"
    dt = datetime.fromisoformat(value).astimezone(LOCAL_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_input_time(value):
    if not value:
        return ""
    dt = datetime.fromisoformat(value).astimezone(LOCAL_TZ)
    return dt.strftime("%Y-%m-%dT%H:%M")


def localize_time(value):
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(LOCAL_TZ)


def format_optional_display_time(value, default="暂无"):
    localized = localize_time(value)
    if localized is None:
        return default
    return localized.strftime("%Y-%m-%d %H:%M:%S")


def generate_subscription_token(length=12):
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_access_token(length=24):
    return generate_subscription_token(length=length)


def generate_tenant_username(length=10):
    alphabet = string.ascii_lowercase + string.digits
    return "t" + "".join(secrets.choice(alphabet) for _ in range(max(length - 1, 5)))


def generate_tenant_password(length=16):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def normalize_subscription_name(note, listen_port):
    note_text = re.sub(r"\s+", " ", str(note or "").strip())
    if note_text:
        return f"{note_text}-{listen_port}"
    return f"{SUBSCRIPTION_NAME_PREFIX}-{listen_port}"


def yaml_quote(value):
    return json.dumps(str(value), ensure_ascii=False)


def current_request_scheme():
    forwarded = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    return forwarded or request.scheme


def current_request_host():
    forwarded = request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
    return forwarded or request.host


def external_url_for(endpoint, base_url=None, **values):
    public_url = PANEL_PUBLIC_URL if base_url is None else str(base_url).strip().rstrip("/")
    if public_url:
        return f"{public_url}{url_for(endpoint, **values)}"
    return f"{current_request_scheme()}://{current_request_host()}{url_for(endpoint, **values)}"


def status_payload(enabled, expires_at, traffic_limit_bytes=None, traffic_usage_bytes=0):
    expired = False
    if expires_at:
        expired = datetime.fromisoformat(expires_at) <= utc_now()
    if expired:
        return {"code": "expired", "label": "已过期"}
    if traffic_limit_bytes is not None and traffic_usage_bytes >= int(traffic_limit_bytes):
        return {"code": "quota", "label": "已达流量上限"}
    if enabled:
        return {"code": "active", "label": "运行中"}
    return {"code": "disabled", "label": "已停用"}


def port_is_expired(expires_at, now_dt=None):
    if not expires_at:
        return False
    return datetime.fromisoformat(expires_at) <= (now_dt or utc_now())
