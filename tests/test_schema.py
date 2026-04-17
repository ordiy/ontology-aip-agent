import sqlite3
from src.ontology.parser import OntologySchema, OntologyClass, OntologyProperty, OntologyRelationship
from src.database.schema import create_tables


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


def test_creates_tables_for_each_class(tmp_path):
    db_path = str(tmp_path / "test.db")
    schema = _make_schema()
    create_tables(db_path, schema)

    conn = sqlite3.connect(db_path)
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()

    assert "customers" in tables
    assert "orders" in tables
    assert "products" in tables


def test_creates_junction_table_for_many_to_many(tmp_path):
    db_path = str(tmp_path / "test.db")
    schema = _make_schema()
    create_tables(db_path, schema)

    conn = sqlite3.connect(db_path)
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()

    assert "orders_products" in tables


def test_table_has_id_primary_key(tmp_path):
    db_path = str(tmp_path / "test.db")
    schema = _make_schema()
    create_tables(db_path, schema)

    conn = sqlite3.connect(db_path)
    cursor = conn.execute("PRAGMA table_info(customers)")
    cols = {row[1]: row for row in cursor.fetchall()}
    conn.close()

    assert "id" in cols
    assert cols["id"][5] == 1  # pk flag


def test_table_has_correct_columns(tmp_path):
    db_path = str(tmp_path / "test.db")
    schema = _make_schema()
    create_tables(db_path, schema)

    conn = sqlite3.connect(db_path)
    cursor = conn.execute("PRAGMA table_info(customers)")
    col_names = {row[1] for row in cursor.fetchall()}
    conn.close()

    assert col_names == {"id", "name", "email", "age"}


def test_foreign_key_column_on_many_side(tmp_path):
    db_path = str(tmp_path / "test.db")
    schema = _make_schema()
    create_tables(db_path, schema)

    conn = sqlite3.connect(db_path)
    cursor = conn.execute("PRAGMA table_info(orders)")
    col_names = {row[1] for row in cursor.fetchall()}
    conn.close()

    assert "customer_id" in col_names


def test_junction_table_has_both_foreign_keys(tmp_path):
    db_path = str(tmp_path / "test.db")
    schema = _make_schema()
    create_tables(db_path, schema)

    conn = sqlite3.connect(db_path)
    cursor = conn.execute("PRAGMA table_info(orders_products)")
    col_names = {row[1] for row in cursor.fetchall()}
    conn.close()

    assert "order_id" in col_names
    assert "product_id" in col_names
