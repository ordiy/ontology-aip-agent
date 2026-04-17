"""Ollama local LLM client.

Implements the LLMClient protocol using Ollama's REST API.
Ollama runs locally and serves open-source models (llama3, mistral, qwen2, etc.)
via a simple HTTP API compatible with this interface.

API reference: https://github.com/ollama/ollama/blob/main/docs/api.md#generate-a-chat-completion
"""

import json
import urllib.request
import urllib.error
from src.llm.base import LLMClient


class OllamaConnectionError(Exception):
    """Raised when Ollama server is unreachable."""
    pass


class OllamaClient:
    """LLM client for Ollama local model server.

    Uses Ollama's /api/chat REST endpoint to send multi-turn conversations.
    No external dependencies — uses only Python's stdlib urllib.

    Args:
        host: Ollama server URL (default: http://localhost:11434)
        model_name: Model to use (e.g., 'llama3', 'mistral', 'qwen2')
        timeout: HTTP request timeout in seconds
    """

    def __init__(
        self,
        host: str = "http://localhost:11434",
        model_name: str = "llama3",
        timeout: int = 120,
    ):
        # Strip trailing slash for consistent URL construction
        self._host = host.rstrip("/")
        self._model_name = model_name
        self._timeout = timeout

    def chat(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        temperature: float = 0.0,
    ) -> str:
        """Send a chat request to Ollama and return the response text.

        Ollama's /api/chat accepts OpenAI-compatible message format.
        System prompts are prepended as a {"role": "system", ...} message.

        Args:
            messages: List of {"role": "user"|"assistant", "content": str} dicts
            system_prompt: Optional system instruction prepended to the conversation
            temperature: Sampling temperature (0.0 = deterministic)

        Returns:
            The model's response text

        Raises:
            OllamaConnectionError: If the Ollama server is unreachable
            RuntimeError: If the API returns an unexpected error
        """
        # Build the full message list: system (if any) + conversation
        all_messages = []
        if system_prompt:
            all_messages.append({"role": "system", "content": system_prompt})

        # Convert our role format to Ollama's format
        # Our "model" role → Ollama's "assistant" role
        for msg in messages:
            role = "assistant" if msg["role"] == "model" else msg["role"]
            all_messages.append({"role": role, "content": msg["content"]})

        payload = {
            "model": self._model_name,
            "messages": all_messages,
            "stream": False,  # Get complete response at once, not streamed
            "options": {
                "temperature": temperature,
            },
        }

        url = f"{self._host}/api/chat"
        request_body = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=request_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as response:
                response_data = json.loads(response.read().decode("utf-8"))
                # Ollama response: {"message": {"role": "assistant", "content": "..."}, ...}
                return response_data["message"]["content"]
        except urllib.error.URLError as e:
            raise OllamaConnectionError(
                f"Cannot connect to Ollama at {self._host}. "
                f"Is Ollama running? Start it with: ollama serve\n"
                f"Original error: {e}"
            ) from e
        except (KeyError, json.JSONDecodeError) as e:
            raise RuntimeError(f"Unexpected Ollama API response format: {e}") from e

    def get_model_name(self) -> str:
        """Return the model name this client is configured to use."""
        return self._model_name
