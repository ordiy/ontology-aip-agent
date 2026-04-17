import sqlite3
from src.ontology.parser import OntologySchema
from src.ontology.context import table_name, _fk_col_name


def _q(identifier: str) -> str:
    """Quote a SQL identifier with double quotes, escaping internal quotes."""
    return '"' + identifier.replace('"', '""') + '"'


_TYPE_TO_SQLITE = {
    "string": "TEXT",
    "integer": "INTEGER",
    "float": "REAL",
    "date": "TEXT",
    "datetime": "TEXT",
    "boolean": "INTEGER",
}


def create_tables(db_path: str, schema: OntologySchema) -> dict[str, str]:
    """Create SQLite tables from ontology schema. Returns class_name -> table_name mapping."""
    class_to_table = {c.name: table_name(c.name) for c in schema.classes}

    # Determine FK columns from relationships
    # fk_additions[table] = [(fk_col_name, referenced_table)]
    fk_additions: dict[str, list[tuple[str, str]]] = {}
    for rel in schema.relationships:
        source_table = class_to_table.get(rel.source, "")
        target_table = class_to_table.get(rel.target, "")
        if not source_table or not target_table:
            continue  # skip relationships referencing unknown classes
        if rel.cardinality == "one-to-many":
            fk_col = _fk_col_name(source_table)
            fk_additions.setdefault(target_table, []).append((fk_col, source_table))
        elif rel.cardinality == "many-to-one":
            fk_col = _fk_col_name(target_table)
            fk_additions.setdefault(source_table, []).append((fk_col, target_table))

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        # Create entity tables
        for cls in schema.classes:
            tbl = class_to_table[cls.name]
            columns = ["id INTEGER PRIMARY KEY AUTOINCREMENT"]

            for fk_col, ref_table in fk_additions.get(tbl, []):
                columns.append(f"{_q(fk_col)} INTEGER REFERENCES {_q(ref_table)}(id)")

            for prop in cls.properties:
                sql_type = _TYPE_TO_SQLITE.get(prop.data_type, "TEXT")
                columns.append(f"{_q(prop.name)} {sql_type}")

            ddl = f"CREATE TABLE IF NOT EXISTS {_q(tbl)} ({', '.join(columns)})"
            conn.execute(ddl)

        # Create junction tables for M:N relationships
        for rel in schema.relationships:
            if rel.cardinality == "many-to-many":
                source_table = class_to_table.get(rel.source, "")
                target_table = class_to_table.get(rel.target, "")
                if not source_table or not target_table:
                    continue
                junction = f"{source_table}_{target_table}"
                src_fk = _fk_col_name(source_table)
                tgt_fk = _fk_col_name(target_table)
                ddl = (
                    f"CREATE TABLE IF NOT EXISTS {_q(junction)} ("
                    f"id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    f"{_q(src_fk)} INTEGER REFERENCES {_q(source_table)}(id), "
                    f"{_q(tgt_fk)} INTEGER REFERENCES {_q(target_table)}(id))"
                )
                conn.execute(ddl)

        conn.commit()
    finally:
        conn.close()
    return class_to_table
