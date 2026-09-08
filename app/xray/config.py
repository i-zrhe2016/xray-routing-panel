from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
LOGS_DIR = BASE_DIR / "logs"
REPORTS_DIR = BASE_DIR / "reports"
HOURLY_REPORTS_DIR = REPORTS_DIR / "hourly-domains"
RUNTIME_DIR = BASE_DIR / "runtime"
ENV_FILE_PATH = BASE_DIR / ".env"
ENV_EXAMPLE_PATH = BASE_DIR / ".env.example"
AI_PROXY_OUTBOUND_TEMPLATE_PATH = BASE_DIR / "ai-proxy-outbound.json"
AI_PROXY_OUTBOUND_EXAMPLE_PATH = ASSETS_DIR / "ai-proxy-outbound.example.json"
GENERATE_SECRETS_SCRIPT = BASE_DIR / "generate-secrets.sh"
DEFAULT_RENDER_MODULE = "app.xray.render_config"
DEFAULT_AI_DOMAIN_MANAGER_MODULE = "app.xray.ai_routing.runner"
DEFAULT_CLIENT_CONFIG_PATH = RUNTIME_DIR / "client-test.json"

REQUIRED_ENV_KEYS = [
    "XRAY_LISTEN_HOST",
    "XRAY_LISTEN_PORT",
    "XRAY_PUBLIC_HOST",
    "XRAY_CLIENT_UUID",
    "XRAY_FLOW",
    "XRAY_REALITY_PRIVATE_KEY",
    "XRAY_REALITY_PUBLIC_KEY",
    "XRAY_REALITY_SHORT_ID",
    "XRAY_SERVER_NAME",
    "XRAY_DEST",
    "XRAY_FINGERPRINT",
    "XRAY_LOGLEVEL",
    "XRAY_NODE_TAG",
]
