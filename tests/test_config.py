import pytest

from sofia.config import ConfigurationError, Settings, load_settings


def test_defaults_ignore_unrelated_environment_values() -> None:
    assert load_settings({}) == Settings(log_level="INFO", provider="mock")
    settings = load_settings({"OPENAI_API_KEY": "test-private-value"})
    assert settings == Settings()
    assert "test-private-value" not in repr(settings)


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
def test_valid_environment_values(level: str) -> None:
    settings = load_settings(
        {"SOFIA_LOG_LEVEL": f" {level.lower()} ", "SOFIA_PROVIDER": " MOCK "}
    )
    assert (settings.log_level, settings.provider) == (level, "mock")


@pytest.mark.parametrize(
    ("name", "value", "hint"),
    [
        (
            "SOFIA_LOG_LEVEL",
            "test-private-value",
            "Expected one of DEBUG, INFO, WARNING, ERROR, CRITICAL.",
        ),
        (
            "SOFIA_LOG_LEVEL",
            "",
            "Expected one of DEBUG, INFO, WARNING, ERROR, CRITICAL.",
        ),
        ("SOFIA_PROVIDER", "test-private-value", "Set it to mock."),
        ("SOFIA_PROVIDER", "openai", "Set it to mock."),
    ],
)
def test_invalid_environment_values(name: str, value: str, hint: str) -> None:
    with pytest.raises(ConfigurationError) as error:
        load_settings({name: value})
    assert str(error.value) == f"Invalid {name}. {hint}"


def test_explicit_settings_validate_values() -> None:
    with pytest.raises(ConfigurationError, match="Invalid SOFIA_PROVIDER"):
        Settings(provider="anthropic")
