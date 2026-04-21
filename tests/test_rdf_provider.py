import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.ontology.provider import OntologyProvider, OntologyContext, PhysicalMapping
from src.ontology.rdf_provider import RDFOntologyProvider


class MockOntologyProvider(OntologyProvider):
    def __init__(self, schema_text: str = "Table: orders\n  Columns: id(integer) PK"):
        self._schema = schema_text
        self.load_call_count = 0

    def load(self) -> OntologyContext:
        self.load_call_count += 1
        return OntologyContext(
            schema_for_llm=self._schema,
            rules={},
            physical_mappings={},
        )


@pytest.fixture
def retail_rdf_path():
    return Path(__file__).parent.parent / "ontologies" / "retail.rdf"


def test_load_physical_mappings(retail_rdf_path):
    if not retail_rdf_path.exists():
        pytest.skip(f"{retail_rdf_path} not found")

    provider = RDFOntologyProvider([str(retail_rdf_path)])
    ctx = provider.load()

    assert "Order" in ctx.physical_mappings
    mapping = ctx.physical_mappings["Order"]
    assert mapping.physical_table == "iceberg_catalog.retail.orders"
    assert mapping.query_engine == "starrocks"
    assert mapping.partition_keys == ["order_date"]


def test_schema_for_llm_contains_physical_table(retail_rdf_path):
    """StarRocks dialect renders full physical table names; SQLite uses simple names."""
    if not retail_rdf_path.exists():
        pytest.skip(f"{retail_rdf_path} not found")

    # StarRocks dialect → physical Iceberg table names
    provider_sr = RDFOntologyProvider(
        [str(retail_rdf_path)], executor_dialect="MySQL (StarRocks-compatible)"
    )
    ctx_sr = provider_sr.load()
    assert "iceberg_catalog.retail.orders" in ctx_sr.schema_for_llm
    assert "Table: iceberg_catalog.retail.orders  -- entity: Order" in ctx_sr.schema_for_llm

    # SQLite dialect (default) → simple snake_case table names
    provider_sq = RDFOntologyProvider([str(retail_rdf_path)], executor_dialect="SQLite")
    ctx_sq = provider_sq.load()
    assert "iceberg_catalog.retail.orders" not in ctx_sq.schema_for_llm
    assert "Table: orders  -- entity: Order" in ctx_sq.schema_for_llm


def test_fallback_without_physical_table(tmp_path):
    rdf_content = """<?xml version="1.0"?>
    <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
             xmlns:owl="http://www.w3.org/2002/07/owl#"
             xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
        <owl:Class rdf:about="http://example.org/TestEntity">
            <rdfs:label>TestEntity</rdfs:label>
        </owl:Class>
    </rdf:RDF>
    """
    rdf_file = tmp_path / "test.rdf"
    rdf_file.write_text(rdf_content)

    dummy_class = MagicMock()
    dummy_class.name = "TestEntity"
    dummy_class.properties = []

    dummy_schema = MagicMock()
    dummy_schema.domain = "TestDomain"
    dummy_schema.classes = [dummy_class]
    dummy_schema.relationships = []
    dummy_schema.rules = {}

    with patch("src.ontology.rdf_provider.parse_ontology", return_value=dummy_schema):
        provider = RDFOntologyProvider([str(rdf_file)])
        ctx = provider.load()

    assert "TestEntity" not in ctx.physical_mappings
    # _sqlite_table_name("TestEntity") → "test_entities" (proper snake_case plural)
    assert "Table: test_entities  -- entity: TestEntity" in ctx.schema_for_llm


def test_context_lazy_cache():
    provider = MockOntologyProvider("Test Schema")

    ctx1 = provider.context
    assert provider.load_call_count == 1

    ctx2 = provider.context
    assert provider.load_call_count == 1

    assert ctx1 is ctx2


def test_mock_provider():
    provider = MockOntologyProvider("Table: mock_table\n  Columns: id(integer) PK")
    ctx = provider.context

    assert ctx.schema_for_llm == "Table: mock_table\n  Columns: id(integer) PK"
    assert ctx.rules == {}
    assert ctx.physical_mappings == {}
