from typing import Protocol


class LLMClient(Protocol):
    def chat(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        temperature: float = 0.0,
    ) -> str: ...

    def get_model_name(self) -> str: ...
