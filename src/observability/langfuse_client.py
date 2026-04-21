from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Generator

from src.llm.base import LLMClient

logger = logging.getLogger(__name__)

try:
    from langfuse import Langfuse
    from langfuse.types import TraceContext
    _LANGFUSE_AVAILABLE = True
except ImportError:
    logger.warning("langfuse package is not installed. Observability will be disabled.")
    _LANGFUSE_AVAILABLE = False

# LangGraph callback handler requires 'langchain' (not just langchain-core).
try:
    from langfuse.langchain import CallbackHandler as _LangfuseCallbackHandler
    _CALLBACK_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    _LangfuseCallbackHandler = None  # type: ignore[assignment,misc]
    _CALLBACK_AVAILABLE = False


def _estimate_tokens(x: Any) -> int:
    return int(len(str(x).split()) * 1.3)


class ObservabilityClient:
    """Manages Langfuse connection, provides trace context manager and LLM wrapper.

    Compatible with Langfuse v4+ (start_as_current_observation / TraceContext API).

    Usage:
        obs = ObservabilityClient(config["langfuse"])
        llm = obs.wrap_llm(raw_llm)

        with obs.start_trace(session_id, "agent-query", input={"query": q}) as trace:
            result = agent.invoke(state)
            if trace:
                trace.update(output={"response": result.get("response")})
    """

    def __init__(self, config: dict):
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

    @contextmanager
    def start_trace(
        self,
        session_id: str,
        name: str = "agent-query",
        input: Any = None,
        metadata: dict | None = None,
    ) -> Generator[Any, None, None]:
        """Context manager that creates a root trace observation.

        All LLM generations executed inside this block are automatically
        nested as children via Langfuse's OpenTelemetry context propagation.

        Yields the root observation (or None when disabled).

        Example:
            with obs.start_trace(session_id, "agent-query", input={"q": q}) as root:
                result = agent.invoke(state)
                if root:
                    root.update(output={"response": result["response"]})
        """
        if not self._enabled or self._langfuse is None:
            yield None
            return

        trace_id = self._langfuse.create_trace_id(seed=session_id)
        trace_ctx = TraceContext(trace_id=trace_id)

        with self._langfuse.start_as_current_observation(
            trace_context=trace_ctx,
            name=name,
            as_type="agent",
            input=input,
            metadata={**(metadata or {}), "session_id": session_id},
        ) as root_obs:
            try:
                yield root_obs
            except Exception as exc:
                root_obs.update(level="ERROR", status_message=str(exc))
                raise
            finally:
                self._langfuse.flush()

    def get_handler(
        self,
        session_id: str,
        trace_name: str = "agent-query",
        user_id: str | None = None,
        metadata: dict | None = None,
    ) -> Any | None:
        """Return LangGraph CallbackHandler (requires langchain); None when unavailable.

        Kept for backward-compatibility. Prefer start_trace() for explicit trace control.
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
        """Return a generation-tracking wrapper around llm; returns llm unchanged when disabled."""
        if not self._enabled or self._langfuse is None:
            return llm
        return LangfuseTrackedLLMClient(llm, self._langfuse)  # type: ignore[return-value]

    def flush(self) -> None:
        """Force-flush pending events (call before process exit)."""
        if self._langfuse:
            self._langfuse.flush()


class LangfuseTrackedLLMClient:
    """Wraps any LLMClient, recording each chat() call as a Langfuse generation.

    When called inside an obs.start_trace() context, the generation is
    automatically nested under the parent trace via OTel context propagation.
    """

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
