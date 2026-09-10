"""Configuration loading for the SP-API integration.

Mirrors keepa_mcp/config.py's shape (dataclass Settings + hand-rolled .env
parser, no python-dotenv) but is a fully independent implementation - this
package must keep working even if keepa_mcp/ is removed.
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
    lwa_client_id: str
    lwa_client_secret: str
    refresh_token: str
    region: str  # na / eu / fe
    marketplace_id: str

    @property
    def configured(self) -> bool:
        """True only if every credential needed to call SP-API is present.

        Callers should check this before making a live call and fall back to
        returning empty data (never raise) when it's False - the dashboard
        must render correctly before SP-API credentials have been issued.
        """
        return bool(self.lwa_client_id and self.lwa_client_secret and self.refresh_token)

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            lwa_client_id=os.getenv("LWA_CLIENT_ID", ""),
            lwa_client_secret=os.getenv("LWA_CLIENT_SECRET", ""),
            refresh_token=os.getenv("SP_API_REFRESH_TOKEN", ""),
            region=os.getenv("SP_API_REGION", "na"),
            marketplace_id=os.getenv("SP_API_MARKETPLACE_ID", "ATVPDKIKX0DER"),  # US
        )


settings = Settings.load()

# SP-API region -> base endpoint host (no AWS SigV4 needed for a
# self-authorized Private App; LWA access token is sufficient as of the
# 2023 SP-API auth simplification).
REGION_ENDPOINTS = {
    "na": "https://sellingpartnerapi-na.amazon.com",
    "eu": "https://sellingpartnerapi-eu.amazon.com",
    "fe": "https://sellingpartnerapi-fe.amazon.com",
}


def endpoint() -> str:
    return REGION_ENDPOINTS.get(settings.region, REGION_ENDPOINTS["na"])
