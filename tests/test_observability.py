import pytest
from unittest.mock import MagicMock

from src.observability.langfuse_client import (
    ObservabilityClient,
    LangfuseTrackedLLMClient,
    _estimate_tokens,
)


class FakeLLM:
    def __init__(self, responses: list[str] = None, exc_to_raise: Exception = None):
        self._responses = responses or []
        self._call_index = 0
        self._exc = exc_to_raise

    def chat(self, messages, system_prompt=None, temperature=0.0):
        if self._exc:
            raise self._exc
        resp = self._responses[self._call_index]
        self._call_index += 1
        return resp

    def get_model_name(self):
        return "fake-model"


def test_disabled_returns_none_handler():
    obs = ObservabilityClient({"enabled": False})
    assert obs.get_handler("session-1") is None


def test_disabled_wrap_llm_returns_original():
    obs = ObservabilityClient({"enabled": False})
    fake_llm = FakeLLM(["hello"])
    assert obs.wrap_llm(fake_llm) is fake_llm


def test_tracked_llm_forwards_chat():
    mock_langfuse = MagicMock()
    mock_gen = MagicMock()
    mock_langfuse.generation.return_value = mock_gen

    client = LangfuseTrackedLLMClient(FakeLLM(["response"]), mock_langfuse)
    result = client.chat([{"role": "user", "content": "hi"}])

    assert result == "response"
    mock_langfuse.generation.assert_called_once()
    mock_gen.end.assert_called_once()


def test_tracked_llm_records_error_on_exception():
    mock_langfuse = MagicMock()
    mock_gen = MagicMock()
    mock_langfuse.generation.return_value = mock_gen

    exc = RuntimeError("test error")
    client = LangfuseTrackedLLMClient(FakeLLM(exc_to_raise=exc), mock_langfuse)

    with pytest.raises(RuntimeError, match="test error"):
        client.chat([{"role": "user", "content": "hi"}])

    mock_langfuse.generation.assert_called_once()
    mock_gen.end.assert_called_once()
    call_kwargs = mock_gen.end.call_args[1]
    assert call_kwargs.get("level") == "ERROR"
    assert call_kwargs.get("status_message") == "test error"


def test_estimate_tokens():
    result = _estimate_tokens("hello world")
    assert isinstance(result, int)
    assert result > 0
