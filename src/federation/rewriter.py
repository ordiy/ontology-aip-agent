"""SQL rewriter for expanding virtual entities in the Federation Layer."""

import sqlglot
import sqlglot.errors
import sqlglot.expressions as exp
from src.ontology.provider import VirtualEntity


def expand_virtual_entities(
    sql: str,
    virtual_entities: dict[str, VirtualEntity],
    dialect: str | None = None,
) -> str:
    """Expand references to virtual entities in a SQL query into inline subqueries.

    Uses sqlglot to parse, walk the AST, and for every Table node whose name
    matches a key in virtual_entities, replace it with a Subquery over the
    base_on table with filter_sql applied as a WHERE clause. Preserves alias.

    Returns the rewritten SQL as a string (round-tripped through sqlglot).
    If no virtual entity is referenced, returns the original SQL unchanged
    (can be the exact same string — round-tripping is acceptable).
    """
    if not virtual_entities:
        return sql

    if dialect:
        dialect = dialect.split()[0].lower()

    try:
        ast = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError as e:
        raise ValueError(f"failed to rewrite virtual entities: {e}") from e

    modified = False

    for table_node in ast.find_all(exp.Table):
        # We only look at the base name of the table
        table_name = table_node.name
        
        if table_name in virtual_entities:
            ve = virtual_entities[table_name]
            
            # Construct the inner query
            inner_sql = f"SELECT * FROM {ve.based_on} WHERE {ve.filter_sql}"
            try:
                inner_ast = sqlglot.parse_one(inner_sql, read=dialect)
            except sqlglot.errors.ParseError as e:
                raise ValueError(f"failed to parse virtual entity inner SQL for {ve.name}: {e}") from e
                
            # Determine the alias to use
            if table_node.alias:
                alias = table_node.alias
            else:
                alias = ve.name
                
            # Wrap as a Subquery expression
            subquery = inner_ast.subquery(alias)
            
            # Replace the table node in the AST
            table_node.replace(subquery)
            modified = True

    if not modified:
        return ast.sql(dialect=dialect)
        
    return ast.sql(dialect=dialect)
