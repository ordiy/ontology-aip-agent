"""DataConnector abstraction for fetching external data alongside ontology queries.

Provides a base class and concrete implementations for pulling data from
sources outside the SQLite database — external APIs, market feeds, etc.
The MockMarketPriceConnector allows demo and test usage without network access.
"""

from abc import ABC, abstractmethod
from typing import Any


class DataConnector(ABC):
    """Abstract base class for all external data connectors.

    Subclasses implement fetch() to retrieve data from their source.
    The returned list of dicts is compatible with the same row format
    used by SQLExecutor so results can be merged or displayed uniformly.
    """

    @abstractmethod
    def fetch(self, query_params: dict[str, Any]) -> list[dict]:
        """Fetch data using the given parameters.

        Args:
            query_params: Source-specific parameters (e.g. {"product_id": 42}).

        Returns:
            List of row dicts, same format as SQLExecutor results.
        """
        ...

    @abstractmethod
    def name(self) -> str:
        """Human-readable connector name for logging and UI display."""
        ...


class ExternalAPIConnector(DataConnector):
    """Connector that fetches JSON data from an external HTTP API.

    Uses the 'requests' library. Falls back gracefully if requests is not
    installed — returns an empty list with an error key instead of raising.

    Intended for real integrations (e.g. a price feed microservice).
    For testing/demo without network access, use MockMarketPriceConnector.
    """

    def __init__(self, base_url: str, timeout: int = 10):
        """
        Args:
            base_url: Root URL of the API, e.g. "https://api.example.com/v1"
            timeout: Request timeout in seconds.
        """
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def name(self) -> str:
        return f"ExternalAPIConnector({self._base_url})"

    def fetch(self, query_params: dict[str, Any]) -> list[dict]:
        """GET {base_url}/data with query_params as URL parameters.

        Returns parsed JSON list on success, or [{"error": "..."}] on failure.
        """
        try:
            import requests
        except ImportError:
            # requests not installed — return informative error row
            return [{"error": "requests library not installed"}]

        try:
            response = requests.get(
                f"{self._base_url}/data",
                params=query_params,
                timeout=self._timeout,
            )
            response.raise_for_status()
            data = response.json()
            # Normalize: API may return a list or a dict with a data key
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "data" in data:
                return data["data"]
            return [data]
        except Exception as exc:
            return [{"error": str(exc)}]


class MockMarketPriceConnector(DataConnector):
    """Mock connector that returns deterministic fake market prices.

    No network access required — useful for demos, tests, and local dev.
    Returns a price for each product_id in query_params["product_ids"],
    or a default catalogue of 5 products if no ids are specified.

    Prices are deterministic: price = base_price + (product_id % 7) * 10
    so the same id always returns the same price across calls.
    """

    # Static catalogue of demo products with base prices
    _CATALOGUE = {
        1: ("Widget A", 29.99),
        2: ("Widget B", 49.99),
        3: ("Gadget Pro", 99.99),
        4: ("Gadget Lite", 59.99),
        5: ("SuperTool", 149.99),
    }

    def name(self) -> str:
        return "MockMarketPriceConnector"

    def fetch(self, query_params: dict[str, Any]) -> list[dict]:
        """Return mock market prices.

        If query_params contains "product_ids" (list of ints), return prices
        for those IDs only. Otherwise return the full default catalogue.

        Args:
            query_params: Optional {"product_ids": [1, 2, 3]}

        Returns:
            List of {"product_id", "product_name", "market_price"} dicts.
        """
        requested_ids = query_params.get("product_ids", list(self._CATALOGUE.keys()))

        results = []
        for pid in requested_ids:
            pid = int(pid)
            if pid in self._CATALOGUE:
                name, base = self._CATALOGUE[pid]
            else:
                # Generate a plausible name+price for unknown IDs
                name = f"Product-{pid}"
                base = 19.99
            # Deterministic jitter so different IDs have different prices
            price = round(base + (pid % 7) * 10, 2)
            results.append({
                "product_id": pid,
                "product_name": name,
                "market_price": price,
            })
        return results
