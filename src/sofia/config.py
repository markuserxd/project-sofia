"""Typed startup settings loaded from the process environment."""

import os
from collections.abc import Mapping
from dataclasses import dataclass

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigurationError(ValueError):
    """A setting is invalid. Its message is safe to display to the user."""


@dataclass(frozen=True, slots=True)
class Settings:
    log_level: str = "INFO"
    provider: str = "mock"

    def __post_init__(self) -> None:
        level = self.log_level.strip().upper() if isinstance(self.log_level, str) else None
        if level not in _LOG_LEVELS:
            raise ConfigurationError(
                f"Invalid SOFIA_LOG_LEVEL. Expected one of {', '.join(_LOG_LEVELS)}."
            )
        provider = self.provider.strip().lower() if isinstance(self.provider, str) else None
        if provider != "mock":
            raise ConfigurationError("Invalid SOFIA_PROVIDER. Set it to mock.")
        object.__setattr__(self, "log_level", level)
        object.__setattr__(self, "provider", provider)


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Read only the supported SOFIA settings from the supplied environment."""
    if environ is None:
        environ = os.environ
    defaults = Settings()
    return Settings(
        log_level=environ.get("SOFIA_LOG_LEVEL", defaults.log_level),
        provider=environ.get("SOFIA_PROVIDER", defaults.provider),
    )
