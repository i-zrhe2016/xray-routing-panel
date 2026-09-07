#!/usr/bin/env python3
import argparse
import ipaddress
import json
import os
import re
import shlex
import socket
import sqlite3
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlparse

from app.xray.config import BASE_DIR, DEFAULT_RENDER_MODULE
from app.xray.envfile import load_env_file as shared_load_env_file
from app.xray.envfile import read_env_or_file as shared_read_env_or_file
from app.xray.node_control import DataPlaneConfig, DataPlaneController, reality_handshake_probe
from app.xray.operation_lock import LockBusyError, exclusive_file_lock


TIMESTAMP_RE = re.compile(r"^(?P<date>\d{4}/\d{2}/\d{2}) (?P<time>\d{2}:\d{2}:\d{2}(?:\.\d+)?) ")
TARGET_RE = re.compile(r" accepted (?P<proto>[a-z]+):(?P<target>\S+)")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")
PLACEHOLDER_RE = re.compile(r"__([A-Z0-9_]+)__")
UPSTREAM_LIST_SEPARATOR_RE = re.compile(r"[\n,;]+")
UNSET_PROXY_PROTOCOL = "replace_me"
SQLITE_BUSY_TIMEOUT_MS = 30000
SQLITE_LOCK_RETRY_ATTEMPTS = 4
SQLITE_LOCK_RETRY_DELAY_SECONDS = 0.25
LOCK_BUSY_EXIT_CODE = 75
FORCED_AI_ROUTE_DOMAIN_SUFFIXES = (
    # Keep Gemini's authenticated web session and API calls on the same AI
    # egress. Routing only gemini.google.com is insufficient because the web
    # app also talks to Google's auth and Generative Language endpoints.
    "accounts.google.com",
    "gemini.google.com",
    "gemini.gstatic.com",
    "generativelanguage.googleapis.com",
    "scholar.google.com",
    # AWS service APIs use *.amazonaws.com, while China and newer dual-stack
    # endpoints also use *.amazonaws.com.cn, *.api.aws, and *.on.aws.
    "amazonaws.com",
    "amazonaws.com.cn",
    "amazonaws.cn",
    "amazonwebservices.com.cn",
    "api.aws",
    "on.aws",
    # AWS console, sign-in, static assets, and dedicated AWS domains. Keep the
    # match scoped to AWS-owned families; amazon.com and cloudfront.net are
    # shared with unrelated consumer sites and are intentionally excluded.
    "aws",
    "aws.amazon.com",
    "awsapps.com",
    "awsapps.cn",
    "awscloud.com",
    "aws.dev",
    "aws.com",
    "awsplayer.com",
    "awsstatic.com",
    "aws.a2z.com",
    "aws.a2z.org.cn",
    # Keep ChatGPT's web/API traffic and its static/upload hosts on the same
    # AI egress. Routing only chatgpt.com misses OpenAI API and asset traffic.
    "chatgpt.com",
    "openai.com",
    "oaistatic.com",
    "oaiusercontent.com",
    # Keep Claude's web/API and generated-content hosts on the same AI egress.
    "anthropic.com",
    "api.ip.sb",
    "api.ipify.org",
    "checkip.amazonaws.com",
    "cip.cc",
    "claude.ai",
    "claude.com",
    "claudeusercontent.com",
    "ident.me",
    "icanhazip.com",
    "ifconfig.co",
    "ifconfig.me",
    "ip-api.com",
    "ipapi.co",
    "ipinfo.io",
    "ip.sb",
    "ipify.org",
    "ippure.com",
    "ipw.cn",
    "ipv4.icanhazip.com",
    "ipv6.icanhazip.com",
    "myip.ipip.net",
    "myexternalip.com",
    "seeip.org",
)
KNOWN_AI_DOMAIN_SUFFIXES = (
    "ai.google.dev",
    "aistudio.google.com",
    "anthropic.com",
    "chatgpt.com",
    "claude.ai",
    "codeium.com",
    "cohere.com",
    "copilot.microsoft.com",
    "cursor.com",
    "deepseek.com",
    "fal.ai",
    "fireworks.ai",
    "gemini.google.com",
    "grok.com",
    "groq.com",
    "huggingface.co",
    "ideogram.ai",
    "kimi.moonshot.cn",
    "leonardo.ai",
    "lovable.dev",
    "midjourney.com",
    "mistral.ai",
    "moonshot.cn",
    "notebooklm.google.com",
    "openai.com",
    "openrouter.ai",
    "perplexity.ai",
    "poe.com",
    "replicate.com",
    "runwayml.com",
    "stability.ai",
    "together.ai",
    "v0.dev",
    "windsurf.com",
    "x.ai",
)


def env_int(name, default):
    raw = str(os.environ.get(name, default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def env_bool(name, default):
    raw = str(os.environ.get(name, default)).strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


def load_env_file_values(path):
    if not path or not path.is_file():
        return {}
    return shared_load_env_file(path)


def read_env_or_file(name, default="", env_file_values=None):
    return shared_read_env_or_file(name, default=default, env_file_values=env_file_values)


def parse_positive_float(raw, field_name):
    try:
        value = float(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a positive number") from exc
    if value <= 0:
        raise ValueError(f"{field_name} must be a positive number")
    return value


def utc_now():
    return datetime.now(timezone.utc)


def format_timestamp(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def connect_panel_db(panel_db_path):
    conn = sqlite3.connect(panel_db_path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    return conn


def run_with_sqlite_lock_retry(operation):
    last_error = None
    for attempt in range(SQLITE_LOCK_RETRY_ATTEMPTS):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "database is locked" not in message and "database is busy" not in message:
                raise
            last_error = exc
            if attempt + 1 < SQLITE_LOCK_RETRY_ATTEMPTS:
                time.sleep(SQLITE_LOCK_RETRY_DELAY_SECONDS * (attempt + 1))
    raise last_error


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


def load_json(path, default):
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


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
            if parsed is None:
                continue
            state["events"].append(parsed)
        state["log_offset"] = handle.tell()
    state["log_inode"] = current_inode


def purge_old_events(state, lookback_seconds, now):
    cutoff = now - timedelta(seconds=lookback_seconds)
    state["events"] = [item for item in state["events"] if item["seen_at"] >= cutoff]
    return cutoff


def load_decisions(path):
    payload = load_json(path, {"domains": {}})
    domains = payload.get("domains", {})
    if not isinstance(domains, dict):
        domains = {}
    return {"domains": domains}


def normalize_classification(value):
    raw = str(value or "").strip().lower()
    if raw in {"ai", "yes", "true", "related", "ai_related"}:
        return "ai"
    if raw in {"not_ai", "no", "false", "unrelated", "non_ai"}:
        return "not_ai"
    return "unknown"


def matches_domain_suffixes(domain, suffixes):
    return any(domain == suffix or domain.endswith(f".{suffix}") for suffix in suffixes)


def matches_forced_ai_route_domain(domain):
    return matches_domain_suffixes(domain, FORCED_AI_ROUTE_DOMAIN_SUFFIXES)


def matches_known_ai_domain(domain):
    return matches_domain_suffixes(domain, KNOWN_AI_DOMAIN_SUFFIXES)


def sync_builtin_domain_decisions(decisions, decisions_path, observed_domains):
    changed = False
    candidate_domains = set(decisions["domains"]) | set(observed_domains) | set(FORCED_AI_ROUTE_DOMAIN_SUFFIXES)
    classified_at = format_timestamp(utc_now())
    for domain in sorted(candidate_domains):
        if matches_forced_ai_route_domain(domain):
            payload = {
                "classification": "ai",
                "reason": "matched_forced_ai_route_domain",
                "classified_at": classified_at,
                "source": "builtin",
                "model": "builtin-forced-ai-route-domains",
            }
        elif matches_known_ai_domain(domain):
            payload = {
                "classification": "ai",
                "reason": "matched_known_ai_domain",
                "classified_at": classified_at,
                "source": "builtin",
                "model": "builtin-known-ai-domains",
            }
        else:
            continue

        existing = decisions["domains"].get(domain)
        if existing:
            same = (
                existing.get("classification") == payload["classification"]
                and existing.get("reason") == payload["reason"]
                and existing.get("source") == payload["source"]
                and existing.get("model") == payload["model"]
            )
            if same:
                continue

        decisions["domains"][domain] = payload
        changed = True

    if changed:
        save_json(decisions_path, decisions)
    return changed


def extract_output_text(payload):
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    texts = []
    for item in payload.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                texts.append(content["text"])
    return "\n".join(texts).strip()


def normalize_openai_base_url(base_url):
    raw = str(base_url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"http://{raw}"
    return raw.rstrip("/")


def is_local_openai_base_url(base_url):
    parsed = urlparse(normalize_openai_base_url(base_url))
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in {
        "localhost",
        "127.0.0.1",
        "::1",
        "0.0.0.0",
        "host.docker.internal",
    } or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def infer_openai_api_style(base_url, allow_no_key=False):
    parsed = urlparse(normalize_openai_base_url(base_url))
    path = parsed.path.rstrip("/").lower()
    if path.endswith("/chat/completions"):
        return "chat_completions"
    if path.endswith("/responses"):
        return "responses"
    if allow_no_key or is_local_openai_base_url(base_url):
        return "chat_completions"
    return "responses"


def resolve_openai_endpoint(base_url, allow_no_key=False):
    normalized = normalize_openai_base_url(base_url)
    parsed = urlparse(normalized)
    style = infer_openai_api_style(normalized, allow_no_key=allow_no_key)
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions") or path.endswith("/responses"):
        return normalized, style
    suffix = "/chat/completions" if style == "chat_completions" else "/responses"
    resolved_path = f"{path}{suffix}" if path else f"/v1{suffix}"
    resolved = parsed._replace(path=resolved_path, params="", query="", fragment="")
    return resolved.geturl(), style


def build_openai_classification_payload(domains, model, api_style):
    system_prompt = (
        "You classify internet domains. Return JSON only. "
        "For each input domain, decide whether the website is primarily an AI product, AI model provider, "
        "AI coding tool, AI chat product, AI inference platform, or an AI-focused developer platform. "
        "Use classification 'ai' only when the domain is clearly AI-related. Use 'not_ai' otherwise."
    )
    user_prompt = json.dumps(
        {
            "task": "classify_domains",
            "domains": domains,
            "return_format": [
                {
                    "domain": "example.com",
                    "classification": "ai|not_ai",
                    "reason": "short reason",
                }
            ],
        },
        ensure_ascii=True,
    )
    if api_style == "chat_completions":
        return {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
        }
    return {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": system_prompt}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": user_prompt}],
            },
        ],
        "store": False,
    }


def extract_chat_completions_text(payload):
    texts = []
    for choice in payload.get("choices", []):
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            texts.append(content.strip())
        elif isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") in {"text", "output_text"} and item.get("text"):
                    texts.append(str(item["text"]).strip())
        if texts:
            break
    if texts:
        return "\n".join(texts).strip()
    return extract_output_text(payload)


def extract_openai_response_text(payload, api_style):
    if api_style == "chat_completions":
        return extract_chat_completions_text(payload)
    return extract_output_text(payload)


def validate_classification_results(domains, parsed):
    if not isinstance(parsed, list):
        raise RuntimeError("classifier output must be a JSON list")

    results = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain", "")).strip().lower()
        if domain not in domains:
            continue
        results[domain] = {
            "classification": normalize_classification(item.get("classification", "")),
            "reason": str(item.get("reason", "")).strip(),
        }

    missing = [domain for domain in domains if domain not in results]
    if missing:
        raise RuntimeError(f"classifier output missing domains: {', '.join(missing)}")
    return results


def sync_codex_home(source_home, runtime_home):
    runtime_home.mkdir(parents=True, exist_ok=True)
    synced_any = False
    for name in ("config.toml", "auth.json"):
        source = source_home / name
        if not source.is_file():
            continue
        target = runtime_home / name
        if not target.exists() or source.read_bytes() != target.read_bytes():
            shutil.copy2(source, target)
        synced_any = True
    if not synced_any:
        raise RuntimeError(f"codex source home is missing config/auth files: {source_home}")


def build_codex_command(args):
    configured = str(getattr(args, "codex_bin", "") or "").strip() or "codex"
    resolved = shutil.which(configured) or configured
    if not Path(resolved).is_file() and shutil.which(resolved) is None:
        raise RuntimeError("native Codex binary was not found; set CODEX_BIN")
    return [resolved]


def classify_domains_via_codex(domains, args):
    sync_codex_home(args.codex_source_home, args.codex_runtime_home)
    args.codex_workdir.mkdir(parents=True, exist_ok=True)

    output_path = args.codex_workdir / "codex-last-message.json"
    output_path.unlink(missing_ok=True)

    prompt = json.dumps(
        {
            "task": "classify_domains",
            "rules": {
                "classify_as_ai_when": [
                    "the domain is primarily an AI product",
                    "the domain is an AI model provider",
                    "the domain is an AI coding tool",
                    "the domain is an AI chat product",
                    "the domain is an AI inference platform",
                    "the domain is an AI-focused developer platform",
                ],
                "otherwise": "not_ai",
            },
            "domains": domains,
            "return_format": [
                {
                    "domain": "example.com",
                    "classification": "ai|not_ai",
                    "reason": "short reason",
                }
            ],
            "output_constraints": [
                "Return JSON only",
                "Return exactly one item per input domain",
                "Do not include markdown",
            ],
        },
        ensure_ascii=True,
    )

    command = build_codex_command(args) + [
        "exec",
        "-C",
        str(args.codex_workdir),
        "--skip-git-repo-check",
        "--ignore-rules",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--output-last-message",
        str(output_path),
    ]
    if args.codex_model:
        command.extend(["--model", args.codex_model])
    command.append(prompt)

    env = os.environ.copy()
    env["CODEX_HOME"] = str(args.codex_runtime_home)
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=args.codex_timeout_seconds,
        env=env,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "codex exec failed"
        raise RuntimeError(detail)
    if not output_path.is_file():
        raise RuntimeError("codex did not produce output-last-message")

    raw = output_path.read_text(encoding="utf-8").strip()
    if not raw:
        raise RuntimeError("codex output-last-message was empty")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"codex output was not valid JSON: {raw}") from exc
    return validate_classification_results(domains, parsed)


def classify_domains_via_openai(domains, api_key, model, base_url, timeout_seconds, allow_no_key=False):
    endpoint, api_style = resolve_openai_endpoint(base_url, allow_no_key=allow_no_key)
    payload = build_openai_classification_payload(domains, model, api_style)
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"openai http {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"openai request failed: {exc}") from exc

    payload = json.loads(raw)
    text = extract_openai_response_text(payload, api_style)
    if not text:
        raise RuntimeError(f"openai-compatible {api_style} response did not contain output text")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"openai-compatible output was not valid JSON: {text}") from exc
    return validate_classification_results(domains, parsed)


def read_panel_target(panel_db_path, preferred_listen_port):
    if not panel_db_path.is_file():
        return None

    def read_target():
        conn = connect_panel_db(panel_db_path)
        try:
            if preferred_listen_port:
                row = conn.execute(
                    """
                    SELECT listen_port, upstream_host, upstream_port, note, updated_at
                    FROM ports
                    WHERE enabled = 1 AND listen_port = ?
                    LIMIT 1
                    """,
                    (preferred_listen_port,),
                ).fetchone()
                if row:
                    return dict(row)
            row = conn.execute(
                """
                SELECT listen_port, upstream_host, upstream_port, note, updated_at
                FROM ports
                WHERE enabled = 1
                ORDER BY updated_at DESC, listen_port ASC
                LIMIT 1
                """
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    return run_with_sqlite_lock_retry(read_target)


def ensure_ai_domain_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ai_domains (
            domain TEXT PRIMARY KEY,
            classification TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            first_seen TEXT,
            last_seen TEXT,
            total_hits INTEGER NOT NULL DEFAULT 0,
            last_protocols TEXT NOT NULL DEFAULT '[]',
            last_report_window_start TEXT,
            last_report_window_end TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ai_domain_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT NOT NULL,
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,
            hits INTEGER NOT NULL,
            classification TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            protocols TEXT NOT NULL DEFAULT '[]',
            first_seen TEXT,
            last_seen TEXT,
            created_at TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_domain_observations_window
        ON ai_domain_observations(domain, window_start, window_end);

        CREATE INDEX IF NOT EXISTS idx_ai_domain_observations_domain
        ON ai_domain_observations(domain);
        """
    )


def _save_ai_domains_to_panel_db_once(panel_db_path, report, decisions):
    status = {
        "status": "skipped",
        "reason": "",
        "path": str(panel_db_path),
        "domains_upserted": 0,
        "observations_upserted": 0,
    }
    if not panel_db_path.is_file():
        status["reason"] = "panel_db_missing"
        return status

    observed_ai_items = [item for item in report["domains"] if item["classification"] == "ai"]
    observed_ai_by_domain = {item["domain"]: item for item in observed_ai_items}
    conn = connect_panel_db(panel_db_path)
    try:
        ensure_ai_domain_schema(conn)

        historical_ai_domains = {
            row["domain"]
            for row in conn.execute(
                "SELECT DISTINCT domain FROM ai_domain_observations WHERE classification = 'ai'"
            ).fetchall()
        }
        historical_ai_domains.update(
            row["domain"]
            for row in conn.execute(
                "SELECT domain FROM ai_domains WHERE classification = 'ai' AND total_hits > 0"
            ).fetchall()
        )
        ai_domains = sorted(
            domain
            for domain, item in decisions["domains"].items()
            if item.get("classification") == "ai"
            and (domain in observed_ai_by_domain or domain in historical_ai_domains)
        )

        for item in observed_ai_items:
            decision = decisions["domains"].get(item["domain"], {})
            protocols = json.dumps(item["protocols"], ensure_ascii=True)
            conn.execute(
                """
                INSERT INTO ai_domain_observations (
                    domain,
                    window_start,
                    window_end,
                    hits,
                    classification,
                    reason,
                    source,
                    model,
                    protocols,
                    first_seen,
                    last_seen,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(domain, window_start, window_end) DO UPDATE SET
                    hits = excluded.hits,
                    classification = excluded.classification,
                    reason = excluded.reason,
                    source = excluded.source,
                    model = excluded.model,
                    protocols = excluded.protocols,
                    first_seen = excluded.first_seen,
                    last_seen = excluded.last_seen,
                    created_at = excluded.created_at
                """,
                (
                    item["domain"],
                    report["window_start"],
                    report["window_end"],
                    item["hits"],
                    item["classification"],
                    item["reason"],
                    str(decision.get("source", "")).strip(),
                    str(decision.get("model", "")).strip(),
                    protocols,
                    item.get("first_seen"),
                    item.get("last_seen"),
                    report["generated_at"],
                ),
            )
            status["observations_upserted"] += 1

        for domain in ai_domains:
            item = observed_ai_by_domain.get(
                domain,
                {
                    "domain": domain,
                    "classification": "ai",
                    "reason": decisions["domains"].get(domain, {}).get("reason", ""),
                    "protocols": [],
                    "first_seen": None,
                    "last_seen": None,
                },
            )
            decision = decisions["domains"].get(domain, {})
            aggregate = conn.execute(
                """
                SELECT
                    COALESCE(SUM(hits), 0) AS total_hits,
                    MIN(COALESCE(first_seen, last_seen)) AS first_seen,
                    MAX(last_seen) AS last_seen
                FROM ai_domain_observations
                WHERE domain = ?
                """,
                (domain,),
            ).fetchone()
            existing = conn.execute(
                "SELECT last_protocols FROM ai_domains WHERE domain = ?",
                (domain,),
            ).fetchone()
            protocols = json.dumps(item["protocols"], ensure_ascii=True)
            if not item["protocols"] and existing and existing["last_protocols"]:
                protocols = existing["last_protocols"]
            conn.execute(
                """
                INSERT INTO ai_domains (
                    domain,
                    classification,
                    reason,
                    source,
                    model,
                    first_seen,
                    last_seen,
                    total_hits,
                    last_protocols,
                    last_report_window_start,
                    last_report_window_end,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    classification = excluded.classification,
                    reason = excluded.reason,
                    source = excluded.source,
                    model = excluded.model,
                    first_seen = excluded.first_seen,
                    last_seen = excluded.last_seen,
                    total_hits = excluded.total_hits,
                    last_protocols = excluded.last_protocols,
                    last_report_window_start = excluded.last_report_window_start,
                    last_report_window_end = excluded.last_report_window_end,
                    updated_at = excluded.updated_at
                """,
                (
                    domain,
                    item["classification"],
                    item["reason"],
                    str(decision.get("source", "")).strip(),
                    str(decision.get("model", "")).strip(),
                    aggregate["first_seen"] or item.get("first_seen"),
                    aggregate["last_seen"] or item.get("last_seen"),
                    int(aggregate["total_hits"]),
                    protocols,
                    report["window_start"],
                    report["window_end"],
                    report["generated_at"],
                ),
            )
            status["domains_upserted"] += 1

        if ai_domains:
            placeholders = ", ".join("?" for _ in ai_domains)
            conn.execute(
                f"DELETE FROM ai_domains WHERE domain NOT IN ({placeholders})",
                ai_domains,
            )
        else:
            conn.execute("DELETE FROM ai_domains")

        conn.commit()
        status["status"] = "written"
        return status
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_ai_domains_to_panel_db(panel_db_path, report, decisions):
    try:
        return run_with_sqlite_lock_retry(
            lambda: _save_ai_domains_to_panel_db_once(panel_db_path, report, decisions)
        )
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "database is locked" not in message and "database is busy" not in message:
            raise
        return {
            "status": "skipped",
            "reason": "database_locked",
            "path": str(panel_db_path),
            "domains_upserted": 0,
            "observations_upserted": 0,
        }


def join_host_port(host, port):
    host_text = str(host).strip()
    if ":" in host_text and not host_text.startswith("["):
        host_text = f"[{host_text}]"
    return f"{host_text}:{int(port)}"


def build_template_upstream_candidate(host, port):
    return {
        "upstream_host": str(host).strip(),
        "upstream_port": int(port),
        "candidate_type": "template",
    }


def parse_upstream_endpoint(raw, default_port=None, field_name="AI_UPSTREAMS"):
    text = str(raw or "").strip()
    if not text:
        raise ValueError(f"{field_name} contains an empty upstream entry")

    host = ""
    port_text = None
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise ValueError(f"{field_name} entry {text!r} has an invalid IPv6 format")
        host = text[1:end].strip()
        remainder = text[end + 1:].strip()
        if remainder:
            if not remainder.startswith(":"):
                raise ValueError(f"{field_name} entry {text!r} must use [host]:port for IPv6")
            port_text = remainder[1:].strip()
    else:
        colon_count = text.count(":")
        if colon_count == 0:
            host = text
        elif colon_count == 1:
            host, port_text = text.rsplit(":", 1)
        else:
            host = text

    host = host.strip()
    if not host:
        raise ValueError(f"{field_name} entry {text!r} is missing a host")

    if port_text is None or not port_text:
        if default_port is None:
            raise ValueError(f"{field_name} entry {text!r} is missing a port")
        port = int(default_port)
    else:
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ValueError(f"{field_name} entry {text!r} has an invalid port") from exc

    if port <= 0 or port > 65535:
        raise ValueError(f"{field_name} entry {text!r} must use a port in 1..65535")

    return {
        "upstream_host": host,
        "upstream_port": port,
    }


def parse_vless_fallback_url(raw, field_name="AI_UPSTREAM_FALLBACK_URL"):
    text = str(raw or "").strip()
    if not text:
        return None

    parsed = urlparse(text)
    if parsed.scheme.lower() != "vless":
        raise ValueError(f"{field_name} must use a vless:// URL")
    if not parsed.username:
        raise ValueError(f"{field_name} is missing the VLESS UUID")
    if not parsed.hostname:
        raise ValueError(f"{field_name} is missing the upstream host")
    if parsed.port is None:
        raise ValueError(f"{field_name} is missing the upstream port")

    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    network = str(params.get("type", "tcp")).strip().lower() or "tcp"
    if network != "tcp":
        raise ValueError(f"{field_name} currently supports only type=tcp")

    security = str(params.get("security", "none")).strip().lower() or "none"
    encryption = str(params.get("encryption", "none")).strip() or "none"
    user = {
        "id": unquote(parsed.username),
        "encryption": encryption,
    }
    flow = str(params.get("flow", "")).strip()
    if flow:
        user["flow"] = flow

    outbound = {
        "tag": "ai_proxy",
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": parsed.hostname,
                    "port": parsed.port,
                    "users": [user],
                }
            ]
        },
        "streamSettings": {
            "network": network,
            "security": security,
        },
    }

    if security == "reality":
        sni = str(params.get("sni", "")).strip()
        fingerprint = str(params.get("fp", "")).strip()
        public_key = str(params.get("pbk", "")).strip()
        short_id = str(params.get("sid", "")).strip()
        if not sni or not fingerprint or not public_key or not short_id:
            raise ValueError(
                f"{field_name} must include sni, fp, pbk, and sid when security=reality"
            )

        reality_settings = {
            "serverName": sni,
            "fingerprint": fingerprint,
            "publicKey": public_key,
            "shortId": short_id,
        }
        pq_verify = str(params.get("pqv", "")).strip()
        if pq_verify:
            reality_settings["mldsa65Verify"] = pq_verify
        spider_x = str(params.get("spx", "")).strip()
        if spider_x:
            reality_settings["spiderX"] = spider_x
        outbound["streamSettings"]["realitySettings"] = reality_settings
    elif security not in {"none", ""}:
        raise ValueError(f"{field_name} currently supports only security=reality or security=none")

    return {
        "upstream_host": parsed.hostname,
        "upstream_port": int(parsed.port),
        "candidate_type": "share_url",
        "candidate_label": unquote(parsed.fragment).strip(),
        "proxy_payload_override": {"outbounds": [outbound]},
        "probe_server_name": str(
            (outbound.get("streamSettings", {}).get("realitySettings", {}) or {}).get("serverName", "")
        ).strip(),
    }


def parse_upstream_list(raw, default_port=None, field_name="AI_UPSTREAMS"):
    text = str(raw or "").strip()
    if not text:
        return []

    candidates = []
    for token in UPSTREAM_LIST_SEPARATOR_RE.split(text):
        token = token.strip()
        if not token:
            continue
        candidates.append(parse_upstream_endpoint(token, default_port=default_port, field_name=field_name))
    return candidates


def dedupe_upstream_candidates(candidates):
    unique = []
    seen = set()
    for candidate in candidates:
        key = (
            str(candidate["upstream_host"]).strip().lower(),
            int(candidate["upstream_port"]),
            str(candidate.get("candidate_type", "template")).strip().lower(),
            str(candidate.get("candidate_label", "")).strip(),
        )
        if key in seen:
            continue
        seen.add(key)
        normalized = dict(candidate)
        normalized["upstream_host"] = str(candidate["upstream_host"]).strip()
        normalized["upstream_port"] = int(candidate["upstream_port"])
        unique.append(normalized)
    return unique


def build_ai_upstream_candidates(
    primary_host,
    primary_port,
    upstreams_raw="",
    fallbacks_raw="",
    fallback_share_url="",
):
    if str(upstreams_raw or "").strip():
        candidates = parse_upstream_list(
            upstreams_raw,
            default_port=primary_port,
            field_name="AI_UPSTREAMS",
        )
        candidates = [build_template_upstream_candidate(item["upstream_host"], item["upstream_port"]) for item in candidates]
    else:
        candidates = [
            build_template_upstream_candidate(primary_host, primary_port)
        ]
        fallback_candidate = parse_vless_fallback_url(fallback_share_url, field_name="AI_UPSTREAM_FALLBACK_URL")
        if fallback_candidate:
            candidates.append(fallback_candidate)
        candidates.extend(
            [
                build_template_upstream_candidate(item["upstream_host"], item["upstream_port"])
                for item in parse_upstream_list(
                    fallbacks_raw,
                    default_port=primary_port,
                    field_name="AI_UPSTREAM_FALLBACKS",
                )
            ]
        )

    candidates = dedupe_upstream_candidates(candidates)
    if not candidates:
        raise ValueError("at least one AI upstream must be configured")
    return candidates


def probe_ai_upstream_candidate(candidate, timeout_seconds, probe_controller=None):
    checked_at = format_timestamp(utc_now())
    reachable = False
    failure_reason = ""
    probe_server_name = str(candidate.get("probe_server_name", "")).strip()
    if probe_controller is not None:
        if probe_server_name:
            probe_result = probe_controller.probe_reality_endpoint(
                candidate["upstream_host"],
                candidate["upstream_port"],
                probe_server_name,
                timeout_seconds,
            )
        else:
            probe_result = probe_controller.probe_tcp_endpoint(
                candidate["upstream_host"],
                candidate["upstream_port"],
                timeout_seconds,
            )
        if not isinstance(probe_result, dict):
            probe_result = {
                "ok": False,
                "error": "AI 上游探测返回格式无效",
                "management_error": True,
                "method": "unknown",
            }
        reachable = bool(probe_result.get("ok"))
        failure_reason = str(probe_result.get("error", "")).strip()[:200]
        probe_method = str(probe_result.get("method", "tcp")).strip() or "tcp"
        management_error = bool(probe_result.get("management_error"))
    elif probe_server_name:
        probe_result = reality_handshake_probe(
            candidate["upstream_host"],
            candidate["upstream_port"],
            probe_server_name,
            timeout=timeout_seconds,
        )
        reachable = bool(probe_result.get("ok"))
        failure_reason = str(probe_result.get("error", "")).strip()[:200]
        probe_method = "reality"
        management_error = False
    else:
        try:
            with socket.create_connection(
                (candidate["upstream_host"], int(candidate["upstream_port"])),
                timeout=timeout_seconds,
            ):
                reachable = True
        except OSError as exc:
            failure_reason = str(exc)[:200]
        probe_method = "tcp"
        management_error = False

    result = dict(candidate)
    result.update(
        {
            "upstream_host": candidate["upstream_host"],
            "upstream_port": int(candidate["upstream_port"]),
            "is_reachable": reachable,
            "failure_reason": failure_reason,
            "checked_at": checked_at,
            "probe_method": probe_method,
            "probe_management_error": management_error,
        }
    )
    return result


def summarize_ai_target_candidate(candidate):
    summary = {
        "upstream_host": candidate["upstream_host"],
        "upstream_port": int(candidate["upstream_port"]),
        "candidate_type": candidate.get("candidate_type", "template"),
        "is_reachable": bool(candidate.get("is_reachable")),
        "failure_reason": str(candidate.get("failure_reason", "")).strip(),
        "checked_at": str(candidate.get("checked_at", "")).strip(),
        "probe_method": str(candidate.get("probe_method", "tcp")).strip() or "tcp",
    }
    if candidate.get("probe_management_error"):
        summary["probe_management_error"] = True
    label = str(candidate.get("candidate_label", "")).strip()
    if label:
        summary["candidate_label"] = label
    return summary


def summarize_ai_target_for_report(ai_target):
    if ai_target.get("probe_status") == "manual_fallback":
        return dict(ai_target)
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
    summary["candidates"] = list(ai_target.get("candidates", []))
    return summary


def should_fallback_to_primary_route(ai_target):
    if not isinstance(ai_target, dict):
        return False
    return str(ai_target.get("probe_status", "")).strip().lower() == "all_unreachable"


def read_ai_routing_manual_mode(panel_db_path):
    path = str(panel_db_path or "").strip()
    if not path:
        return "auto"

    def read_mode():
        conn = connect_panel_db(path)
        try:
            return conn.execute(
                "SELECT value FROM app_state WHERE key = 'ai_routing_manual_mode'"
            ).fetchone()
        finally:
            conn.close()

    try:
        row = run_with_sqlite_lock_retry(read_mode)
    except (OSError, sqlite3.Error):
        return "auto"
    mode = str(row[0] if row else "auto").strip().lower()
    return mode if mode in {"auto", "primary", "backup", "forced_fallback"} else "auto"


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
            "failure_reason": selected["failure_reason"] if (
                all_unreachable
                or management_error
                or (preferred_index is not None and not selected["is_reachable"])
            ) else "",
            "candidates": [summarize_ai_target_candidate(item) for item in probes],
        }
    )
    return selected_target


def build_proxy_sockopt_payload():
    sockopt = {}
    if env_bool("XRAY_TCP_FAST_OPEN", True):
        sockopt["tcpFastOpen"] = True

    keepalive_idle = env_int("XRAY_TCP_KEEPALIVE_IDLE", 180)
    if keepalive_idle > 0:
        sockopt["tcpKeepAliveIdle"] = keepalive_idle

    keepalive_interval = env_int("XRAY_TCP_KEEPALIVE_INTERVAL", 30)
    if keepalive_interval > 0:
        sockopt["tcpKeepAliveInterval"] = keepalive_interval

    return sockopt


def apply_default_proxy_sockopt(outbounds):
    default_sockopt = build_proxy_sockopt_payload()
    if not default_sockopt:
        return

    for outbound in outbounds:
        if not isinstance(outbound, dict):
            continue
        stream_settings = outbound.get("streamSettings")
        if not isinstance(stream_settings, dict):
            continue
        network = str(stream_settings.get("network", "tcp")).strip().lower()
        if network and network != "tcp":
            continue

        existing = stream_settings.get("sockopt", {})
        if not isinstance(existing, dict):
            existing = {}
        merged = dict(existing)
        for key, value in default_sockopt.items():
            merged.setdefault(key, value)
        # Keep the AI relay connection on IPv4 even when the data plane is
        # dual-stack. The AI node intentionally has no IPv6 egress.
        merged.setdefault("domainStrategy", "UseIPv4")
        stream_settings["sockopt"] = merged


def build_default_proxy_payload(ai_target):
    return {
        "outbounds": [
            {
                "tag": "ai_proxy",
                "protocol": "freedom",
                "settings": {
                    # The AI node is IPv4-only by design. Resolve the relay
                    # endpoint over IPv4 so a dual-stack data plane cannot
                    # create a second egress path before reaching the AI node.
                    "domainStrategy": "UseIPv4",
                    "redirect": join_host_port(ai_target["upstream_host"], ai_target["upstream_port"]),
                    "proxyProtocol": 0,
                    "finalRules": [{"action": "allow"}],
                },
            }
        ]
    }


def render_proxy_template(template_path, ai_target, panel_target):
    override_payload = ai_target.get("proxy_payload_override")
    if isinstance(override_payload, dict):
        outbounds = override_payload.get("outbounds", [])
        if not isinstance(outbounds, list) or not outbounds:
            return None, "proxy_payload_override_has_no_outbounds"
        apply_default_proxy_sockopt(outbounds)
        return {"outbounds": outbounds}, "share_url_override"

    if not template_path or not template_path.is_file():
        return build_default_proxy_payload(ai_target), "builtin_freedom_redirect"
    raw = template_path.read_text(encoding="utf-8")
    replacements = {
        "AI_UPSTREAM_HOST": str(ai_target["upstream_host"]),
        "AI_UPSTREAM_PORT": str(ai_target["upstream_port"]),
        "PANEL_LISTEN_PORT": str(panel_target["listen_port"]) if panel_target else "",
        "PANEL_UPSTREAM_HOST": str(panel_target["upstream_host"]) if panel_target else str(ai_target["upstream_host"]),
        "PANEL_UPSTREAM_PORT": (
            str(panel_target["upstream_port"]) if panel_target else str(ai_target["upstream_port"])
        ),
    }

    def replace(match):
        return replacements.get(match.group(1), match.group(0))

    rendered = PLACEHOLDER_RE.sub(replace, raw)
    try:
        parsed = json.loads(rendered)
    except json.JSONDecodeError as exc:
        return None, f"invalid_proxy_template_json: {exc}"

    if isinstance(parsed, dict) and "outbounds" in parsed:
        outbounds = parsed.get("outbounds")
    elif isinstance(parsed, list):
        outbounds = parsed
    else:
        outbounds = [parsed]

    if not isinstance(outbounds, list) or not outbounds:
        return None, "proxy_template_has_no_outbounds"

    first = outbounds[0]
    if not isinstance(first, dict):
        return None, "proxy_template_first_outbound_invalid"
    if str(first.get("protocol", "")).strip() == UNSET_PROXY_PROTOCOL:
        return None, "proxy_template_protocol_placeholder_not_replaced"
    if str(first.get("tag", "")).strip() != "ai_proxy":
        first["tag"] = "ai_proxy"
    apply_default_proxy_sockopt(outbounds)
    return {"outbounds": outbounds}, ""


def extract_probe_server_name(proxy_payload):
    if not isinstance(proxy_payload, dict):
        return ""
    for outbound in proxy_payload.get("outbounds", []):
        if not isinstance(outbound, dict) or outbound.get("tag") != "ai_proxy":
            continue
        stream_settings = outbound.get("streamSettings", {})
        reality_settings = stream_settings.get("realitySettings", {}) if isinstance(stream_settings, dict) else {}
        if isinstance(reality_settings, dict):
            server_name = str(reality_settings.get("serverName", "")).strip()
            if server_name:
                return server_name
    return ""


def resolve_probe_server_name(template_path, candidate, panel_target, explicit_server_name=""):
    candidate_server_name = str(candidate.get("probe_server_name", "")).strip()
    if candidate_server_name:
        return candidate_server_name
    explicit = str(explicit_server_name or "").strip()
    if explicit:
        return explicit
    try:
        proxy_payload, _reason = render_proxy_template(template_path, candidate, panel_target)
    except (OSError, ValueError, TypeError):
        return ""
    return extract_probe_server_name(proxy_payload)


def write_routing_fragment(path, ai_domains, proxy_payload):
    if not ai_domains or proxy_payload is None:
        path.unlink(missing_ok=True)
        return False
    fragment = {
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                {
                    "type": "field",
                    "domain": [f"domain:{domain}" for domain in sorted(ai_domains)],
                    "outboundTag": "ai_proxy",
                }
            ],
        },
        "outbounds": proxy_payload["outbounds"],
    }
    save_json(path, fragment)
    return True


def build_domain_report(state, cutoff, now, decisions, ai_target, panel_target, route_status):
    route_status_code = str(route_status.get("status", "unknown") or "unknown").strip()

    def domain_route(classification):
        if classification != "ai":
            return {
                "outbound_tag": "direct",
                "path": "normal_data_plane",
                "target": None,
                "status": route_status_code,
                "reason": "classification_not_ai",
            }
        if route_status_code == "applied":
            target = None
            if isinstance(ai_target, dict) and ai_target.get("upstream_host"):
                target = {
                    "upstream_host": ai_target["upstream_host"],
                    "upstream_port": int(ai_target["upstream_port"]),
                }
            return {
                "outbound_tag": "ai_proxy",
                "path": "ai_node",
                "target": target,
                "status": route_status_code,
                "reason": str(route_status.get("reason", "") or "").strip(),
            }
        if route_status_code in {
            "disabled",
            "idle",
            "fallback_to_primary",
            "manual_fallback",
            "manual_target_unreachable",
            "pending_proxy_template",
        }:
            return {
                "outbound_tag": "direct",
                "path": "normal_data_plane",
                "target": None,
                "status": route_status_code,
                "reason": str(route_status.get("reason", "") or "").strip() or "ai_route_not_applied",
            }
        return {
            "outbound_tag": "unknown",
            "path": "unknown",
            "target": None,
            "status": route_status_code,
            "reason": str(route_status.get("reason", "") or "").strip() or "route_status_unavailable",
        }

    domains = {}
    protocols = {}
    for item in state["events"]:
        decision = decisions["domains"].get(item["domain"], {})
        domain_item = domains.setdefault(
            item["domain"],
            {
                "domain": item["domain"],
                "hits": 0,
                "first_seen": item["seen_at"],
                "last_seen": item["seen_at"],
                "protocols": set(),
                "classification": decision.get("classification", "unknown"),
                "reason": decision.get("reason", ""),
                "source": str(decision.get("source", "") or "").strip(),
                "model": str(decision.get("model", "") or "").strip(),
            },
        )
        domain_item["hits"] += 1
        domain_item["protocols"].add(item["protocol"])
        if item["seen_at"] < domain_item["first_seen"]:
            domain_item["first_seen"] = item["seen_at"]
        if item["seen_at"] > domain_item["last_seen"]:
            domain_item["last_seen"] = item["seen_at"]
        protocols[item["protocol"]] = protocols.get(item["protocol"], 0) + 1

    domain_items = sorted(
        (
            {
                "domain": item["domain"],
                "hits": item["hits"],
                "first_seen": format_timestamp(item["first_seen"]),
                "last_seen": format_timestamp(item["last_seen"]),
                "protocols": sorted(item["protocols"]),
                "classification": item["classification"],
                "reason": item["reason"],
                "source": item["source"] or "unknown",
                "model": item["model"],
                "traffic_route": domain_route(item["classification"]),
            }
            for item in domains.values()
        ),
        key=lambda item: (-item["hits"], item["domain"]),
    )
    ai_domains = [item["domain"] for item in domain_items if item["classification"] == "ai"]
    return {
        "generated_at": format_timestamp(now),
        "window_start": format_timestamp(cutoff),
        "window_end": format_timestamp(now),
        "unique_domains": len(domain_items),
        "ai_domains": ai_domains,
        "domains": domain_items,
        "protocols": [
            {"protocol": protocol, "hits": hits}
            for protocol, hits in sorted(protocols.items())
        ],
        "ai_target": summarize_ai_target_for_report(ai_target) if ai_target else None,
        "panel_target": panel_target,
        "route_status": route_status,
    }


def write_domain_report(output_dir, report):
    output_dir.mkdir(parents=True, exist_ok=True)
    history_dir = output_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    latest_json = output_dir / "latest.json"
    latest_txt = output_dir / "latest.txt"
    stamp = report["window_end"].replace(":", "").replace("-", "").replace("+00:00", "Z")
    history_json = history_dir / f"{stamp}.json"
    history_txt = history_dir / f"{stamp}.txt"

    payload = json.dumps(report, indent=2, ensure_ascii=True) + "\n"
    latest_json.write_text(payload, encoding="utf-8")
    history_json.write_text(payload, encoding="utf-8")

    lines = [
        f"generated_at: {report['generated_at']}",
        f"window_start: {report['window_start']}",
        f"window_end: {report['window_end']}",
        f"unique_domains: {report['unique_domains']}",
        f"ai_domains: {len(report['ai_domains'])}",
        f"route_status: {report['route_status'].get('status', 'unknown')}",
    ]
    if report["route_status"].get("config_retried"):
        lines.append("config_retried: true")
    if report["route_status"].get("config_apply_status"):
        lines.append(f"config_apply_status: {report['route_status']['config_apply_status']}")
    if report.get("panel_db_status"):
        lines.append(
            "panel_db_status: "
            f"{report['panel_db_status'].get('status', 'unknown')} "
            f"(ai_domains={report['panel_db_status'].get('domains_upserted', 0)}, "
            f"observations={report['panel_db_status'].get('observations_upserted', 0)})"
        )
    if report.get("ai_target"):
        lines.append(
            "ai_target: "
            f"{report['ai_target']['upstream_host']}:{report['ai_target']['upstream_port']}"
        )
        if report["ai_target"].get("candidate_count", 0) > 1:
            lines.append(
                "ai_target_selection: "
                f"{report['ai_target']['selected_number']}/{report['ai_target']['candidate_count']} "
                f"({'fallback' if report['ai_target'].get('failover_active') else 'primary'})"
            )
        if report["ai_target"].get("probe_status"):
            lines.append(f"ai_target_probe_status: {report['ai_target']['probe_status']}")
        candidates = report["ai_target"].get("candidates", [])
        if candidates:
            lines.append(
                "ai_target_candidates: "
                + ", ".join(
                    f"{item['upstream_host']}:{item['upstream_port']}({'ok' if item['is_reachable'] else 'down'})"
                    for item in candidates
                )
            )
    if report["panel_target"]:
        lines.append(
            "panel_target: "
            f"{report['panel_target']['upstream_host']}:{report['panel_target']['upstream_port']} "
            f"(listen_port={report['panel_target']['listen_port']})"
        )
    lines.append("")
    if report["domains"]:
        for item in report["domains"]:
            protocols = ",".join(item["protocols"])
            lines.append(
                f"{item['domain']}\thits={item['hits']}\tclass={item['classification']}\t"
                f"source={item.get('source', 'unknown')}\troute={item.get('traffic_route', {}).get('outbound_tag', 'unknown')}\t"
                f"last_seen={item['last_seen']}\tprotocols={protocols}"
            )
    else:
        lines.append("no domains observed in the last window")
    text = "\n".join(lines) + "\n"
    latest_txt.write_text(text, encoding="utf-8")
    history_txt.write_text(text, encoding="utf-8")


def rerender_config(render_script, env_file, config_out, client_out, share_out, dynamic_routing_file):
    render_entry = str(render_script).strip() or DEFAULT_RENDER_MODULE
    panel_ports_file = Path(config_out).with_name("panel-ports.json")
    command = [sys.executable]
    if render_entry.endswith(".py") or "/" in render_entry or "\\" in render_entry:
        command.append(render_entry)
    else:
        command.extend(["-m", render_entry])
    command.extend(
        [
            "--env-file",
            str(env_file),
            "--config-out",
            str(config_out),
            "--client-out",
            str(client_out),
            "--share-out",
            str(share_out),
            "--dynamic-routing-file",
            str(dynamic_routing_file),
            "--panel-ports-file",
            str(panel_ports_file),
        ]
    )
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "render_config failed"
        raise RuntimeError(detail)


def restart_xray_container(container_name, timeout_seconds):
    if not container_name:
        return
    completed = subprocess.run(
        ["docker", "restart", container_name],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "docker restart failed"
        raise RuntimeError(detail)


def restart_xray_command(command, timeout_seconds):
    if not command:
        return
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
        shell=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "restart command failed"
        raise RuntimeError(detail)


def build_data_plane_controller(args):
    upstream_host = ""
    upstream_port = None
    if getattr(args, "ai_upstream_candidates", None):
        upstream_host = str(args.ai_upstream_candidates[0]["upstream_host"])
        upstream_port = int(args.ai_upstream_candidates[0]["upstream_port"])
    access_log_path = str(getattr(args, "data_plane_access_log_path", "") or "").strip()
    if not access_log_path and args.data_plane_ssh_target and args.data_plane_config_path:
        config_path = Path(args.data_plane_config_path)
        if config_path.name == "config.json" and config_path.parent.name == "runtime":
            access_log_path = str(config_path.parent.parent / "logs" / "access.log")
    data_plane_config_path = str(getattr(args, "data_plane_config_path", "") or "").strip()
    panel_ports_path = ""
    if data_plane_config_path:
        panel_ports_path = str(Path(data_plane_config_path).with_name("panel-ports.json"))
    source_panel_ports_path = Path(args.config_out).with_name("panel-ports.json")
    return DataPlaneController(
        DataPlaneConfig(
            role="data_plane",
            label="数据面",
            api_server=args.data_plane_api_server,
            xray_bin=args.data_plane_xray_bin,
            local_bin=args.data_plane_local_bin,
            docker_bin=args.data_plane_docker_bin,
            container_name=args.data_plane_container_name,
            restart_command=args.data_plane_restart_command,
            ssh_target=args.data_plane_ssh_target,
            ssh_bin=args.data_plane_ssh_bin,
            ssh_options=tuple(args.data_plane_ssh_options),
            ssh_known_hosts_file=str(getattr(args, "data_plane_ssh_known_hosts_file", "") or "").strip(),
            remote_command_timeout=float(getattr(args, "data_plane_remote_command_timeout", 8.0) or 8.0),
            config_path=args.data_plane_config_path,
            dynamic_routing_path=args.data_plane_dynamic_routing_path,
            access_log_path=access_log_path,
            panel_ports_path=panel_ports_path,
            source_config_path=args.config_out,
            source_dynamic_routing_path=args.dynamic_routing_path,
            source_panel_ports_path=source_panel_ports_path,
            upstream_host=upstream_host,
            upstream_port=upstream_port,
        )
    )


def classify_pending_domains(decisions, decisions_path, observed_domains, args):
    known = set(decisions["domains"])
    pending = sorted(domain for domain in observed_domains if domain not in known)
    if not pending:
        return []

    classified_at = format_timestamp(utc_now())
    remaining = []
    for domain in pending:
        if matches_known_ai_domain(domain):
            decisions["domains"][domain] = {
                "classification": "ai",
                "reason": "matched_known_ai_domain",
                "classified_at": classified_at,
                "source": "builtin",
                "model": "builtin-known-ai-domains",
            }
        else:
            remaining.append(domain)
    if len(remaining) != len(pending):
        save_json(decisions_path, decisions)

    if not remaining:
        return []

    if args.codex_classifier_enabled:
        unresolved = []
        for start in range(0, len(remaining), args.batch_size):
            batch = remaining[start:start + args.batch_size]
            try:
                results = classify_domains_via_codex(batch, args)
            except Exception as exc:
                print(f"[ai_domain_manager] codex classifier unavailable: {exc}", file=sys.stderr, flush=True)
                unresolved = remaining[start:]
                break
            classified_at = format_timestamp(utc_now())
            for domain in batch:
                result = results[domain]
                decisions["domains"][domain] = {
                    "classification": result["classification"],
                    "reason": result["reason"],
                    "classified_at": classified_at,
                    "source": "codex",
                    "model": args.codex_model or "config-default",
                }
            save_json(decisions_path, decisions)
        else:
            return []
        remaining = unresolved

    if not remaining or not args.openai_classifier_enabled:
        return remaining

    unresolved = []
    for start in range(0, len(remaining), args.batch_size):
        batch = remaining[start:start + args.batch_size]
        try:
            results = classify_domains_via_openai(
                batch,
                args.openai_api_key,
                args.openai_model,
                args.openai_base_url,
                args.openai_timeout_seconds,
                allow_no_key=args.openai_allow_no_key,
            )
        except Exception as exc:
            print(f"[ai_domain_manager] openai classifier unavailable: {exc}", file=sys.stderr, flush=True)
            unresolved = remaining[start:]
            break
        classified_at = format_timestamp(utc_now())
        for domain in batch:
            result = results[domain]
            decisions["domains"][domain] = {
                "classification": result["classification"],
                "reason": result["reason"],
                "classified_at": classified_at,
                "source": "openai",
                "model": args.openai_model,
            }
        save_json(decisions_path, decisions)
    else:
        return []
    return unresolved


def _resolve_lock_path(args, attribute, default_name):
    configured = getattr(args, attribute, None)
    if isinstance(configured, (str, os.PathLike)) and str(configured).strip():
        return Path(configured)
    return Path(args.config_out).with_name(default_name)


def run_once(args):
    """Run one apply cycle while excluding every other manager process."""

    lock_path = _resolve_lock_path(args, "apply_lock_path", ".ai-domain-manager.lock")
    manual_mode_override = getattr(args, "manual_mode", None)
    has_manual_override = isinstance(manual_mode_override, str) and bool(manual_mode_override.strip())
    manual_lock_held = getattr(args, "manual_lock_held", False)
    if not isinstance(manual_lock_held, bool):
        manual_lock_held = False
    if has_manual_override:
        if manual_lock_held:
            # The panel holds the manual-operation gate while it waits for this
            # one-shot process and commits the resulting mode.  Acquiring only
            # the apply lock here avoids trying to lock the same gate twice.
            with exclusive_file_lock(lock_path):
                return _run_once_locked(args)
        # Direct CLI callers that provide an override still need the same gate
        # as scheduled runs; only the panel can explicitly declare ownership.
        manual_lock_path = _resolve_lock_path(args, "manual_lock_path", ".ai-domain-manager-manual.lock")
        with exclusive_file_lock(manual_lock_path), exclusive_file_lock(lock_path):
            return _run_once_locked(args)

    manual_lock_path = _resolve_lock_path(args, "manual_lock_path", ".ai-domain-manager-manual.lock")
    # Scheduled runs take the manual-operation gate first, then the shared
    # apply lock.  A panel request takes the same gate before starting its
    # one-shot child, so no scheduled run can observe an intermediate mode or
    # overwrite the config between apply and the panel's state commit.
    with exclusive_file_lock(manual_lock_path), exclusive_file_lock(lock_path):
        return _run_once_locked(args)


def _run_once_locked(args):
    now = utc_now()
    data_plane_controller = build_data_plane_controller(args)
    log_state = load_log_state(args.log_state_path)
    sync_log(
        args.log_path,
        log_state,
        data_plane_controller=data_plane_controller,
        lookback_seconds=args.lookback_seconds,
        now=now,
    )
    cutoff = purge_old_events(log_state, args.lookback_seconds, now)

    decisions = load_decisions(args.classification_state_path)
    observed_domains = {item["domain"] for item in log_state["events"]}
    sync_builtin_domain_decisions(decisions, args.classification_state_path, observed_domains)
    panel_target = read_panel_target(args.panel_db_path, args.panel_route_listen_port)
    manual_mode_override = getattr(args, "manual_mode", None)
    if not isinstance(manual_mode_override, str):
        manual_mode_override = ""
    manual_mode = manual_mode_override.strip().lower() or read_ai_routing_manual_mode(args.panel_db_path)
    if manual_mode == "forced_fallback":
        ai_target = {
            "probe_status": "manual_fallback",
            "failure_reason": "manual_override",
            "is_reachable": False,
            "candidates": [],
        }
    else:
        for candidate in args.ai_upstream_candidates:
            candidate["probe_server_name"] = resolve_probe_server_name(
                args.proxy_template_path,
                candidate,
                panel_target,
                getattr(args, "ai_upstream_probe_server_name", ""),
            )
        preferred_index = {"primary": 0, "backup": 1}.get(manual_mode)
        ai_target = select_ai_target(
            args.ai_upstream_candidates,
            args.ai_upstream_probe_timeout_seconds,
            probe_controller=data_plane_controller,
            preferred_index=preferred_index,
        )
    route_status = {"status": "disabled", "reason": ""}

    if manual_mode == "forced_fallback":
        # The emergency path must not wait on Codex/OpenAI classification.  A
        # direct config render and reload is safer than retaining the previous
        # AI route while a remote classifier is unavailable.
        pending_without_classifier = []
    else:
        pending_without_classifier = classify_pending_domains(
            decisions,
            args.classification_state_path,
            observed_domains,
            args,
        )

    ai_domains = sorted(
        domain
        for domain, item in decisions["domains"].items()
        if item.get("classification") == "ai"
    )

    proxy_payload = None
    if ai_domains:
        if manual_mode == "forced_fallback":
            args.dynamic_routing_path.unlink(missing_ok=True)
            route_status = {"status": "manual_fallback", "reason": "manual_override"}
        elif should_fallback_to_primary_route(ai_target) or str(ai_target.get("probe_status", "")).strip().lower() == "manual_unreachable":
            args.dynamic_routing_path.unlink(missing_ok=True)
            route_status = {
                "status": "manual_target_unreachable" if manual_mode in {"primary", "backup"} else "fallback_to_primary",
                "reason": "manual_ai_target_unreachable" if manual_mode in {"primary", "backup"} else "ai_upstream_unreachable",
            }
        elif str(ai_target.get("probe_status", "")).strip().lower() == "probe_error":
            args.dynamic_routing_path.unlink(missing_ok=True)
            route_status = {
                "status": "probe_error",
                "reason": ai_target.get("failure_reason", "ai_probe_management_failed"),
            }
        else:
            proxy_payload, proxy_error = render_proxy_template(args.proxy_template_path, ai_target, panel_target)
            if proxy_payload is None:
                args.dynamic_routing_path.unlink(missing_ok=True)
                route_status = {"status": "pending_proxy_template", "reason": proxy_error}
            else:
                applied = write_routing_fragment(args.dynamic_routing_path, ai_domains, proxy_payload)
                route_status = {
                    "status": "applied" if applied else "disabled",
                    "reason": proxy_error if applied else "no_ai_domains",
                }
    elif manual_mode == "forced_fallback":
        args.dynamic_routing_path.unlink(missing_ok=True)
        route_status = {"status": "manual_fallback", "reason": "manual_override"}
    else:
        args.dynamic_routing_path.unlink(missing_ok=True)
        route_status = {"status": "idle", "reason": "no_ai_domains"}

    previous_config = args.config_out.read_text(encoding="utf-8") if args.config_out.is_file() else ""
    pending_apply_path = args.config_out.with_name(args.config_out.name + ".pending-apply")
    pending_apply = pending_apply_path.exists()
    pending_apply_path.parent.mkdir(parents=True, exist_ok=True)
    # Persist before rendering/syncing: either can update files before failing.
    # A later invocation must retry even if the generated files no longer differ.
    pending_apply_path.touch()
    rerender_config(
        args.render_script,
        args.env_file,
        args.config_out,
        args.client_out,
        args.share_out,
        args.dynamic_routing_path,
    )
    current_config = args.config_out.read_text(encoding="utf-8") if args.config_out.is_file() else ""
    config_changed = current_config != previous_config
    config_retried = pending_apply
    remote_config_changed = False
    config_apply_status = "not_needed"
    apply_needed = config_changed or pending_apply
    if data_plane_controller.is_configured():
        # Sync on every management cycle. The rendered config can be unchanged
        # while the remote source fragment was deleted or replaced manually;
        # keeping both artifacts synchronized makes the next panel render safe.
        synced_paths = []
        if data_plane_controller.supports_sync():
            synced_paths = data_plane_controller.sync_generated_files(validate_config=True)
            remote_config_path = str(getattr(args, "data_plane_config_path", "") or "").strip()
            remote_config_changed = bool(remote_config_path and remote_config_path in {str(path) for path in synced_paths})
        apply_needed = config_changed or remote_config_changed or pending_apply
        external_reloader_enabled = getattr(args, "data_plane_external_reloader_enabled", False)
        if not isinstance(external_reloader_enabled, bool):
            external_reloader_enabled = False
        if apply_needed and external_reloader_enabled:
            # Explicit external-reloader mode (for example Kubernetes' xray-
            # reloader sidecar) owns the process restart.  The rendered files
            # are delegated to that watcher instead of requiring a direct
            # restart command from the manager.
            config_apply_status = "delegated"
        elif apply_needed and (
            not data_plane_controller.supports_restart() or not data_plane_controller.restart()
        ):
            raise RuntimeError("AI 路由配置重载失败，保留待应用状态以便重试。")
        elif apply_needed:
            config_apply_status = "direct"
        else:
            config_apply_status = "unchanged"
        pending_apply_path.unlink(missing_ok=True)
    elif (config_changed or pending_apply) and args.restart_command:
        restart_xray_command(args.restart_command, args.docker_timeout_seconds)
        config_apply_status = "direct"
        pending_apply_path.unlink(missing_ok=True)
    elif (config_changed or pending_apply) and args.restart_container_name:
        restart_xray_container(args.restart_container_name, args.docker_timeout_seconds)
        config_apply_status = "direct"
        pending_apply_path.unlink(missing_ok=True)
    elif apply_needed:
        config_apply_status = "unmanaged"
        if manual_mode_override.strip():
            raise RuntimeError("AI 路由配置重载未配置，保留待应用状态以便重试。")

    # Once the config has been synchronized and the reload has succeeded (or
    # been delegated to an external watcher), reporting is bookkeeping.  A
    # report/database write failure must not make the panel roll back a mode
    # that is already active on the data plane.
    try:
        report = build_domain_report(log_state, cutoff, now, decisions, ai_target, panel_target, route_status)
        if pending_without_classifier:
            report["route_status"]["pending_domains_without_classifier"] = pending_without_classifier
        report["route_status"]["config_changed"] = config_changed or remote_config_changed
        report["route_status"]["config_retried"] = config_retried
        report["route_status"]["config_apply_status"] = config_apply_status
        report["panel_db_status"] = save_ai_domains_to_panel_db(args.panel_db_path, report, decisions)
        write_domain_report(args.report_output_dir, report)
        save_log_state(args.log_state_path, log_state)
        save_json(args.classification_state_path, decisions)
        print(
            "[ai_domain_manager] "
            f"domains={report['unique_domains']} ai_domains={len(report['ai_domains'])} "
            f"route_status={report['route_status']['status']}",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001 - reporting must not undo a successful config apply
        print(
            "[ai_domain_manager] config applied but report persistence failed: "
            f"{exc}",
            file=sys.stderr,
            flush=True,
        )
        return {
            "status": "applied_with_reporting_error",
            "config_apply_status": config_apply_status,
            "config_changed": config_changed or remote_config_changed,
            "config_retried": config_retried,
        }
    return {
        "status": "applied",
        "config_apply_status": config_apply_status,
        "config_changed": config_changed or remote_config_changed,
        "config_retried": config_retried,
    }


def seconds_until_next_boundary(interval_seconds):
    now = time.time()
    next_boundary = ((int(now) // interval_seconds) + 1) * interval_seconds
    return max(1, next_boundary - now)


def build_args():
    parser = argparse.ArgumentParser(description="Classify Xray destination domains and maintain dynamic AI routing.")
    parser.add_argument("--workspace-dir", default=os.environ.get("XRAY_WORKSPACE_DIR", str(BASE_DIR)))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=env_int("AI_DOMAIN_INTERVAL_SECONDS", 3600))
    parser.add_argument("--lookback-seconds", type=int, default=env_int("AI_DOMAIN_LOOKBACK_SECONDS", 3600))
    parser.add_argument("--batch-size", type=int, default=env_int("AI_DOMAIN_BATCH_SIZE", 50))
    parser.add_argument("--openai-timeout-seconds", type=int, default=env_int("OPENAI_TIMEOUT_SECONDS", 45))
    parser.add_argument("--codex-timeout-seconds", type=int, default=env_int("CODEX_TIMEOUT_SECONDS", 180))
    parser.add_argument("--docker-timeout-seconds", type=int, default=env_int("DOCKER_TIMEOUT_SECONDS", 30))
    parser.add_argument("--panel-route-listen-port", type=int, default=env_int("PANEL_ROUTE_LISTEN_PORT", 0))
    parser.add_argument(
        "--manual-mode",
        choices=("auto", "primary", "backup", "forced_fallback"),
        default="",
        help="Apply this mode for a one-shot request without reading the panel state.",
    )
    parser.add_argument("--manual-lock-held", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    workspace = Path(args.workspace_dir)
    args.log_path = Path(os.environ.get("XRAY_ACCESS_LOG_PATH", str(workspace / "logs" / "access.log")))
    args.report_output_dir = Path(os.environ.get("AI_DOMAIN_REPORT_OUTPUT_DIR", str(workspace / "reports" / "hourly-domains")))
    args.log_state_path = Path(os.environ.get("AI_DOMAIN_LOG_STATE_PATH", str(args.report_output_dir / ".state.json")))
    args.classification_state_path = Path(
        os.environ.get("AI_DOMAIN_CLASSIFICATION_STATE_PATH", str(workspace / "runtime" / "ai-domain-decisions.json"))
    )
    args.dynamic_routing_path = Path(
        os.environ.get("AI_DOMAIN_DYNAMIC_ROUTING_PATH", str(workspace / "runtime" / "dynamic-routing.json"))
    )
    args.env_file = Path(os.environ.get("XRAY_ENV_FILE", str(workspace / ".env")))
    args.render_script = (
        os.environ.get("XRAY_RENDER_MODULE", "").strip()
        or os.environ.get("XRAY_RENDER_SCRIPT", "").strip()
        or DEFAULT_RENDER_MODULE
    )
    args.config_out = Path(os.environ.get("XRAY_CONFIG_OUT", str(workspace / "runtime" / "config.json")))
    args.apply_lock_path = Path(
        os.environ.get(
            "AI_DOMAIN_MANAGER_LOCK_PATH",
            str(args.config_out.with_name(".ai-domain-manager.lock")),
        )
    )
    args.manual_lock_path = Path(
        os.environ.get(
            "AI_DOMAIN_MANAGER_MANUAL_LOCK_PATH",
            str(args.config_out.with_name(".ai-domain-manager-manual.lock")),
        )
    )
    args.client_out = Path(os.environ.get("XRAY_CLIENT_OUT", str(workspace / "runtime" / "client-test.json")))
    args.share_out = Path(os.environ.get("XRAY_SHARE_OUT", str(workspace / "runtime" / "client-share.txt")))
    args.panel_db_path = Path(os.environ.get("PANEL_DB_PATH", "/panel-data/panel.db"))
    args.proxy_template_path = Path(
        os.environ.get("AI_PROXY_OUTBOUND_TEMPLATE_PATH", str(workspace / "ai-proxy-outbound.json"))
    )
    env_file_values = load_env_file_values(args.env_file)
    args.restart_container_name = os.environ.get("DATAPLANE_RESTART_CONTAINER", "").strip()
    args.restart_command = os.environ.get("DATAPLANE_RESTART_COMMAND", "").strip()
    args.data_plane_ssh_target = os.environ.get(
        "DATAPLANE_SSH_TARGET", ""
    ).strip()
    args.data_plane_ssh_bin = os.environ.get("DATAPLANE_SSH_BIN", "ssh").strip() or "ssh"
    args.data_plane_ssh_options = tuple(
        shlex.split(os.environ.get("DATAPLANE_SSH_OPTIONS", "").strip())
    ) if os.environ.get("DATAPLANE_SSH_OPTIONS", "").strip() else ()
    args.data_plane_ssh_known_hosts_file = os.environ.get(
        "DATAPLANE_SSH_KNOWN_HOSTS", "/root/.ssh/known_hosts"
    ).strip()
    args.data_plane_api_server = os.environ.get("DATAPLANE_API_SERVER", "127.0.0.1:10085").strip() or "127.0.0.1:10085"
    args.data_plane_xray_bin = os.environ.get("DATAPLANE_XRAY_BIN", "/usr/local/bin/xray").strip() or "/usr/local/bin/xray"
    args.data_plane_local_bin = os.environ.get("DATAPLANE_LOCAL_BIN", "").strip()
    args.data_plane_container_name = os.environ.get("DATAPLANE_CONTAINER_NAME", "").strip()
    args.data_plane_docker_bin = os.environ.get("DATAPLANE_DOCKER_BIN", "docker").strip() or "docker"
    args.data_plane_restart_command = os.environ.get("DATAPLANE_RESTART_COMMAND", "").strip()
    args.data_plane_config_path = os.environ.get("DATAPLANE_CONFIG_PATH", "").strip()
    args.data_plane_dynamic_routing_path = os.environ.get("DATAPLANE_DYNAMIC_ROUTING_PATH", "").strip()
    args.data_plane_access_log_path = os.environ.get("DATAPLANE_ACCESS_LOG_PATH", "").strip()
    args.data_plane_external_reloader_enabled = env_bool("DATAPLANE_EXTERNAL_RELOADER_ENABLED", "0")
    args.codex_classifier_enabled = env_bool("CODEX_CLASSIFIER_ENABLED", "1")
    args.codex_source_home = Path(os.environ.get("CODEX_SOURCE_HOME", "/host-codex-home"))
    args.codex_runtime_home = Path(
        os.environ.get("CODEX_RUNTIME_HOME", str(workspace / "runtime" / "codex-home"))
    )
    args.codex_workdir = Path(os.environ.get("CODEX_WORKDIR", "/tmp/codex-domain-classifier"))
    args.codex_bin = os.environ.get("CODEX_BIN", "codex").strip() or "codex"
    args.codex_model = os.environ.get("CODEX_MODEL", "").strip()
    args.openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    args.openai_model = os.environ.get("OPENAI_MODEL", "gpt-5.5").strip() or "gpt-5.5"
    args.openai_base_url = normalize_openai_base_url(
        os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1/responses")
    )
    args.openai_allow_no_key = env_bool(
        "OPENAI_ALLOW_NO_KEY",
        "1" if is_local_openai_base_url(args.openai_base_url) else "0",
    )
    args.openai_classifier_enabled = bool(args.openai_api_key) or args.openai_allow_no_key
    args.ai_upstream_host = read_env_or_file("AI_UPSTREAM_HOST", "upstream.example.com", env_file_values)
    args.ai_upstream_port = int(read_env_or_file("AI_UPSTREAM_PORT", "27166", env_file_values))
    args.ai_upstreams = read_env_or_file("AI_UPSTREAMS", "", env_file_values)
    args.ai_upstream_fallbacks = read_env_or_file("AI_UPSTREAM_FALLBACKS", "", env_file_values)
    args.ai_upstream_fallback_url = read_env_or_file("AI_UPSTREAM_FALLBACK_URL", "", env_file_values)
    args.data_plane_remote_command_timeout = parse_positive_float(
        os.environ.get("DATAPLANE_REMOTE_COMMAND_TIMEOUT", "8"),
        "DATAPLANE_REMOTE_COMMAND_TIMEOUT",
    )
    args.ai_upstream_probe_timeout_seconds = parse_positive_float(
        read_env_or_file("AI_UPSTREAM_PROBE_TIMEOUT_SECONDS", "3", env_file_values),
        "AI_UPSTREAM_PROBE_TIMEOUT_SECONDS",
    )
    args.ai_upstream_probe_server_name = read_env_or_file(
        "AI_UPSTREAM_PROBE_SERVER_NAME",
        "",
        env_file_values,
    )
    args.ai_upstream_candidates = build_ai_upstream_candidates(
        args.ai_upstream_host,
        args.ai_upstream_port,
        upstreams_raw=args.ai_upstreams,
        fallbacks_raw=args.ai_upstream_fallbacks,
        fallback_share_url=args.ai_upstream_fallback_url,
    )
    return args


def main():
    args = build_args()

    if args.interval_seconds <= 0:
        print("AI_DOMAIN_INTERVAL_SECONDS must be > 0", file=sys.stderr)
        return 1
    if args.lookback_seconds <= 0:
        print("AI_DOMAIN_LOOKBACK_SECONDS must be > 0", file=sys.stderr)
        return 1
    if args.batch_size <= 0:
        print("AI_DOMAIN_BATCH_SIZE must be > 0", file=sys.stderr)
        return 1
    if not args.ai_upstream_candidates:
        print("at least one AI upstream must be configured", file=sys.stderr)
        return 1

    while True:
        try:
            run_once(args)
        except LockBusyError as exc:
            print(f"[ai_domain_manager] busy: {exc}", file=sys.stderr, flush=True)
            if args.once:
                return LOCK_BUSY_EXIT_CODE
        except Exception as exc:
            print(f"[ai_domain_manager] error: {exc}", file=sys.stderr, flush=True)
            if args.once:
                return 1
        else:
            if args.once:
                return 0
        time.sleep(seconds_until_next_boundary(args.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
