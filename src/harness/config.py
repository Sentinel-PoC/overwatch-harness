"""Settings — non-secret config from env file + os.environ.

Secrets are NOT in this module. They come from secrets.py at startup, which
reads Vault using the paths declared here. Keeping the split clean means a
crash dump of HarnessSettings never contains a token.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class HarnessSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path.home() / ".config" / "overwatch-harness" / "env",
        env_file_encoding="utf-8",
        env_prefix="HARNESS_",
        extra="ignore",
        case_sensitive=False,
    )

    # Vault — direct VM IP per operator preference (survives cluster outage)
    vault_addr: str = Field(default="https://192.168.12.206:8200", alias="VAULT_ADDR")
    vault_skip_verify: bool = Field(default=True, alias="VAULT_SKIP_VERIFY")

    # Vault KV paths — daemon resolves actual values via hvac at startup
    vault_telegram_token_path: str = "secret/data/overwatch-harness/telegram"
    vault_telegram_token_field: str = "TELEGRAM_BOT_TOKEN"
    vault_plane_key_path: str = "secret/data/plane/api-key"
    vault_plane_key_field: str = "api_key"
    vault_langfuse_path: str = "secret/data/langfuse/overwatch-agents"

    # Plane
    plane_base_url: str = "https://plane.208.haist.farm"
    plane_workspace: str = "haists-it-consulting"
    plane_project_id: str = ""

    # Tunables
    max_concurrent_sessions: int = 4
    idle_window_seconds: int = 1800
    log_level: str = "INFO"

    # Allowlist — comma-separated Telegram chat_ids; empty = fail closed
    telegram_allowlist: str = ""

    # Local paths
    state_dir: Path = Field(default=Path.home() / ".local" / "state" / "overwatch-harness")
    db_path: Path = Field(default=Path.home() / ".local" / "state" / "overwatch-harness" / "harness.db")

    # /healthz bind — 127.0.0.1 only, never network-exposed
    healthz_host: str = "127.0.0.1"
    healthz_port: int = 8787

    # Self-test
    selftest_command: str = "claude"
    selftest_timeout_seconds: float = 30.0

    @field_validator("telegram_allowlist")
    @classmethod
    def _strip_allowlist(cls, v: str) -> str:
        return v.strip()

    def allowed_chat_ids(self) -> frozenset[int]:
        if not self.telegram_allowlist:
            return frozenset()
        return frozenset(
            int(part.strip())
            for part in self.telegram_allowlist.split(",")
            if part.strip()
        )


def load_settings() -> HarnessSettings:
    return HarnessSettings()
