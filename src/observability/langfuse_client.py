from __future__ import annotations

import logging
import time
from typing import Any

from src.llm.base import LLMClient

logger = logging.getLogger(__name__)

try:
    from langfuse import Langfuse
    _LANGFUSE_AVAILABLE = True
except ImportError:
    logger.warning("langfuse package is not installed. Observability will be disabled.")
    _LANGFUSE_AVAILABLE = False

# LangGraph callback handler requires 'langchain' (not just langchain-core).
# Try to import; gracefully degrade to None if unavailable.
try:
    from langfuse.langchain import CallbackHandler as _LangfuseCallbackHandler
    _CALLBACK_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    _LangfuseCallbackHandler = None  # type: ignore[assignment,misc]
    _CALLBACK_AVAILABLE = False


def _estimate_tokens(x: Any) -> int:
    return int(len(str(x).split()) * 1.3)


class ObservabilityClient:
    """管理 Langfuse 连接，提供 handler 和 LLM 包装器。

    兼容 Langfuse v4+ (start_observation API)。
    LangGraph CallbackHandler 在 langchain 可用时自动启用，否则跳过节点追踪
    但保留 LLM generation 追踪。
    """

    def __init__(self, config: dict):
        """
        Args:
            config: 来自 config.yaml 的 langfuse 节，含 enabled/public_key/secret_key/host
        """
        self._config = config
        self._enabled = bool(config.get("enabled", False))
        self._langfuse: Any = None

        if self._enabled:
            if not _LANGFUSE_AVAILABLE:
                logger.warning("langfuse not installed; disabling observability.")
                self._enabled = False
            else:
                try:
                    self._langfuse = Langfuse(
                        public_key=config.get("public_key"),
                        secret_key=config.get("secret_key"),
                        host=config.get("host", "https://cloud.langfuse.com"),
                    )
                    logger.info("Langfuse observability enabled (host=%s)", config.get("host"))
                    if not _CALLBACK_AVAILABLE:
                        logger.info(
                            "LangGraph node tracing disabled (langchain not installed). "
                            "LLM generation tracing is active."
                        )
                except Exception as exc:
                    logger.warning("Failed to initialize Langfuse: %s", exc)
                    self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def get_handler(
        self,
        session_id: str,
        trace_name: str = "agent-query",
        user_id: str | None = None,
        metadata: dict | None = None,
    ) -> Any | None:
        """返回 LangGraph CallbackHandler（需要 langchain）；不可用时返回 None。

        None 时 agent.invoke(state, config={}) 仍正常运行，只是无节点级追踪。
        LLM generation 追踪由 wrap_llm() 独立保证。
        """
        if not self._enabled or not _CALLBACK_AVAILABLE:
            return None
        try:
            return _LangfuseCallbackHandler(
                public_key=self._config.get("public_key"),
                secret_key=self._config.get("secret_key"),
                host=self._config.get("host", "https://cloud.langfuse.com"),
                session_id=session_id,
                trace_name=trace_name,
                user_id=user_id,
                metadata=metadata or {},
            )
        except Exception as exc:
            logger.warning("Failed to create Langfuse CallbackHandler: %s", exc)
            return None

    def wrap_llm(self, llm: LLMClient) -> LLMClient:
        """返回带 generation 追踪的包装 LLM；disabled 时原样返回。"""
        if not self._enabled or self._langfuse is None:
            return llm
        return LangfuseTrackedLLMClient(llm, self._langfuse)  # type: ignore[return-value]

    def flush(self) -> None:
        """强制刷新待发送的事件（进程退出前调用）。"""
        if self._langfuse:
            self._langfuse.flush()


class LangfuseTrackedLLMClient:
    """包装任意 LLMClient，使用 Langfuse v4 start_observation API 追踪每次 chat() 调用。"""

    def __init__(self, inner: LLMClient, langfuse_instance: Any):
        self._inner = inner
        self._langfuse = langfuse_instance

    def chat(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        temperature: float = 0.0,
    ) -> str:
        obs = self._langfuse.start_observation(
            name="llm-chat",
            as_type="generation",
            model=self._inner.get_model_name(),
            input={"messages": messages, "system_prompt": system_prompt},
            model_parameters={"temperature": temperature},
        )
        try:
            output = self._inner.chat(messages, system_prompt, temperature)
            obs.update(
                output=output,
                usage_details={
                    "input": _estimate_tokens(messages),
                    "output": _estimate_tokens(output),
                },
            )
            obs.end()
            return output
        except Exception as exc:
            obs.update(level="ERROR", status_message=str(exc))
            obs.end()
            raise

    def get_model_name(self) -> str:
        return self._inner.get_model_name()
