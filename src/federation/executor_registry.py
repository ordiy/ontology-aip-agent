"""Executor Registry for the Federation Layer.

Maps query engine names (e.g., 'sqlite', 'starrocks') to BaseExecutor instances.
"""

from src.database.executor import BaseExecutor


class ExecutorRegistry:
    """Maps engine name to a BaseExecutor instance."""

    def __init__(self, executors: dict[str, BaseExecutor], default_engine: str = "sqlite") -> None:
        """Initialize the registry with a dict of executors and a default engine.

        Args:
            executors: A dictionary mapping engine names to BaseExecutor instances.
            default_engine: The name of the default engine to fallback to if no mapping is found.
        """
        self._executors = executors
        self._default_engine = default_engine
        
        if default_engine not in self._executors:
            raise KeyError(f"Default engine '{default_engine}' not found in registered executors: {list(self._executors.keys())}")

    def get(self, engine: str) -> BaseExecutor:
        """Get an executor by engine name.

        Args:
            engine: The name of the engine to retrieve.

        Returns:
            The BaseExecutor instance for the specified engine.

        Raises:
            KeyError: If the engine is not registered.
        """
        if engine not in self._executors:
            raise KeyError(f"Engine '{engine}' not found in ExecutorRegistry. Available engines: {self.engines}")
        return self._executors[engine]

    def default(self) -> BaseExecutor:
        """Get the default executor instance.

        Returns:
            The default BaseExecutor.
        """
        return self._executors[self._default_engine]

    @property
    def engines(self) -> list[str]:
        """List of registered engine names.

        Returns:
            A list of engine names registered in the registry.
        """
        return list(self._executors.keys())
