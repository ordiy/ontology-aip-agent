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
        contents = []
        for msg in messages:
            role = 'user' if msg['role'] == 'user' else 'model'
            contents.append(Content(role=role, parts=[Part.from_text(msg['content'])]))

        generation_config = {'temperature': temperature, 'max_output_tokens': 2048}

        response = self._model.generate_content(
            contents,
            generation_config=generation_config,
            system_instruction=system_prompt,
        )
        return response.text

    def get_model_name(self) -> str:
        return self._model_name
