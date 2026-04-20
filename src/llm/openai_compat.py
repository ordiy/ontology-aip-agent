"""OpenAI-compatible LLM client for OpenAI and OpenRouter.

Both OpenAI and OpenRouter expose the same chat-completion REST API.
This single client handles both by accepting a configurable base_url:

  OpenAI:      base_url = "https://api.openai.com/v1"
  OpenRouter:  base_url = "https://openrouter.ai/api/v1"

OpenRouter gives access to 200+ models (Claude, Gemini, Llama, Mistral, etc.)
under one API key, which is useful for multi-model experiments.

API references:
  https://platform.openai.com/docs/api-reference/chat
  https://openrouter.ai/docs
"""

from src.llm.base import LLMClient

_OPENAI_BASE_URL = "https://api.openai.com/v1"
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenAICompatClient:
    """LLM client for OpenAI API and any OpenAI-compatible endpoint (OpenRouter, etc.).

    Uses the official `openai` Python SDK, which supports custom base_url so
    the same code path works for both services.

    Args:
        api_key:       API key for the provider.
        model_name:    Model identifier (e.g. "gpt-4o", "anthropic/claude-3.5-sonnet").
        base_url:      API base URL. Defaults to OpenAI. Pass OpenRouter URL for that provider.
        provider_name: Human-readable name shown in logs/errors (e.g. "OpenAI", "OpenRouter").
        extra_headers: Additional HTTP headers sent with every request.
                       OpenRouter recommends "HTTP-Referer" and "X-Title" for rate-limit tiers.
    """

    def __init__(
        self,
        api_key: str,
        model_name: str,
        base_url: str = _OPENAI_BASE_URL,
        provider_name: str = "OpenAI",
        extra_headers: dict | None = None,
    ):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError(
                "openai package is required. Install it with: pip install openai"
            ) from e

        self._model_name = model_name
        self._provider_name = provider_name

        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=extra_headers or {},
        )

    def chat(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        temperature: float = 0.0,
    ) -> str:
        """Send a chat-completion request and return the response text.

        Converts internal "model" role (Gemini convention used in nodes.py) to
        "assistant" before sending, so this client is a drop-in replacement for
        VertexGeminiClient and OllamaClient.

        Args:
            messages:      Conversation history with role/content dicts.
                           Accepted roles: "user", "assistant", "model" (converted to "assistant").
            system_prompt: Optional system instruction prepended to the request.
            temperature:   Sampling temperature (0.0 = deterministic).

        Returns:
            The model's response as a plain string.

        Raises:
            RuntimeError: On API error or unexpected response format.
        """
        all_messages = []

        if system_prompt:
            all_messages.append({"role": "system", "content": system_prompt})

        # Normalize "model" role (Gemini convention) → "assistant" (OpenAI convention)
        for msg in messages:
            role = "assistant" if msg["role"] == "model" else msg["role"]
            all_messages.append({"role": role, "content": msg["content"]})

        try:
            response = self._client.chat.completions.create(
                model=self._model_name,
                messages=all_messages,
                temperature=temperature,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            raise RuntimeError(
                f"{self._provider_name} API error ({self._model_name}): {e}"
            ) from e

    def get_model_name(self) -> str:
        """Return the model name this client is configured to use."""
        return self._model_name
