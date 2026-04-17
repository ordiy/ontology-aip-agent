from google.cloud import aiplatform
from vertexai.generative_models import GenerativeModel, Content, Part
from src.llm.base import LLMClient


class VertexGeminiClient:
    def __init__(self, project: str, location: str, model_name: str):
        aiplatform.init(project=project, location=location)
        self._model_name = model_name
        self._model = GenerativeModel(model_name)

    def chat(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        temperature: float = 0.0,
    ) -> str:
        # system_instruction must be passed at model construction, not generate_content
        if system_prompt:
            model = GenerativeModel(self._model_name, system_instruction=system_prompt)
        else:
            model = self._model

        contents = []
        for msg in messages:
            role = "user" if msg["role"] == "user" else "model"
            contents.append(Content(role=role, parts=[Part.from_text(msg["content"])]))

        generation_config = {"temperature": temperature, "max_output_tokens": 2048}

        response = model.generate_content(
            contents,
            generation_config=generation_config,
        )
        return response.text

    def get_model_name(self) -> str:
        return self._model_name
