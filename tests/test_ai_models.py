"""Default provider resolution (Claude Sonnet) + runtime model lists (ADR-078)."""

import json
import logging

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import ai_client
from app import app
from database import Base, get_db
from models import SettingsModel


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = Session()
    monkeypatch.setattr(ai_client, "SessionLocal", Session)

    def _override_db():
        yield db
    app.dependency_overrides[get_db] = _override_db
    ai_client.invalidate_config_cache()
    with TestClient(app) as c:
        yield c, db
    app.dependency_overrides.clear()
    ai_client.invalidate_config_cache()
    db.close()


def _save(db, **kv):
    for k, v in kv.items():
        db.add(SettingsModel(setting_key=k, setting_value=v))
    db.commit()
    ai_client.invalidate_config_cache()


# ---- default resolution ----------------------------------------------------
def test_fresh_install_defaults_to_claude_sonnet(client):
    c, _ = client
    cfg = ai_client.get_ai_config(force=True)
    assert cfg["provider"] == "anthropic"
    assert cfg["model"] == "claude-sonnet-5-5"
    assert cfg["base_url"] == "https://api.anthropic.com/v1"
    assert ai_client.PRESETS["anthropic"]["models"] == [
        "claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-4-5-20251001"]
    assert c.get("/api/settings/ai").json()["provider"] == "anthropic"


def test_saved_settings_keep_theirs_untouched(client):
    c, db = client
    _save(db, ai_provider="anthropic", ai_model="claude-sonnet-5", ai_api_key="k")
    cfg = ai_client.get_ai_config(force=True)
    assert (cfg["provider"], cfg["model"]) == ("anthropic", "claude-sonnet-5")
    c.get("/api/settings/ai")
    assert db.query(SettingsModel).filter_by(setting_key="ai_model").first().setting_value == "claude-sonnet-5"


def test_saved_gemini_stays_gemini(client):
    _, db = client
    _save(db, ai_provider="gemini", ai_model="gemini-3.5-flash", ai_api_key="k")
    cfg = ai_client.get_ai_config(force=True)
    assert (cfg["provider"], cfg["model"]) == ("gemini", "gemini-3.5-flash")


def test_legacy_env_key_install_stays_gemini(client, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "env-g")
    cfg = ai_client.get_ai_config(force=True)
    assert (cfg["provider"], cfg["api_key"], cfg["model"]) == ("gemini", "env-g", "gemini-3.5-flash")


def test_env_gemini_key_never_used_for_other_provider(client, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "env-g")
    _save(client[1], ai_provider="anthropic", ai_model="claude-sonnet-5-5")
    assert ai_client.get_ai_config(force=True)["api_key"] == ""


def test_anthropic_payload_caps_tokens_and_skips_temperature():
    seen = {}

    def _h(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    with respx.mock:
        respx.post("https://api.anthropic.com/v1/chat/completions").mock(side_effect=_h)
        cfg = {"provider": "anthropic", "base_url": "https://api.anthropic.com/v1", "api_key": "k",
               "model": "claude-sonnet-5-5", "model_fast": "claude-sonnet-5-5", "temperature": 0.4}
        assert ai_client.chat("vision", [{"role": "user", "content": "x"}], cfg=cfg) == "ok"
    assert seen["max_tokens"] == 8192 and "temperature" not in seen


# ---- list parsing + filtering ---------------------------------------------
FIXTURES = {
    "openai": ("https://api.openai.com/v1", {"data": [
        {"id": "gpt-5.6-terra"}, {"id": "gpt-5.6-terra-2026-09-01"}, {"id": "text-embedding-3-large"},
        {"id": "gpt-image-2"}, {"id": "tts-1"}, {"id": "whisper-1"}, {"id": "omni-moderation-latest"},
        {"id": "gpt-4o"}, {"id": "gpt-3.5-turbo"}, {"id": "gpt-4o-realtime-preview"}]},
        ["gpt-4o", "gpt-5.6-terra"]),
    "anthropic": ("https://api.anthropic.com/v1", {"data": [
        {"id": "claude-sonnet-5-5", "capabilities": {"image_input": {"supported": True}}},
        {"id": "claude-haiku-4-5"}, {"id": "claude-haiku-4-5-20251001"},
        {"id": "claude-3-opus-20240229"}, {"id": "claude-opus-5-5"},
        {"id": "claude-textonly-5", "capabilities": {"image_input": {"supported": False}}},
        {"id": "claude-sonnet-4", "capabilities": {"image_input": {"supported": False}}}]},
        ["claude-haiku-4-5", "claude-opus-5-5", "claude-sonnet-5-5"]),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", {"data": [
        {"id": "models/gemini-3.6-flash"}, {"id": "models/gemini-3.6-flash-001"},
        {"id": "models/gemini-3.5-flash-image"}, {"id": "models/text-embedding-004"},
        {"id": "models/gemini-3.5-flash-tts"}, {"id": "models/gemini-3.5-flash-live"},
        {"id": "models/imagen-4"}]},
        ["gemini-3.6-flash"]),
    "openrouter": ("https://openrouter.ai/api/v1", {"data": [
        {"id": "google/gemini-3.6-flash",
         "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]}},
        {"id": "x/textonly", "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
        {"id": "x/painter",
         "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["image"]}},
        {"id": "x/old", "expiration_date": "2026-01-01",
         "architecture": {"input_modalities": ["image"], "output_modalities": ["text"]}},
        {"id": "x/vis-2026-09-01",
         "architecture": {"input_modalities": ["image"], "output_modalities": ["text"]}},
        {"id": "x/vis", "architecture": {"input_modalities": ["image"], "output_modalities": ["text"]}}]},
        ["google/gemini-3.6-flash", "x/vis"]),
    "ollama": ("http://host.docker.internal:11434/v1", {"data": [
        {"id": "llama3.2-vision:latest"}, {"id": "nomic-embed-text"}, {"id": "llama3.1:8b"},
        {"id": "qwen2.5vl:7b"}]},
        ["llama3.2-vision:latest", "qwen2.5vl:7b"]),
}


@pytest.mark.parametrize("provider", list(FIXTURES))
def test_each_provider_list_parsed_and_filtered(provider):
    base, body, want = FIXTURES[provider]
    with respx.mock:
        route = respx.get(f"{base}/models").mock(return_value=httpx.Response(200, json=body))
        got = ai_client.fetch_provider_models(provider, base, "sekret-key-123")
    assert got == want
    req = route.calls[0].request
    if provider == "anthropic":
        assert req.headers["x-api-key"] == "sekret-key-123" and "anthropic-version" in req.headers
    else:
        assert req.headers["authorization"] == "Bearer sekret-key-123"


def test_redirect_is_not_followed():
    base = "https://api.openai.com/v1"
    with respx.mock:
        respx.get(f"{base}/models").mock(
            return_value=httpx.Response(302, headers={"location": "https://evil.example/models"}))
        evil = respx.get("https://evil.example/models").mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(ai_client.AIConfigError):
            ai_client.fetch_provider_models("openai", base, "sekret-key-123")
    assert not evil.called


# ---- route: cache, fallback, key hygiene -----------------------------------
def test_refresh_route_caches_then_falls_back_to_cache_when_down(client, caplog):
    c, db = client
    _save(db, ai_provider="anthropic", ai_api_key="sk-ant-sekretsekret1", ai_model="claude-sonnet-5-5")
    base, body, want = FIXTURES["anthropic"]
    with respx.mock:
        respx.get(f"{base}/models").mock(return_value=httpx.Response(200, json=body))
        with caplog.at_level(logging.DEBUG):
            r = c.post("/api/settings/ai/models", json={"provider": "anthropic"}).json()
        assert (r["source"], r["models"]) == ("live", want) and r["fetched_at"]
    assert c.get("/api/settings/ai").json()["fetched_models"]["anthropic"]["models"] == want

    with respx.mock:
        respx.get(f"{base}/models").mock(side_effect=httpx.ConnectError("down"))
        r = c.post("/api/settings/ai/models", json={"provider": "anthropic"}).json()
    assert r["source"] == "cache" and r["models"] == want and r["error"]
    assert "sk-ant-sekretsekret1" not in caplog.text and "sk-ant-sekretsekret1" not in str(r)


def test_refresh_route_falls_back_to_presets_with_no_cache(client):
    c, db = client
    _save(db, ai_provider="anthropic", ai_api_key="k1")
    with respx.mock:
        respx.get("https://api.anthropic.com/v1/models").mock(return_value=httpx.Response(401))
        r = c.post("/api/settings/ai/models", json={"provider": "anthropic"}).json()
    assert r["source"] == "preset" and r["models"] == ai_client.PRESETS["anthropic"]["models"]


def test_saved_key_not_sent_to_other_provider_or_client_supplied_host(client):
    c, db = client
    _save(db, ai_provider="anthropic", ai_api_key="sk-ant-onlyforanthropic")
    with respx.mock:
        evil = respx.get("https://evil.example/v1/models").mock(return_value=httpx.Response(200, json={}))
        oai = respx.get("https://api.openai.com/v1/models").mock(
            return_value=httpx.Response(200, json={"data": []}))
        r = c.post("/api/settings/ai/models",
                   json={"provider": "openai", "base_url": "https://evil.example/v1"}).json()
    assert not evil.called
    assert not oai.called            # no key for openai => never even asks
    assert r["source"] == "preset"
