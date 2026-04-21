import logging
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)


class VertexGeminiClient:
    """LLM client backed by Google Cloud Vertex AI using the google-genai SDK.

    Replaces the deprecated vertexai.generative_models API (removed June 2026).
    Credentials priority: credentials_path arg → GOOGLE_APPLICATION_CREDENTIALS env var → ADC.
    """

    def __init__(
        self,
        project: str,
        location: str,
        model_name: str,
        credentials_path: str = "",
    ):
        client_kwargs: dict = {"vertexai": True, "project": project, "location": location}

        if credentials_path:
            from google.oauth2 import service_account
            client_kwargs["credentials"] = service_account.Credentials.from_service_account_file(
                credentials_path,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )

        self._client = genai.Client(**client_kwargs)
        self._model_name = model_name
        logger.info("VertexGeminiClient initialized (model=%s, project=%s)", model_name, project)

    def chat(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        temperature: float = 0.0,
    ) -> str:
        contents = [
            types.Content(
                role="user" if msg["role"] == "user" else "model",
                parts=[types.Part(text=msg["content"])],
            )
            for msg in messages
        ]

        config = types.GenerateContentConfig(
            max_output_tokens=8192,
            temperature=temperature,
            system_instruction=system_prompt or None,
        )

        response = self._client.models.generate_content(
            model=self._model_name,
            contents=contents,
            config=config,
        )
        return response.text or ""

    def get_model_name(self) -> str:
        return self._model_name
