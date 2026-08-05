import os
from dataclasses import dataclass

ENV_FILE = ".env"


def load_dotenv(path: str = ENV_FILE) -> None:
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value


load_dotenv()


@dataclass
class Settings:
    keepa_api_key: str
    email_smtp_host: str
    email_smtp_port: int
    email_username: str
    email_password: str
    email_from: str
    email_to: str
    line_notify_token: str
    search_keyword: str
    search_category: str
    us_max_results: int
    price_diff_threshold: float
    usd_to_jpy: float
    sales_rank_threshold: int

    @classmethod
    def load(cls):
        return cls(
            keepa_api_key=os.getenv("KEEPA_API_KEY", ""),
            email_smtp_host=os.getenv("EMAIL_SMTP_HOST", ""),
            email_smtp_port=int(os.getenv("EMAIL_SMTP_PORT", "587")),
            email_username=os.getenv("EMAIL_USERNAME", ""),
            email_password=os.getenv("EMAIL_PASSWORD", ""),
            email_from=os.getenv("EMAIL_FROM", ""),
            email_to=os.getenv("EMAIL_TO", ""),
            line_notify_token=os.getenv("LINE_NOTIFY_TOKEN", ""),
            search_keyword=os.getenv("SEARCH_KEYWORD", "import japan"),
            search_category=os.getenv("SEARCH_CATEGORY", "All"),
            us_max_results=int(os.getenv("US_MAX_RESULTS", "20")),
            price_diff_threshold=float(os.getenv("PRICE_DIFF_THRESHOLD", "0.30")),
            usd_to_jpy=float(os.getenv("USD_TO_JPY", "150.0")),
            sales_rank_threshold=int(os.getenv("SALES_RANK_THRESHOLD", "50000")),
        )
