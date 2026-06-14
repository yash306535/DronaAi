"""Application configuration and secrets management.

Implements Requirement 15.1 and 15.2:

- 15.1: All secrets (JWT secret, API keys) are loaded from environment
  configuration. Secret values are wrapped in ``pydantic.SecretStr`` so they are
  never accidentally rendered into logs, error messages, error envelopes, or API
  responses (``repr``/``str`` of a ``SecretStr`` yields ``'**********'``).
- 15.2: If any required secret is absent from environment configuration at
  startup, ``get_settings()`` aborts startup by raising ``MissingSecretError``
  whose message names the missing configuration key(s) *by key name only* and
  never includes any secret value.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Configuration keys that are required secrets. Startup aborts (by key name
# only) if any of these are absent from environment configuration.
REQUIRED_SECRET_KEYS: tuple[str, ...] = (
    "JWT_SECRET",
    "OPENAI_API_KEY",
)


class MissingSecretError(RuntimeError):
    """Raised at startup when one or more required secrets are absent.

    The message names the missing configuration key(s) by name only. It never
    contains any secret value (there is none to contain, since the value is
    absent), satisfying Requirement 15.2's "by configuration key name only".
    """

    def __init__(self, missing_keys: list[str]) -> None:
        self.missing_keys = list(missing_keys)
        joined = ", ".join(self.missing_keys)
        super().__init__(
            "Startup aborted: required secret configuration key(s) missing: "
            f"{joined}. Set these environment variables and restart."
        )


class Settings(BaseSettings):
    """Environment-driven application settings.

    Secrets are typed as ``SecretStr`` so their values are never echoed by
    ``repr``/``str``/logging. Required secrets have no default; if absent the
    field is ``None`` and ``validate_required_secrets`` reports it by key name.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- Required secrets (no defaults; absence aborts startup) ---
    JWT_SECRET: SecretStr | None = Field(default=None)
    OPENAI_API_KEY: SecretStr | None = Field(default=None)

    # --- Optional secrets ---
    ANTHROPIC_API_KEY: SecretStr | None = Field(default=None)
    SMTP_PASSWORD: SecretStr | None = Field(default=None)

    # --- Non-secret application configuration ---
    ENVIRONMENT: str = Field(default="local")
    DATABASE_URL: str = Field(default="sqlite:///./drona.db")
    JWT_ALGORITHM: str = Field(default="HS256")
    ACCESS_TOKEN_TTL_MINUTES: int = Field(default=15)
    REFRESH_TOKEN_TTL_DAYS: int = Field(default=7)
    FRONTEND_ORIGINS: str = Field(default="http://localhost:5173")
    MAX_BODY_BYTES: int = Field(default=1_048_576)
    SMTP_HOST: str | None = Field(default=None)
    SMTP_PORT: int = Field(default=587)
    SMTP_USERNAME: str | None = Field(default=None)

    def missing_required_secrets(self) -> list[str]:
        """Return the names of required secret keys that are absent/empty."""
        missing: list[str] = []
        for key in REQUIRED_SECRET_KEYS:
            value = getattr(self, key, None)
            if value is None or not value.get_secret_value():
                missing.append(key)
        return missing

    def validate_required_secrets(self) -> None:
        """Abort startup (by key name only) if any required secret is absent."""
        missing = self.missing_required_secrets()
        if missing:
            raise MissingSecretError(missing)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build and validate settings once per process.

    Raises ``MissingSecretError`` if any required secret is absent, naming the
    missing configuration key(s) by key name only.
    """
    settings = Settings()
    settings.validate_required_secrets()
    return settings
