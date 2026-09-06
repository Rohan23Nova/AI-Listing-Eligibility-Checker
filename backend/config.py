"""
config.py — Central environment variable loading and feature flags.

All configuration lives here. Modules import from this file rather than
reading env vars directly, so the entire config surface is visible in one place.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env file if present (development convenience; production uses real env vars)
load_dotenv()

# ── LLM Provider ──────────────────────────────────────────────────────────────
# Supports "groq", "gemini", or "mock".
# If the key for the chosen provider is empty, the system automatically falls
# back to "mock" and labels those scores as "unavailable — LLM not configured".
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "mock").lower()
LLM_MODEL: str = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

# Resolve effective provider: if key is missing, silently degrade to mock
def _resolve_provider() -> str:
    if LLM_PROVIDER == "groq" and not GROQ_API_KEY:
        return "mock"
    if LLM_PROVIDER == "gemini" and not GEMINI_API_KEY:
        return "mock"
    return LLM_PROVIDER

EFFECTIVE_LLM_PROVIDER: str = _resolve_provider()

# ── Feature Flags ─────────────────────────────────────────────────────────────
# These gate expensive/optional subsystems. Each defaults to False so the tool
# works fully in deterministic mode with no external dependencies.
ENABLE_PLAYWRIGHT: bool = os.getenv("ENABLE_PLAYWRIGHT", "false").lower() == "true"
ENABLE_GEO_CHECK: bool = os.getenv("ENABLE_GEO_CHECK", "false").lower() == "true"
ENABLE_LLM: bool = os.getenv("ENABLE_LLM", "false").lower() == "true"

# ── Cloudflare Worker endpoints (Tier-2 geo trip-wire) ───────────────────────
# If empty, geo checks are skipped gracefully.
GEO_WORKER_URL_US: str = os.getenv("GEO_WORKER_URL_US", "")
GEO_WORKER_URL_EU: str = os.getenv("GEO_WORKER_URL_EU", "")
GEO_WORKER_URL_APAC: str = os.getenv("GEO_WORKER_URL_APAC", "")

GEO_WORKER_URLS: list[str] = [
    u for u in [GEO_WORKER_URL_US, GEO_WORKER_URL_EU, GEO_WORKER_URL_APAC] if u
]

# ── Storage ───────────────────────────────────────────────────────────────────
SQLITE_DB_PATH: str = os.getenv("SQLITE_DB_PATH", "data/history.db")

# ── Server ────────────────────────────────────────────────────────────────────
HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = int(os.getenv("PORT", "8000"))

# ── HTTP Client tuning ────────────────────────────────────────────────────────
# Cheap checks (robots.txt, meta fetch) use a short timeout.
# UA-spoof fetches get slightly more time in case of slow sites.
HTTP_TIMEOUT_CHEAP: float = 10.0
HTTP_TIMEOUT_UA_FETCH: float = 15.0

# Common headers for our own analysis requests (not UA-spoof requests)
DEFAULT_HEADERS: dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
}
