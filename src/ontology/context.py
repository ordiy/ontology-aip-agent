import re
from src.ontology.parser import OntologySchema, OntologyRelationship


_TYPE_TO_SQL = {
    "string": "TEXT",
    "integer": "INTEGER",
    "float": "REAL",
    "date": "TEXT",
    "datetime": "TEXT",
    "boolean": "INTEGER",
}

_CARDINALITY_SHORT = {
    "one-to-one": "1:1",
    "one-to-many": "1:N",
    "many-to-one": "N:1",
    "many-to-many": "M:N",
}


def table_name(class_name: str) -> str:
    """Convert class name to snake_case table name (plural)."""
    # CamelCase → snake_case
    name = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", class_name)
    name = re.sub(r"(?<=[A-Z])([A-Z][a-z])", r"_\1", name)
    name = name.lower().replace("-", "_").replace(" ", "_")
    # Pluralize
    if name.endswith("y") and len(name) >= 2 and name[-2] not in "aeiou":
        # consonant+y → ies (e.g. category → categories)
        name = name[:-1] + "ies"
    elif name.endswith(("s", "sh", "ch", "x", "z")):
        # sibilant endings → add es (e.g. address → addresses, status → statuses)
        name = name + "es"
    else:
        name = name + "s"
    return name


def generate_context(schema: OntologySchema) -> str:
    class_to_table = {c.name: table_name(c.name) for c in schema.classes}
    table_names = ", ".join(class_to_table.values())

    lines = [
        f"Domain: {schema.domain}",
        f"Tables: {table_names}",
        "",
    ]

    # Build FK info from relationships
    # fk_columns[table] = list of FK column description strings
    fk_columns: dict[str, list[str]] = {}
    for rel in schema.relationships:
        source_table = class_to_table.get(rel.source, "")
        target_table = class_to_table.get(rel.target, "")
        if rel.cardinality == "one-to-many":
            fk_col = _fk_col_name(source_table)
            fk_columns.setdefault(target_table, []).append(f"{fk_col} (INTEGER FK->{source_table})")
        elif rel.cardinality == "many-to-one":
            fk_col = _fk_col_name(target_table)
            fk_columns.setdefault(source_table, []).append(f"{fk_col} (INTEGER FK->{target_table})")

    for cls in schema.classes:
        tbl = class_to_table[cls.name]
        cols = ["id (INTEGER PK)"]
        if tbl in fk_columns:
            cols.extend(fk_columns[tbl])
        for prop in cls.properties:
            sql_type = _TYPE_TO_SQL.get(prop.data_type, "TEXT")
            cols.append(f"{prop.name} ({sql_type})")
        lines.append(f"Table: {tbl}")
        lines.append(f"  Columns: {', '.join(cols)}")
        lines.append("")

    # M:N junction tables
    for rel in schema.relationships:
        if rel.cardinality == "many-to-many":
            source_table = class_to_table.get(rel.source, "")
            target_table = class_to_table.get(rel.target, "")
            junction = f"{source_table}_{target_table}"
            src_fk = _fk_col_name(source_table)
            tgt_fk = _fk_col_name(target_table)
            lines.append(f"Table: {junction} (junction)")
            lines.append(f"  Columns: id (INTEGER PK), {src_fk} (INTEGER FK->{source_table}), {tgt_fk} (INTEGER FK->{target_table})")
            lines.append("")

    # Relationships section
    if schema.relationships:
        lines.append("Relationships:")
        for rel in schema.relationships:
            source_table = class_to_table.get(rel.source, "")
            target_table = class_to_table.get(rel.target, "")
            short = _CARDINALITY_SHORT.get(rel.cardinality, rel.cardinality)
            if rel.cardinality == "many-to-many":
                junction = f"{source_table}_{target_table}"
                lines.append(f"  - {source_table} {short} {target_table} (via {junction})")
            elif rel.cardinality == "one-to-many":
                fk_col = _fk_col_name(source_table)
                lines.append(f"  - {source_table} {short} {target_table} (via {target_table}.{fk_col})")
            elif rel.cardinality == "many-to-one":
                fk_col = _fk_col_name(target_table)
                lines.append(f"  - {source_table} {short} {target_table} (via {source_table}.{fk_col})")
            else:
                lines.append(f"  - {source_table} {short} {target_table}")

    return "\n".join(lines)


def _fk_col_name(table_name_str: str) -> str:
    """Derive FK column name from table name (de-pluralize + _id)."""
    if table_name_str.endswith("ies"):
        singular = table_name_str[:-3] + "y"
    elif table_name_str.endswith("ses") or table_name_str.endswith("shes") or \
         table_name_str.endswith("ches") or table_name_str.endswith("xes") or \
         table_name_str.endswith("zes"):
        singular = table_name_str[:-2]
    elif table_name_str.endswith("s"):
        singular = table_name_str[:-1]
    else:
        singular = table_name_str
    return f"{singular}_id"
