"""SQL parser for the Federation Layer.

Uses sqlglot to analyze SQL queries and extract table dependencies.
"""

import sqlglot
import sqlglot.errors
import sqlglot.expressions as exp


def extract_tables(sql: str, dialect: str | None = None) -> list[str]:
    """Parse SQL and return a de-duplicated, order-preserving list of referenced table names.

    Uses sqlglot.parse_one with the given dialect. Returns table names exactly as
    they appear (no case-normalization). For qualified names (catalog.schema.table)
    returns the full dotted form.

    Args:
        sql: The SQL query to parse.
        dialect: Optional SQL dialect to use for parsing (e.g., 'sqlite', 'mysql').

    Returns:
        A list of table names referenced in the query.

    Raises:
        ValueError: If the SQL query cannot be parsed.
    """
    if dialect:
        # Extract the base dialect name, e.g. "MySQL (StarRocks-compatible)" -> "mysql"
        dialect = dialect.split()[0].lower()

    try:
        ast = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError as e:
        raise ValueError(f"Failed to parse SQL: {e}") from e

    # Extract tables, preserving order and deduplicating
    tables = []
    seen = set()

    for table_exp in ast.find_all(exp.Table):
        # Join the parts (catalog, db, name) of the table expression
        table_name = ".".join(p.name for p in table_exp.parts)
        if table_name not in seen:
            seen.add(table_name)
            tables.append(table_name)

    return tables
