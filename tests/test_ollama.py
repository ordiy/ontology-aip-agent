"""Tests for OllamaClient.

All tests use mocked HTTP responses since Ollama is not available in this
environment. Tests verify the client correctly formats requests and parses
responses per the Ollama /api/chat API spec.
"""
import json
import pytest
from unittest.mock import patch, MagicMock
from src.llm.ollama import OllamaClient, OllamaConnectionError
import urllib.error


class MockHTTPResponse:
    """Minimal mock for urllib HTTP response context manager."""

    def __init__(self, data: dict):
        self._data = json.dumps(data).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _mock_urlopen(response_data: dict):
    """Return a context manager mock that yields MockHTTPResponse."""
    return MockHTTPResponse(response_data)


def test_chat_basic_response():
    """OllamaClient.chat() should return the 'content' field from Ollama's response."""
    client = OllamaClient(host="http://localhost:11434", model_name="llama3")

    fake_response = {"message": {"role": "assistant", "content": "Hello, world!"}}

    with patch("urllib.request.urlopen", return_value=_mock_urlopen(fake_response)):
        result = client.chat([{"role": "user", "content": "hi"}])

    assert result == "Hello, world!"


def test_chat_includes_system_prompt():
    """OllamaClient.chat() should prepend system message when system_prompt is given."""
    client = OllamaClient(model_name="llama3")

    captured_payload = {}

    def fake_urlopen(req, timeout=None):
        # Capture the request body to verify it includes the system message
        captured_payload.update(json.loads(req.data.decode("utf-8")))
        fake_response = {"message": {"role": "assistant", "content": "ok"}}
        return _mock_urlopen(fake_response)

    with patch("urllib.request.urlopen", fake_urlopen):
        client.chat(
            [{"role": "user", "content": "hello"}],
            system_prompt="You are a helpful assistant."
        )

    messages = captured_payload.get("messages", [])
    # First message should be the system prompt
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "You are a helpful assistant."


def test_chat_converts_model_role_to_assistant():
    """OllamaClient should convert 'model' role to 'assistant' for Ollama API compatibility."""
    client = OllamaClient(model_name="llama3")

    captured_payload = {}

    def fake_urlopen(req, timeout=None):
        captured_payload.update(json.loads(req.data.decode("utf-8")))
        return _mock_urlopen({"message": {"role": "assistant", "content": "ok"}})

    with patch("urllib.request.urlopen", fake_urlopen):
        client.chat([
            {"role": "user", "content": "hi"},
            {"role": "model", "content": "hello"},  # Our internal 'model' role
            {"role": "user", "content": "bye"},
        ])

    messages = captured_payload["messages"]
    roles = [m["role"] for m in messages]
    # 'model' should be converted to 'assistant'
    assert "model" not in roles
    assert "assistant" in roles


def test_chat_raises_connection_error_when_server_unreachable():
    """OllamaClient should raise OllamaConnectionError when server is down."""
    client = OllamaClient(host="http://localhost:11434", model_name="llama3")

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
        with pytest.raises(OllamaConnectionError) as exc_info:
            client.chat([{"role": "user", "content": "hi"}])

    assert "ollama serve" in str(exc_info.value).lower() or "ollama" in str(exc_info.value).lower()


def test_get_model_name():
    """get_model_name() should return the configured model name."""
    client = OllamaClient(model_name="mistral")
    assert client.get_model_name() == "mistral"


def test_chat_temperature_passed_to_options():
    """Temperature should be passed in the 'options' field of the Ollama request."""
    client = OllamaClient(model_name="llama3")
    captured_payload = {}

    def fake_urlopen(req, timeout=None):
        captured_payload.update(json.loads(req.data.decode("utf-8")))
        return _mock_urlopen({"message": {"role": "assistant", "content": "ok"}})

    with patch("urllib.request.urlopen", fake_urlopen):
        client.chat([{"role": "user", "content": "hi"}], temperature=0.7)

    assert captured_payload["options"]["temperature"] == 0.7


def test_chat_uses_non_streaming_mode():
    """Ollama request must set stream=False to get a complete response at once."""
    client = OllamaClient(model_name="llama3")
    captured_payload = {}

    def fake_urlopen(req, timeout=None):
        captured_payload.update(json.loads(req.data.decode("utf-8")))
        return _mock_urlopen({"message": {"role": "assistant", "content": "ok"}})

    with patch("urllib.request.urlopen", fake_urlopen):
        client.chat([{"role": "user", "content": "hi"}])

    # stream=False is required — streaming mode sends line-delimited JSON
    # which our simple response parser doesn't handle
    assert captured_payload.get("stream") is False