"""Configuration loading for the Keepa MCP server.

Reuses the repository's .env convention (see ../config.py) so the same
KEEPA_API_KEY / USD_TO_JPY values used by the rest of the project apply here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"


def _load_dotenv(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


@dataclass
class Settings:
    keepa_api_key: str
    usd_to_jpy: float
    default_domain: str
    cache_enabled: bool
    # Prices/ranks/reviews move; keep these caches short-lived by default.
    cache_ttl_product_hours: float
    cache_ttl_finder_hours: float
    # Category trees barely change; safe to cache much longer.
    cache_ttl_category_hours: float

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            keepa_api_key=os.getenv("KEEPA_API_KEY", ""),
            usd_to_jpy=float(os.getenv("USD_TO_JPY", "150.0")),
            default_domain=os.getenv("KEEPA_DEFAULT_DOMAIN", "US"),
            cache_enabled=os.getenv("KEEPA_CACHE_ENABLED", "true").lower() not in ("0", "false", "no"),
            cache_ttl_product_hours=float(os.getenv("KEEPA_CACHE_TTL_PRODUCT_HOURS", "6")),
            cache_ttl_finder_hours=float(os.getenv("KEEPA_CACHE_TTL_FINDER_HOURS", "6")),
            cache_ttl_category_hours=float(os.getenv("KEEPA_CACHE_TTL_CATEGORY_HOURS", "720")),
        )


settings = Settings.load()
