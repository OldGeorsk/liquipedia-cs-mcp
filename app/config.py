"""Central configuration, loaded from environment variables with sane defaults."""

from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "liquipedia-cs-mcp"
APP_VERSION = "0.7.0"

# Liquipedia text is CC BY-SA 3.0; every result must carry this attribution.
LICENSE_NAME = "CC BY-SA 3.0"
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/3.0/"


class Settings(BaseSettings):
    """All knobs of the server. Override any field via LIQUIPEDIA_* env vars."""

    model_config = SettingsConfigDict(env_prefix="LIQUIPEDIA_", env_file=".env", extra="ignore")

    # Upstream MediaWiki API endpoint of the Counter-Strike wiki.
    api_url: str = "https://liquipedia.net/counterstrike/api.php"
    # Real contact address, required by Liquipedia's User-Agent policy.
    # Set yours in .env (LIQUIPEDIA_CONTACT=...); never commit a real address.
    contact: str = "you@example.com"

    # Randomized spacing between request starts: official floor is 2s, we add margin.
    global_interval_min: float = 2.3
    global_interval_max: float = 3.0
    # action=parse requests additionally respect this lane (official floor 30s).
    parse_interval_min: float = 33.0
    parse_interval_max: float = 35.0

    request_timeout: float = 30.0
    cache_ttl_seconds: float = 86400.0  # one day
    page_budget_per_job: int = 30
    # A job without a heartbeat this long is declared failed, not "running".
    job_stall_seconds: float = 60.0
    # Titles per batched query request; keeps responses modest in size.
    batch_page_limit: int = 10

    host: str = "127.0.0.1"
    port: int = 8000
    db_path: str = "data/liquipedia.db"

    @property
    def user_agent(self) -> str:
        # Format follows Liquipedia's API etiquette: identify the project and a contact.
        return f"{APP_NAME}/{APP_VERSION} (local MVP; contact: {self.contact})"
