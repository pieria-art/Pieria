"""
Unit tests for ai_client — the unified OpenAI-compatible model client.

These tests avoid real network calls: the pooled ai_client._http_client.post is monkeypatched, and
DB reads are stubbed via ai_client._read_settings_rows so config resolution is exercised in isolation.
"""

import io
import json

import pytest
from PIL import Image

import ai_client


# --------------------------------------------------------------------------- #
# JSON tolerance
# --------------------------------------------------------------------------- #
def test_strip_fences_plain():
    assert ai_client.strip_json_fences('{"a": 1}') == '{"a": 1}'


def test_strip_fences_markdown_json():
    fenced = '```json\n{"a": 1}\n```'
    assert ai_client.parse_json(fenced) == {"a": 1}


def test_strip_fences_bare_backticks():
    fenced = '```\n{"b": 2}\n```'
    assert ai_client.parse_json(fenced) == {"b": 2}


# --------------------------------------------------------------------------- #
# Image content parts
# --------------------------------------------------------------------------- #
def _sample_image_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_image_part_from_bytes():
    part = ai_client.image_part(_sample_image_bytes())
    assert part["type"] == "image_url"
    assert part["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_image_part_normalizes_mode(tmp_path):
    # An RGBA (transparent) PNG must be coerced to RGB JPEG without error.
    p = tmp_path / "rgba.png"
    Image.new("RGBA", (16, 16), (0, 0, 0, 0)).save(p)
    part = ai_client.image_part(str(p))
    assert part["image_url"]["url"].startswith("data:image/jpeg;base64,")


# --------------------------------------------------------------------------- #
# Capability flags
# --------------------------------------------------------------------------- #
def test_supports_json_mode():
    assert ai_client.supports_json_mode("gemini") is True
    assert ai_client.supports_json_mode("openai") is True
    assert ai_client.supports_json_mode("anthropic") is False
    assert ai_client.supports_json_mode("ollama") is False
    assert ai_client.supports_json_mode("nope") is False


# --------------------------------------------------------------------------- #
# Config resolution
# --------------------------------------------------------------------------- #
def test_config_env_fallback(monkeypatch):
    monkeypatch.setattr(ai_client, "_read_settings_rows", lambda: {})
    monkeypatch.setenv("GEMINI_API_KEY", "env-key-123")
    cfg = ai_client.get_ai_config(force=True)
    assert cfg["provider"] == "gemini"
    assert cfg["api_key"] == "env-key-123"
    assert cfg["base_url"] == ai_client.PRESETS["gemini"]["base_url"].rstrip("/")
    assert cfg["model"] == ai_client.DEFAULT_MODEL
    assert cfg["model_fast"] == ai_client.DEFAULT_MODEL  # no override ⇒ primary
    assert cfg["configured"] is True


def test_config_from_db_overrides(monkeypatch):
    monkeypatch.setattr(ai_client, "_read_settings_rows", lambda: {
        "ai_provider": "openai",
        "ai_base_url": "https://api.openai.com/v1",
        "ai_api_key": "sk-test",
        "ai_model": "gpt-4o",
        "ai_model_fast": "gpt-4o-mini",
        "ai_temperature": "0.5",
    })
    cfg = ai_client.get_ai_config(force=True)
    assert cfg["provider"] == "openai"
    assert cfg["model"] == "gpt-4o"
    assert cfg["model_fast"] == "gpt-4o-mini"
    assert cfg["temperature"] == 0.5
    assert cfg["api_key"] == "sk-test"


def test_config_base_url_falls_back_to_preset(monkeypatch):
    # Provider set, base_url blank ⇒ use the provider preset's base_url.
    monkeypatch.setattr(ai_client, "_read_settings_rows", lambda: {
        "ai_provider": "anthropic",
        "ai_api_key": "x",
        "ai_model": "claude-3-5-haiku-latest",
    })
    cfg = ai_client.get_ai_config(force=True)
    assert cfg["base_url"] == ai_client.PRESETS["anthropic"]["base_url"].rstrip("/")


# --------------------------------------------------------------------------- #
# chat() — payload construction & role/model selection (no real network)
# --------------------------------------------------------------------------- #
class _FakeResp:
    def __init__(self, status=200, content="ok"):
        self.status_code = status
        self._content = content
        self.text = json.dumps(self._body()) if status == 200 else "error body"

    def _body(self):
        return {"choices": [{"message": {"content": self._content}}]}

    def json(self):
        return self._body()


def _capture_post(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResp(200, "hello")

    monkeypatch.setattr(ai_client._http_client, "post", fake_post)
    return captured


def _cfg(provider="gemini", **kw):
    base = {
        "provider": provider,
        "base_url": ai_client.PRESETS[provider]["base_url"].rstrip("/"),
        "api_key": "k",
        "model": "smart-model",
        "model_fast": "fast-model",
        "temperature": None,
    }
    base.update(kw)
    return base


def test_chat_vision_uses_primary_model_and_json_mode(monkeypatch):
    cap = _capture_post(monkeypatch)
    out = ai_client.chat("vision", [{"role": "user", "content": "hi"}], json_mode=True, cfg=_cfg("gemini"))
    assert out == "hello"
    assert cap["json"]["model"] == "smart-model"
    assert cap["json"]["response_format"] == {"type": "json_object"}
    assert cap["url"].endswith("/chat/completions")
    assert cap["headers"]["Authorization"] == "Bearer k"


def test_chat_fast_uses_fast_model(monkeypatch):
    cap = _capture_post(monkeypatch)
    ai_client.chat("fast", [{"role": "user", "content": "hi"}], cfg=_cfg("gemini"))
    assert cap["json"]["model"] == "fast-model"


def test_chat_json_mode_skipped_for_unsupported_provider(monkeypatch):
    cap = _capture_post(monkeypatch)
    ai_client.chat("vision", [{"role": "user", "content": "hi"}], json_mode=True, cfg=_cfg("anthropic"))
    assert "response_format" not in cap["json"]


def test_chat_raises_without_key(monkeypatch):
    _capture_post(monkeypatch)
    with pytest.raises(ai_client.AIConfigError):
        ai_client.chat("vision", [{"role": "user", "content": "hi"}], cfg=_cfg("gemini", api_key=""))


def test_chat_raises_on_http_error(monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        return _FakeResp(401)
    monkeypatch.setattr(ai_client._http_client, "post", fake_post)
    with pytest.raises(ai_client.AIConfigError):
        ai_client.chat("vision", [{"role": "user", "content": "hi"}], cfg=_cfg("gemini"))


def test_validate_config_roundtrips(monkeypatch):
    cap = _capture_post(monkeypatch)
    reply = ai_client.validate_config("openai", "https://api.openai.com/v1", "sk-x", "gpt-4o")
    assert reply == "hello"
    assert cap["json"]["model"] == "gpt-4o"
