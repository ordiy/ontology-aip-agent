from src.ontology.parser import OntologySchema, OntologyClass, OntologyProperty, OntologyRelationship
from src.ontology.context import generate_context


def _make_schema():
    return OntologySchema(
        domain="Test Store",
        classes=[
            OntologyClass(
                name="Customer",
                properties=[
                    OntologyProperty(name="name", data_type="string"),
                    OntologyProperty(name="email", data_type="string"),
                    OntologyProperty(name="age", data_type="integer"),
                ],
            ),
            OntologyClass(
                name="Order",
                properties=[
                    OntologyProperty(name="orderId", data_type="string", is_identifier=True),
                    OntologyProperty(name="total", data_type="float"),
                    OntologyProperty(name="orderDate", data_type="date"),
                ],
            ),
        ],
        relationships=[
            OntologyRelationship(source="Customer", target="Order", name="places", cardinality="one-to-many"),
        ],
    )


def test_context_contains_domain():
    schema = _make_schema()
    ctx = generate_context(schema)
    assert "Domain: Test Store" in ctx


def test_context_contains_table_names():
    schema = _make_schema()
    ctx = generate_context(schema)
    assert "customers" in ctx
    assert "orders" in ctx


def test_context_contains_column_definitions():
    schema = _make_schema()
    ctx = generate_context(schema)
    assert "name (TEXT)" in ctx
    assert "age (INTEGER)" in ctx
    assert "total (REAL)" in ctx


def test_context_contains_primary_key():
    schema = _make_schema()
    ctx = generate_context(schema)
    assert "id (INTEGER PK)" in ctx


def test_context_contains_foreign_key():
    schema = _make_schema()
    ctx = generate_context(schema)
    assert "customer_id" in ctx
    assert "FK" in ctx


def test_context_contains_relationships():
    schema = _make_schema()
    ctx = generate_context(schema)
    assert "customers 1:N orders" in ctx
