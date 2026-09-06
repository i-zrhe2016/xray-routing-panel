import re

XRAY_ACCESS_LOG_LINE_RE = re.compile(
    r"^(?P<seen_at>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?) .* \[(?P<tag>[^\]]+) >> [^\]]+\](?: email: (?P<email>\S+))?$"
)
PLAN_SLUG_RE = re.compile(r"[^a-z0-9]+")
