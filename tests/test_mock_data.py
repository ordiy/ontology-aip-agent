import sqlite3
from src.ontology.parser import OntologySchema, OntologyClass, OntologyProperty, OntologyRelationship
from src.database.schema import create_tables
from src.database.mock_data import generate_mock_data


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
            OntologyClass(
                name="Product",
                properties=[
                    OntologyProperty(name="name", data_type="string"),
                    OntologyProperty(name="price", data_type="float"),
                ],
            ),
        ],
        relationships=[
            OntologyRelationship(source="Customer", target="Order", name="places", cardinality="one-to-many"),
            OntologyRelationship(source="Order", target="Product", name="contains", cardinality="many-to-many"),
        ],
    )


def test_generates_rows_in_each_table(tmp_path):
    db_path = str(tmp_path / "test.db")
    schema = _make_schema()
    create_tables(db_path, schema)
    generate_mock_data(db_path, schema, rows_per_table=20)

    conn = sqlite3.connect(db_path)
    for table in ["customers", "orders", "products"]:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert count == 20, f"Expected 20 rows in {table}, got {count}"
    conn.close()


def test_generates_junction_table_rows(tmp_path):
    db_path = str(tmp_path / "test.db")
    schema = _make_schema()
    create_tables(db_path, schema)
    generate_mock_data(db_path, schema, rows_per_table=20)

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM orders_products").fetchone()[0]
    assert count > 0
    conn.close()


def test_foreign_keys_reference_existing_rows(tmp_path):
    db_path = str(tmp_path / "test.db")
    schema = _make_schema()
    create_tables(db_path, schema)
    generate_mock_data(db_path, schema, rows_per_table=20)

    conn = sqlite3.connect(db_path)
    orphans = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE customer_id NOT IN (SELECT id FROM customers)"
    ).fetchone()[0]
    assert orphans == 0
    conn.close()


def test_data_types_are_correct(tmp_path):
    db_path = str(tmp_path / "test.db")
    schema = _make_schema()
    create_tables(db_path, schema)
    generate_mock_data(db_path, schema, rows_per_table=10)

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT name, email, age FROM customers LIMIT 1").fetchone()
    assert isinstance(row[0], str)
    assert isinstance(row[1], str)
    assert isinstance(row[2], int)

    row = conn.execute("SELECT total FROM orders LIMIT 1").fetchone()
    assert isinstance(row[0], (int, float))
    conn.close()
