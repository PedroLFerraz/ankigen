"""Amazon Bedrock through the Converse API, with a stubbed client."""
import pytest

from ankigen import llm
from ankigen.config import Settings, settings


class ClientError(Exception):
    """Shaped like botocore's: the code lives in .response."""

    def __init__(self, code):
        super().__init__(f"An error occurred ({code}) when calling the Converse operation")
        self.response = {"Error": {"Code": code}}


class FakeBedrock:
    def __init__(self, reply='{"cards": []}', error=None):
        self.reply, self.error, self.calls = reply, error, []

    def converse(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return {"output": {"message": {"role": "assistant", "content": [{"text": self.reply}]}},
                "usage": {"inputTokens": 12, "outputTokens": 5}}


@pytest.fixture
def bedrock(monkeypatch):
    for name, value in {"llm_provider": "bedrock", "llm_model": "m-a,m-b",
                        "bedrock_region": "eu-west-1", "verify_provider": "",
                        "verify_model": "", "fallback_provider": ""}.items():
        monkeypatch.setattr(settings, name, value, raising=False)
    monkeypatch.setattr(llm, "_bedrock_clients", {})
    monkeypatch.setattr(llm, "_spent", set())
    monkeypatch.setattr(llm, "_busy_until", {})
    monkeypatch.setattr(llm, "_down", {})

    def use(client):
        monkeypatch.setattr(llm, "_get_bedrock_client", lambda: client)
        return client
    return use


def test_config_takes_the_chain_from_llm_model_and_needs_no_key():
    cfg = Settings(_env_file=None, llm_provider="bedrock", llm_model="a, b",
                   bedrock_region="us-east-1").resolve_llm()
    assert (cfg["provider"], cfg["models"], cfg["region"]) == ("bedrock", ["a", "b"], "us-east-1")
    assert cfg["needs_key"] is False and cfg["api_key"] == ""


def test_bedrock_is_a_provider_the_checker_and_a_pipeline_can_name():
    from ankigen.config import provider_names
    assert "bedrock" in provider_names()
    cfg = Settings(_env_file=None, llm_provider="groq", verify_provider="bedrock",
                   verify_model="m-x").resolve_verify()
    assert (cfg["provider"], cfg["models"]) == ("bedrock", ["m-x"])


def test_a_call_returns_the_json_and_records_the_tokens(bedrock):
    client = bedrock(FakeBedrock('Here:\n```json\n{"cards": [1]}\n```'))
    before = llm.tokens_used().get("m-a", {"prompt": 0, "completion": 0})
    result = llm.call_json("write cards")
    assert result.data == {"cards": [1]} and result.model == "m-a"
    assert (result.prompt_tokens, result.completion_tokens) == (12, 5)
    [call] = client.calls
    assert call["modelId"] == "m-a"
    assert call["messages"] == [{"role": "user", "content": [{"text": "write cards"}]}]
    used = llm.tokens_used()["m-a"]
    assert used["prompt"] - before["prompt"] == 12 and used["completion"] - before["completion"] == 5


def test_throttling_is_transient_and_moves_on_to_the_next_model(bedrock, monkeypatch):
    class Throttled(FakeBedrock):
        def converse(self, **kw):
            if kw["modelId"] == "m-a":
                self.calls.append(kw)
                raise ClientError("ThrottlingException")
            return super().converse(**kw)

    client = bedrock(Throttled())
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    result = llm.call_json("p")
    assert result.model == "m-b" and [c["modelId"] for c in client.calls] == ["m-a", "m-b"]


def test_throttling_raises_a_transient_error(bedrock):
    bedrock(FakeBedrock(error=ClientError("ThrottlingException")))
    with pytest.raises(llm.TransientProviderError):
        llm._bedrock_json("m-a", "p")
    assert llm._is_retryable("bedrock (m-a) rate limit: ...")


def test_access_denied_is_unavailable_and_not_tried_on_the_next_model(bedrock):
    client = bedrock(FakeBedrock(error=ClientError("AccessDeniedException")))
    with pytest.raises(llm.ProviderUnavailable):
        llm.call_json("p")
    assert len(client.calls) == 1


def test_access_denied_hands_the_run_to_the_fallback(bedrock, monkeypatch):
    bedrock(FakeBedrock(error=ClientError("AccessDeniedException")))
    monkeypatch.setattr(settings, "fallback_provider", "groq")
    monkeypatch.setattr(settings, "llm_api_key", "k")
    real = llm._call_one_model

    def call(prompt, cfg, model, retries):
        if cfg["provider"] == "groq":
            return llm.LLMResult({"from": "groq"}, model)
        return real(prompt, cfg, model, retries)

    monkeypatch.setattr(llm, "_call_one_model", call)
    assert llm.call_json("p").data == {"from": "groq"}
    assert "bedrock" in llm._down


def test_missing_credentials_are_unavailable(bedrock):
    class NoCredentialsError(Exception):
        pass

    bedrock(FakeBedrock(error=NoCredentialsError("Unable to locate credentials")))
    with pytest.raises(llm.ProviderUnavailable):
        llm._bedrock_json("m-a", "p")


def test_a_picture_is_checked_through_converse(bedrock):
    client = bedrock(FakeBedrock('{"shows": "a diagram", "helps": true}'))
    assert llm.check_image(b"\xff\xd8jpeg", "card", "query") == (True, "a diagram")
    block = client.calls[0]["messages"][0]["content"][1]
    assert block["image"]["source"]["bytes"] == b"\xff\xd8jpeg"
