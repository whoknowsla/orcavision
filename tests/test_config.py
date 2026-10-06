import pytest


@pytest.fixture
def config(submodules):
    return submodules.config


def test_defaults(config):
    defaults = config.DEFAULTS
    assert defaults["provider"] == "openai"
    assert defaults["openai-model"] == "gpt-4o"
    assert defaults["anthropic-model"] == "claude-opus-5-5"
    assert defaults["gemini-model"] == "gemini-2.5-flash"
    assert defaults["ollama-model"] == ""
    assert defaults["openai-compatible-model"] == ""
    assert defaults["openai-compatible-base-url"] == ""
    assert defaults["prompt"] == config.DEFAULT_PROMPT
    assert defaults["sound-theme"] == "modern"
    assert defaults["key-storage"] == "auto"
    assert not [key for key in defaults if key.endswith("-api-key")]


@pytest.mark.parametrize("language,expected_suffix", [
    ("en", ""), ("English", ""), ("", ""), ("Turkish", "\n\nRespond in Turkish."),
])
def test_build_prompt(config, language, expected_suffix):
    assert config.build_prompt("Describe.", language) == "Describe." + expected_suffix


def test_build_prompt_with_hint_and_default(config):
    assert config.build_prompt("  ", "en") == config.DEFAULT_PROMPT
    assert config.build_prompt("Describe.", "Turkish", "Only the button.") == (
        "Describe.\n\nOnly the button.\n\nRespond in Turkish.")
    assert config.build_prompt("", "en", "Only the button.") == (
        config.DEFAULT_PROMPT + "\n\nOnly the button.")


ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")


def test_environment_api_key(config):
    assert config.environment_api_key(ENV_VARS, {"GOOGLE_API_KEY": "google"}) == "google"
    assert config.environment_api_key(ENV_VARS, {"GEMINI_API_KEY": "gem", "GOOGLE_API_KEY": "g"}) == "gem"
    assert config.environment_api_key(ENV_VARS, {"GEMINI_API_KEY": " ", "GOOGLE_API_KEY": "g"}) == "g"
    assert config.environment_api_key(ENV_VARS, {}) == ""
