"""Classify observed domains using built-in rules and optional AI providers."""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .common import format_timestamp, load_json, save_json, utc_now

FORCED_AI_ROUTE_DOMAIN_SUFFIXES = (
    "accounts.google.com",
    "gemini.google.com",
    "gemini.gstatic.com",
    "generativelanguage.googleapis.com",
    "scholar.google.com",
    "amazonaws.com",
    "amazonaws.com.cn",
    "amazonaws.cn",
    "amazonwebservices.com.cn",
    "api.aws",
    "on.aws",
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
    "chatgpt.com",
    "openai.com",
    "oaistatic.com",
    "oaiusercontent.com",
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


def load_decisions(path):
    payload = load_json(path, {"domains": {}})
    domains = payload.get("domains", {})
    if not isinstance(domains, dict):
        domains = {}
    return {"domains": domains}


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
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"} or host.endswith(".localhost"):
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
    if path.endswith(("/chat/completions", "/responses")):
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
            "return_format": [{"domain": "example.com", "classification": "ai|not_ai", "reason": "short reason"}],
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
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
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
        raise RuntimeError("classifier output must be a JSON list")  # noqa: TRY004 - provider protocol failure

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
            "return_format": [{"domain": "example.com", "classification": "ai|not_ai", "reason": "short reason"}],
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
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
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
            batch = remaining[start : start + args.batch_size]
            try:
                results = classify_domains_via_codex(batch, args)
            except Exception as exc:  # noqa: BLE001 - optional classifier failure falls back to unresolved
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
        batch = remaining[start : start + args.batch_size]
        try:
            results = classify_domains_via_openai(
                batch,
                args.openai_api_key,
                args.openai_model,
                args.openai_base_url,
                args.openai_timeout_seconds,
                allow_no_key=args.openai_allow_no_key,
            )
        except Exception as exc:  # noqa: BLE001 - optional classifier failure falls back to unresolved
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


__all__ = [
    "FORCED_AI_ROUTE_DOMAIN_SUFFIXES",
    "KNOWN_AI_DOMAIN_SUFFIXES",
    "classify_domains_via_codex",
    "classify_domains_via_openai",
    "classify_pending_domains",
    "extract_chat_completions_text",
    "load_decisions",
    "matches_forced_ai_route_domain",
    "matches_known_ai_domain",
    "normalize_classification",
    "normalize_openai_base_url",
    "resolve_openai_endpoint",
    "sync_builtin_domain_decisions",
]
