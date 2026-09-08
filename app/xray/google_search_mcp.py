#!/usr/bin/env python3
import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from app.xray.ai_routing.classifier import extract_chat_completions_text
from app.xray.ai_routing.classifier import load_decisions
from app.xray.ai_routing.classifier import normalize_classification
from app.xray.ai_routing.common import DOMAIN_RE
from app.xray.ai_routing.common import env_int
from app.xray.ai_routing.common import format_timestamp
from app.xray.ai_routing.common import load_json
from app.xray.ai_routing.common import save_json
from app.xray.ai_routing.common import utc_now
from app.xray.config import HOURLY_REPORTS_DIR
from app.xray.config import RUNTIME_DIR
from app.xray.envfile import load_env_file as load_env_file_values


DEFAULT_REPORT_PATH = HOURLY_REPORTS_DIR / "latest.json"
DEFAULT_DECISIONS_PATH = RUNTIME_DIR / "ai-domain-decisions.json"
ROOT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
XRAY_ENV_PATH = Path(__file__).resolve().parent / ".env"
MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "google-openrouter-ai-domain-classifier"
SERVER_VERSION = "0.1.0"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_OPENROUTER_MODEL = "openai/gpt-5-nano"
DEFAULT_GOOGLE_SEARCH_URL = "https://www.google.com/search"
DEFAULT_GOOGLE_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
GOOGLE_RESULT_ANCHOR_RE = re.compile(r'<a\b[^>]*\bhref="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
GOOGLE_SNIPPET_RE = re.compile(
    r'<(?:div|span)\b[^>]*\bclass="[^"]*\b(?:VwiC3b|yXK7lf|MUxGbd|aCOpRe|s3v9rd)\b[^"]*"[^>]*>(.*?)</(?:div|span)>',
    re.IGNORECASE | re.DOTALL,
)
HTML_TAG_RE = re.compile(r"<[^>]+>")


def parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def load_default_env_files():
    for path in (ROOT_ENV_PATH, XRAY_ENV_PATH):
        if not path.is_file():
            continue
        values = load_env_file_values(path)
        for key, value in values.items():
            os.environ.setdefault(key, value)


def coerce_int(value, default, minimum=None, maximum=None, field_name="value"):
    if value is None:
        value = default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{field_name} must be <= {maximum}")
    return parsed


def resolve_report_path(path):
    return Path(path).expanduser() if path else DEFAULT_REPORT_PATH


def resolve_decisions_path(path):
    return Path(path).expanduser() if path else DEFAULT_DECISIONS_PATH


def normalize_domain_input(domains):
    if domains is None:
        return []
    if not isinstance(domains, list):
        raise ValueError("domains must be an array of domain strings")

    normalized = []
    seen = set()
    for item in domains:
        domain = str(item or "").strip().lower()
        if not domain:
            continue
        if not DOMAIN_RE.fullmatch(domain):
            raise ValueError(f"invalid domain: {domain}")
        if domain in seen:
            continue
        seen.add(domain)
        normalized.append(domain)
    return normalized


def load_report(report_path):
    payload = load_json(report_path, {})
    domains = payload.get("domains", [])
    if not isinstance(domains, list):
        domains = []
    payload["domains"] = domains
    return payload


def collect_uncategorized_domain_items(report, decisions, min_hits=1, include_unknown=True, limit=None):
    domains = []
    for item in report.get("domains", []):
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain", "")).strip().lower()
        if not domain or not DOMAIN_RE.fullmatch(domain):
            continue

        try:
            hits = int(item.get("hits", 0))
        except (TypeError, ValueError):
            hits = 0
        if hits < min_hits:
            continue

        decision = decisions["domains"].get(domain)
        decision_classification = normalize_classification((decision or {}).get("classification", ""))
        if decision is not None and decision_classification != "unknown":
            continue
        if decision is not None and not include_unknown:
            continue

        domains.append(
            {
                "domain": domain,
                "hits": hits,
                "protocols": sorted(str(value) for value in item.get("protocols", []) if str(value).strip()),
                "report_classification": normalize_classification(item.get("classification", "")),
                "existing_classification": decision_classification if decision is not None else "missing",
                "reason": str((decision or {}).get("reason", "") or item.get("reason", "")).strip(),
            }
        )

    domains.sort(key=lambda entry: (-entry["hits"], entry["domain"]))
    if limit is not None:
        return domains[:limit]
    return domains


def build_google_query(domain, query_template):
    template = str(query_template or '"{domain}"').strip() or '"{domain}"'
    if "{domain}" not in template:
        raise ValueError("google search query template must contain {domain}")
    return template.format(domain=domain)


def normalize_html_text(raw):
    return " ".join(html.unescape(HTML_TAG_RE.sub(" ", raw or "")).split())


def extract_google_result_link(href):
    raw_href = html.unescape(str(href or "").strip())
    if not raw_href:
        return ""
    if raw_href.startswith("/url?"):
        query = urllib.parse.urlsplit(raw_href).query
        params = urllib.parse.parse_qs(query)
        for key in ("q", "url"):
            target = params.get(key, [""])[0].strip()
            if target:
                return target
        return ""
    if raw_href.startswith("http://") or raw_href.startswith("https://"):
        return raw_href
    return ""


def is_google_internal_link(link):
    try:
        host = (urllib.parse.urlparse(link).hostname or "").lower()
    except ValueError:
        return True
    return not host or host.endswith("google.com") or host.endswith("googleusercontent.com")


def parse_google_search_html(domain, query, raw_html, limit):
    results = []
    seen_links = set()
    for match in GOOGLE_RESULT_ANCHOR_RE.finditer(raw_html):
        link = extract_google_result_link(match.group(1))
        if not link or link in seen_links or is_google_internal_link(link):
            continue
        title = normalize_html_text(match.group(2))
        if not title:
            continue

        snippet = ""
        nearby_html = raw_html[match.end():match.end() + 2500]
        snippet_match = GOOGLE_SNIPPET_RE.search(nearby_html)
        if snippet_match:
            snippet = normalize_html_text(snippet_match.group(1))

        display_link = (urllib.parse.urlparse(link).hostname or "").lower()
        seen_links.add(link)
        results.append(
            {
                "rank": len(results) + 1,
                "title": title,
                "link": link,
                "display_link": display_link,
                "snippet": snippet,
            }
        )
        if len(results) >= limit:
            break

    return {
        "domain": domain,
        "query": query,
        "provider": "google_html",
        "total_results": "",
        "search_time_seconds": None,
        "results": results,
    }

def build_openrouter_classification_payload(domain, search_payload, model):
    system_prompt = (
        "You classify internet domains using search evidence. "
        "Return JSON only. "
        "Classify as 'ai' only when the domain is clearly an AI product, AI model provider, "
        "AI coding tool, AI chat product, AI inference platform, or AI-focused developer platform. "
        "Classify as 'not_ai' for general SaaS, infra, media, ecommerce, corporate, CDN, or unrelated sites. "
        "Classify as 'unknown' when evidence is sparse or conflicting."
    )
    user_prompt = json.dumps(
        {
            "task": "classify_domain_from_google_search_results",
            "domain": domain,
            "instructions": [
                "Use the Google search results below as the main evidence.",
                "Prioritize official homepage, docs, pricing, or product pages for the exact domain.",
                "Do not classify as ai only because third-party articles mention AI in passing.",
                "Keep the reason short.",
                "Return valid JSON only.",
            ],
            "search_query": search_payload["query"],
            "search_results": search_payload["results"],
            "return_format": {
                "domain": "example.com",
                "classification": "ai|not_ai|unknown",
                "reason": "short reason",
                "evidence": [
                    {
                        "rank": 1,
                        "signal": "short explanation of what the result proves",
                    }
                ],
            },
        },
        ensure_ascii=True,
    )
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }


def validate_domain_classification_result(domain, parsed):
    if not isinstance(parsed, dict):
        raise RuntimeError("classifier output must be a JSON object")

    result_domain = str(parsed.get("domain", "")).strip().lower()
    if result_domain != domain:
        raise RuntimeError(f"classifier output domain mismatch: expected {domain}, got {result_domain or '<empty>'}")

    classification = normalize_classification(parsed.get("classification", ""))
    reason = str(parsed.get("reason", "")).strip()
    evidence = []
    for item in parsed.get("evidence", []):
        if not isinstance(item, dict):
            continue
        try:
            rank = int(item.get("rank", 0))
        except (TypeError, ValueError):
            continue
        signal = str(item.get("signal", "")).strip()
        if rank > 0 and signal:
            evidence.append({"rank": rank, "signal": signal})

    return {
        "domain": domain,
        "classification": classification,
        "reason": reason,
        "evidence": evidence[:5],
    }


def classify_domain_with_google_results_via_openrouter(domain, search_payload, args):
    if not args.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not configured")

    payload = build_openrouter_classification_payload(domain, search_payload, args.openrouter_model)
    request = urllib.request.Request(
        args.openrouter_base_url,
        data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
        method="POST",
        headers=build_openrouter_headers(args),
    )
    try:
        with urllib.request.urlopen(request, timeout=args.openrouter_timeout_seconds) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"openrouter http {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"openrouter request failed: {exc}") from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"openrouter response was not valid JSON: {raw}") from exc
    text = extract_chat_completions_text(parsed)
    if not text:
        raise RuntimeError("openrouter response did not contain assistant text")
    try:
        classified = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"openrouter assistant output was not valid JSON: {text}") from exc
    return validate_domain_classification_result(domain, classified)


def build_openrouter_headers(args):
    headers = {
        "Authorization": f"Bearer {args.openrouter_api_key}",
        "Content-Type": "application/json",
    }
    if args.openrouter_referer:
        headers["HTTP-Referer"] = args.openrouter_referer
    if args.openrouter_title:
        headers["X-OpenRouter-Title"] = args.openrouter_title
    return headers


class GoogleOpenRouterDomainClassifierService:
    def __init__(self, args):
        self.args = args

    def collect_uncategorized_domains(
        self,
        reportPath=None,
        classificationStatePath=None,
        limit=None,
        minHits=None,
        includeUnknown=None,
    ):
        report_path = resolve_report_path(reportPath or self.args.report_path)
        decisions_path = resolve_decisions_path(classificationStatePath or self.args.classification_state_path)
        min_hits = coerce_int(minHits, self.args.default_min_hits, minimum=1, field_name="minHits")
        include_unknown = parse_bool(includeUnknown, default=True)
        normalized_limit = None
        if limit is not None:
            normalized_limit = coerce_int(limit, limit, minimum=1, field_name="limit")

        report = load_report(report_path)
        decisions = load_decisions(decisions_path)
        domains = collect_uncategorized_domain_items(
            report,
            decisions,
            min_hits=min_hits,
            include_unknown=include_unknown,
            limit=normalized_limit,
        )
        return {
            "report_path": str(report_path),
            "classification_state_path": str(decisions_path),
            "domain_count": len(domains),
            "domains": domains,
        }

    def google_search_domain(self, domain, num_results=None, gl=None, hl=None, safe=None):
        normalized_num_results = coerce_int(
            num_results,
            self.args.google_num_results,
            minimum=1,
            maximum=10,
            field_name="numResults",
        )
        query = build_google_query(domain, self.args.google_query_template)
        params = {
            "q": query,
            "num": normalized_num_results,
            "pws": "0",
            "filter": "0",
        }
        if gl or self.args.google_gl:
            params["gl"] = str(gl or self.args.google_gl).strip()
        if hl or self.args.google_hl:
            params["hl"] = str(hl or self.args.google_hl).strip()
        if safe or self.args.google_safe:
            params["safe"] = str(safe or self.args.google_safe).strip()

        url = self.args.google_search_url + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": self.args.google_accept_language,
                "User-Agent": self.args.google_user_agent,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.args.google_timeout_seconds) as response:
                payload = response.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"google search http {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"google search request failed: {exc}") from exc

        return parse_google_search_html(domain, query, payload, normalized_num_results)

    def search_domains_with_google(self, domains, numResults=None, gl=None, hl=None, safe=None):
        normalized_domains = normalize_domain_input(domains)
        if not normalized_domains:
            raise ValueError("domains must contain at least one domain")

        results = [
            self.google_search_domain(
                domain,
                num_results=numResults,
                gl=gl,
                hl=hl,
                safe=safe,
            )
            for domain in normalized_domains
        ]
        return {
            "domain_count": len(results),
            "results": results,
        }

    def classify_domains_with_google(
        self,
        domains=None,
        reportPath=None,
        classificationStatePath=None,
        limit=None,
        minHits=None,
        includeUnknown=None,
        numResults=None,
        gl=None,
        hl=None,
        safe=None,
        writeBack=None,
    ):
        normalized_domains = normalize_domain_input(domains)
        report_path = resolve_report_path(reportPath or self.args.report_path)
        decisions_path = resolve_decisions_path(classificationStatePath or self.args.classification_state_path)
        min_hits = coerce_int(minHits, self.args.default_min_hits, minimum=1, field_name="minHits")
        include_unknown = parse_bool(includeUnknown, default=True)
        write_back = parse_bool(writeBack, default=False)
        normalized_limit = None
        if limit is not None:
            normalized_limit = coerce_int(limit, limit, minimum=1, field_name="limit")

        if not normalized_domains:
            collected = self.collect_uncategorized_domains(
                reportPath=str(report_path),
                classificationStatePath=str(decisions_path),
                limit=normalized_limit,
                minHits=min_hits,
                includeUnknown=include_unknown,
            )
            normalized_domains = [item["domain"] for item in collected["domains"]]
        else:
            collected = None

        results = []
        decisions = load_decisions(decisions_path) if write_back else None
        updated_count = 0
        for domain in normalized_domains:
            try:
                search_payload = self.google_search_domain(
                    domain,
                    num_results=numResults,
                    gl=gl,
                    hl=hl,
                    safe=safe,
                )
                if search_payload["results"]:
                    classification = classify_domain_with_google_results_via_openrouter(domain, search_payload, self.args)
                else:
                    classification = {
                        "domain": domain,
                        "classification": "unknown",
                        "reason": "no_search_results",
                        "evidence": [],
                    }
                record = {
                    "domain": domain,
                    "classification": classification["classification"],
                    "reason": classification["reason"],
                    "evidence": classification["evidence"],
                    "search_query": search_payload["query"],
                    "search_result_count": len(search_payload["results"]),
                    "search_results": search_payload["results"],
                    "total_results": search_payload["total_results"],
                }
                if write_back:
                    decisions["domains"][domain] = {
                        "classification": record["classification"],
                        "reason": record["reason"],
                        "classified_at": format_timestamp(utc_now()),
                        "source": "google_openrouter_mcp",
                        "model": self.args.openrouter_model,
                    }
                    updated_count += 1
                results.append(record)
            except Exception as exc:
                results.append(
                    {
                        "domain": domain,
                        "classification": "unknown",
                        "reason": "",
                        "error": str(exc),
                    }
                )

        if write_back and decisions is not None:
            save_json(decisions_path, decisions)

        return {
            "report_path": str(report_path),
            "classification_state_path": str(decisions_path),
            "requested_domain_count": len(normalized_domains),
            "updated_count": updated_count,
            "collected_from_report": collected is not None,
            "results": results,
        }

    def call_tool(self, name, arguments):
        if name == "collect_uncategorized_domains":
            return self.collect_uncategorized_domains(**arguments)
        if name == "search_domains_with_google":
            return self.search_domains_with_google(**arguments)
        if name == "classify_domains_with_google":
            return self.classify_domains_with_google(**arguments)
        raise ValueError(f"unknown tool: {name}")


class StdioJsonRpcTransport:
    def __init__(self, input_stream, output_stream):
        self.input_stream = input_stream
        self.output_stream = output_stream

    def read_message(self):
        headers = {}
        while True:
            line = self.input_stream.readline()
            if not line:
                return None
            if line in {b"\r\n", b"\n"}:
                break
            decoded = line.decode("utf-8")
            if ":" not in decoded:
                continue
            key, value = decoded.split(":", 1)
            headers[key.strip().lower()] = value.strip()

        content_length = headers.get("content-length")
        if not content_length:
            return None
        body = self.input_stream.read(int(content_length))
        if not body:
            return None
        return json.loads(body.decode("utf-8"))

    def write_message(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = (
            f"Content-Length: {len(body)}\r\n"
            "Content-Type: application/json\r\n"
            "\r\n"
        ).encode("ascii")
        self.output_stream.write(header)
        self.output_stream.write(body)
        self.output_stream.flush()


class GoogleOpenRouterDomainClassifierMcpServer:
    TOOLS = [
        {
            "name": "collect_uncategorized_domains",
            "description": "Read the latest domain report and return domains that are still missing a final AI classification.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "reportPath": {"type": "string"},
                    "classificationStatePath": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1},
                    "minHits": {"type": "integer", "minimum": 1},
                    "includeUnknown": {"type": "boolean"},
                },
            },
        },
        {
            "name": "search_domains_with_google",
            "description": "Search domains with the Google results page and return normalized titles, links, and snippets.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "domains": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "numResults": {"type": "integer", "minimum": 1, "maximum": 10},
                    "gl": {"type": "string"},
                    "hl": {"type": "string"},
                    "safe": {"type": "string"},
                },
                "required": ["domains"],
            },
        },
        {
            "name": "classify_domains_with_google",
            "description": "Collect uncategorized domains, search each domain with Google, and ask OpenRouter to decide ai/not_ai/unknown.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "domains": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "reportPath": {"type": "string"},
                    "classificationStatePath": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1},
                    "minHits": {"type": "integer", "minimum": 1},
                    "includeUnknown": {"type": "boolean"},
                    "numResults": {"type": "integer", "minimum": 1, "maximum": 10},
                    "gl": {"type": "string"},
                    "hl": {"type": "string"},
                    "safe": {"type": "string"},
                    "writeBack": {"type": "boolean"},
                },
            },
        },
    ]

    def __init__(self, service, transport, error_stream):
        self.service = service
        self.transport = transport
        self.error_stream = error_stream

    def run(self):
        while True:
            request = self.transport.read_message()
            if request is None:
                return 0

            response = self.handle_request(request)
            if response is not None:
                self.transport.write_message(response)

    def handle_request(self, request):
        method = request.get("method")
        request_id = request.get("id")
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION,
                    },
                },
            }
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": self.TOOLS}}
        if method == "tools/call":
            params = request.get("params", {})
            tool_name = str(params.get("name", "")).strip()
            arguments = params.get("arguments") or {}
            try:
                payload = self.service.call_tool(tool_name, arguments)
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(payload, ensure_ascii=False, indent=2),
                        }
                    ]
                }
            except Exception as exc:
                print(f"[{SERVER_NAME}] tool error for {tool_name}: {exc}", file=self.error_stream, flush=True)
                result = {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                }
            return {"jsonrpc": "2.0", "id": request_id, "result": result}

        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32601,
                "message": f"method not found: {method}",
            },
        }


def parse_args(argv=None):
    load_default_env_files()
    parser = argparse.ArgumentParser(description="MCP server for Google search + OpenRouter AI domain classification")
    parser.add_argument(
        "--report-path",
        default=os.environ.get("GOOGLE_DOMAIN_MCP_REPORT_PATH", str(DEFAULT_REPORT_PATH)),
    )
    parser.add_argument(
        "--classification-state-path",
        default=os.environ.get("GOOGLE_DOMAIN_MCP_CLASSIFICATION_STATE_PATH", str(DEFAULT_DECISIONS_PATH)),
    )
    parser.add_argument(
        "--default-min-hits",
        type=int,
        default=env_int("GOOGLE_DOMAIN_MCP_MIN_HITS", 1),
    )
    parser.add_argument(
        "--google-search-url",
        default=os.environ.get("GOOGLE_SEARCH_URL", DEFAULT_GOOGLE_SEARCH_URL).strip() or DEFAULT_GOOGLE_SEARCH_URL,
    )
    parser.add_argument(
        "--google-query-template",
        default=os.environ.get("GOOGLE_SEARCH_QUERY_TEMPLATE", '"{domain}"'),
    )
    parser.add_argument(
        "--google-user-agent",
        default=os.environ.get("GOOGLE_SEARCH_USER_AGENT", DEFAULT_GOOGLE_USER_AGENT).strip() or DEFAULT_GOOGLE_USER_AGENT,
    )
    parser.add_argument(
        "--google-timeout-seconds",
        type=int,
        default=env_int("GOOGLE_SEARCH_TIMEOUT_SECONDS", 20),
    )
    parser.add_argument(
        "--google-num-results",
        type=int,
        default=env_int("GOOGLE_SEARCH_NUM_RESULTS", 5),
    )
    parser.add_argument(
        "--google-gl",
        default=os.environ.get("GOOGLE_SEARCH_GL", "").strip(),
    )
    parser.add_argument(
        "--google-hl",
        default=os.environ.get("GOOGLE_SEARCH_HL", "").strip(),
    )
    parser.add_argument(
        "--google-accept-language",
        default=os.environ.get("GOOGLE_SEARCH_ACCEPT_LANGUAGE", "en-US,en;q=0.9").strip() or "en-US,en;q=0.9",
    )
    parser.add_argument(
        "--google-safe",
        default=os.environ.get("GOOGLE_SEARCH_SAFE", "off").strip() or "off",
    )
    parser.add_argument(
        "--openrouter-api-key",
        default=os.environ.get("OPENROUTER_API_KEY", "").strip(),
    )
    parser.add_argument(
        "--openrouter-base-url",
        default=os.environ.get("OPENROUTER_BASE_URL", DEFAULT_OPENROUTER_BASE_URL).strip() or DEFAULT_OPENROUTER_BASE_URL,
    )
    parser.add_argument(
        "--openrouter-model",
        default=os.environ.get("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL).strip() or DEFAULT_OPENROUTER_MODEL,
    )
    parser.add_argument(
        "--openrouter-timeout-seconds",
        type=int,
        default=env_int("OPENROUTER_TIMEOUT_SECONDS", 45),
    )
    parser.add_argument(
        "--openrouter-referer",
        default=os.environ.get("OPENROUTER_REFERER", "").strip(),
    )
    parser.add_argument(
        "--openrouter-title",
        default=os.environ.get("OPENROUTER_TITLE", "xray-routing-panel google domain classifier").strip(),
    )
    args = parser.parse_args(argv)
    args.report_path = Path(args.report_path).expanduser()
    args.classification_state_path = Path(args.classification_state_path).expanduser()
    return args


def main(argv=None):
    args = parse_args(argv)
    service = GoogleOpenRouterDomainClassifierService(args)
    transport = StdioJsonRpcTransport(sys.stdin.buffer, sys.stdout.buffer)
    server = GoogleOpenRouterDomainClassifierMcpServer(service, transport, sys.stderr)
    return server.run()


# Backward-compatible aliases for any local imports that were already using the old names.
GoogleCodexDomainClassifierService = GoogleOpenRouterDomainClassifierService
GoogleCodexDomainClassifierMcpServer = GoogleOpenRouterDomainClassifierMcpServer


if __name__ == "__main__":
    raise SystemExit(main())
