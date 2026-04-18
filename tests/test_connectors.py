"""Tests for DataConnector abstraction and implementations."""
import pytest
from src.database.connectors import DataConnector, ExternalAPIConnector, MockMarketPriceConnector


def test_mock_connector_returns_full_catalogue_by_default():
    """MockMarketPriceConnector with no params returns all 5 catalogue items."""
    connector = MockMarketPriceConnector()
    results = connector.fetch({})
    assert len(results) == 5
    assert all("product_id" in r for r in results)
    assert all("market_price" in r for r in results)


def test_mock_connector_filters_by_product_ids():
    """MockMarketPriceConnector respects product_ids filter."""
    connector = MockMarketPriceConnector()
    results = connector.fetch({"product_ids": [1, 3]})
    assert len(results) == 2
    ids = [r["product_id"] for r in results]
    assert 1 in ids
    assert 3 in ids


def test_mock_connector_deterministic_prices():
    """MockMarketPriceConnector returns same price for same id across calls."""
    connector = MockMarketPriceConnector()
    r1 = connector.fetch({"product_ids": [2]})
    r2 = connector.fetch({"product_ids": [2]})
    assert r1[0]["market_price"] == r2[0]["market_price"]


def test_mock_connector_unknown_id_returns_row():
    """MockMarketPriceConnector returns a row (not error) for unknown product ids."""
    connector = MockMarketPriceConnector()
    results = connector.fetch({"product_ids": [999]})
    assert len(results) == 1
    assert results[0]["product_id"] == 999
    assert "market_price" in results[0]
    assert "error" not in results[0]


def test_mock_connector_name():
    """MockMarketPriceConnector.name() returns a non-empty string."""
    connector = MockMarketPriceConnector()
    assert connector.name() == "MockMarketPriceConnector"


def test_external_api_connector_name():
    """ExternalAPIConnector.name() includes the base URL."""
    connector = ExternalAPIConnector("https://api.example.com/v1")
    assert "api.example.com" in connector.name()


def test_external_api_connector_returns_error_row_on_network_failure():
    """ExternalAPIConnector.fetch() returns error row instead of raising on failure."""
    from unittest.mock import patch, MagicMock

    connector = ExternalAPIConnector("https://invalid.example.invalid")

    import requests
    with patch("requests.get", side_effect=requests.exceptions.ConnectionError("no network")):
        results = connector.fetch({"product_id": 1})

    assert len(results) == 1
    assert "error" in results[0]


def test_data_connector_is_abstract():
    """DataConnector cannot be instantiated directly — it's an ABC."""
    with pytest.raises(TypeError):
        DataConnector()  # type: ignore