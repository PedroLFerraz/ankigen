"""Provider resolution (ported from v1) and connection checks."""
from unittest.mock import patch

import pytest

from ankigen import llm
from ankigen.config import PROVIDERS, Settings


def _s(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_ollama_defaults():
    cfg = _s(llm_provider="ollama").resolve_llm()
    assert cfg["base_url"] == "http://localhost:11434/v1" and cfg["needs_key"] is False


def test_preset_used():
    cfg = _s(llm_provider="groq", llm_api_key="k").resolve_llm()
    assert cfg["base_url"] == PROVIDERS["groq"]["base_url"]
    assert cfg["model"] == PROVIDERS["groq"]["model"] and cfg["api_key"] == "k"


def test_overrides_win():
    cfg = _s(llm_provider="groq", llm_base_url="https://x/v1", llm_model="m").resolve_llm()
    assert (cfg["base_url"], cfg["model"]) == ("https://x/v1", "m")


def test_embeddings_fall_back_to_an_in_process_model():
    """The fallback has to work on a machine that has never run this before —
    a CI runner has no Ollama on localhost."""
    cfg = _s(llm_provider="groq").resolve_embedding()
    assert cfg["provider"] == "fastembed"
    assert cfg["base_url"] is None and cfg["api_key"] == ""


def test_ollama_embeddings_still_available_when_asked_for():
    assert _s(embedding_provider="ollama").resolve_embedding()["provider"] == "ollama"


def test_embeddings_stay_on_provider_that_supports_them():
    assert _s(llm_provider="nvidia").resolve_embedding()["provider"] == "nvidia"


def test_openrouter_serves_both_on_one_key():
    """OpenRouter has a free embeddings endpoint, so it needs no Ollama sidecar."""
    s = _s(llm_provider="openrouter", llm_api_key="k")
    emb = s.resolve_embedding()
    assert emb["provider"] == "openrouter"
    assert emb["model"] == PROVIDERS["openrouter"]["embedding_model"]
    assert emb["api_key"] == "k"          # inherited from the chat provider


def test_embeddings_none_for_unsupported_explicit_choice():
    assert _s(embedding_provider="groq").resolve_embedding()["provider"] is None


def test_checker_defaults_to_the_generating_model():
    cfg = _s(llm_provider="groq", llm_api_key="k")
    assert cfg.resolve_verify() == cfg.resolve_llm()


def test_checker_can_be_a_different_model_or_provider():
    """A model marking its own homework shares its own blind spots, and free
    tiers meter per model, so an independent checker is free in both senses."""
    same_provider = _s(llm_provider="claude", claude_model="claude-opus-5-5",
                       verify_model="claude-sonnet-5")
    assert same_provider.resolve_verify()["model"] == "claude-sonnet-5"
    assert same_provider.resolve_llm()["model"] == "claude-opus-5-5"

    split = _s(llm_provider="groq", llm_api_key="k",
               verify_provider="claude", verify_model="claude-sonnet-5")
    assert split.resolve_llm()["provider"] == "groq"
    assert (split.resolve_verify()["provider"], split.resolve_verify()["model"]) \
        == ("claude", "claude-sonnet-5")


def test_threshold_comes_from_the_embedding_model():
    """Similarity scales differ per model, so the threshold travels with it."""
    assert _s(llm_provider="ollama").resolve_embedding()["threshold"] == 0.90
    assert _s(embedding_provider="fastembed").resolve_embedding()["threshold"] == 0.90
    assert _s(llm_provider="openrouter", llm_api_key="k").resolve_embedding()["threshold"] == 0.85
    assert _s(llm_provider="openrouter", llm_api_key="k",
              semantic_threshold=0.75).resolve_embedding()["threshold"] == 0.75


@pytest.fixture
def use(monkeypatch):
    def apply(**kw):
        monkeypatch.setattr("ankigen.llm.settings", _s(**kw))
    return apply


def test_missing_key(use):
    use(llm_provider="groq", llm_api_key="")
    assert "No API key" in llm.check_connection()


def test_ollama_probe_strips_v1_suffix_not_characters(use):
    """v1 used rstrip('/v1'), which mangled ports ending in 1."""
    use(llm_provider="ollama", ollama_base_url="http://host:8001/v1")
    with patch("requests.get") as get:
        assert llm.check_connection() is None
    assert get.call_args[0][0] == "http://host:8001/api/tags"


def test_hosted_probe_sends_bearer_token(use):
    use(llm_provider="groq", llm_api_key="secret")
    with patch("requests.get") as get:
        assert llm.check_connection() is None
    assert get.call_args[1]["headers"] == {"Authorization": "Bearer secret"}


# ---------------------------------------------------------------- fallback

def test_claude_has_no_fallback_unless_one_is_set():
    cfg = _s(llm_provider="claude")
    assert "fallback" not in cfg.resolve_llm() and "fallback" not in cfg.resolve_verify()
    backed = _s(llm_provider="claude", fallback_provider="groq", llm_api_key="k")
    assert backed.resolve_llm()["fallback"]["provider"] == "groq"
