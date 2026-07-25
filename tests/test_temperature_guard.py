"""Don't send `temperature` to Claude models that reject it.

Anthropic removed the sampling parameters from Claude Opus 4.7 onward and across the 5-family — sending
`temperature` is a hard 400, not a warning. The Temperature box in Admin → AI Engine is
provider-agnostic, so a user who sets it and then picks a current Claude model would break every
enrichment. Found in the 2026-07-25 UAT as a latent trap: harmless only because the shipped presets
still listed older models and the temperature setting defaults to empty.
"""

import pytest

from ai_client import rejects_temperature


@pytest.mark.parametrize("model", [
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "anthropic/claude-sonnet-5",      # OpenRouter-style prefix
    "Claude-Sonnet-5",                # case-insensitive
])
def test_models_that_reject_temperature(model):
    assert rejects_temperature(model) is True


@pytest.mark.parametrize("model", [
    "claude-haiku-4-5",               # still accepts sampling params
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "gemini-3.5-flash",
    "gpt-5.4-mini",
    "llama3.2-vision",
    "",
    None,
])
def test_models_that_accept_temperature(model):
    assert rejects_temperature(model) is False
