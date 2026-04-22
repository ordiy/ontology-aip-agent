import logging
import sqlglot
import sqlglot.expressions as exp

logger = logging.getLogger(__name__)

def build_federated_plan(rewritten_sql: str, mappings: dict, registry, default_engine: str):
    """Builds a federated QueryPlan with predicate and projection pushdown (Phase 4.1)."""
    # Import locally to avoid circular dependencies if planner.py imports us at the top
    from src.federation.planner import QueryPlan, SubQuery, JoinSpec
    
    try:
        parse_dialect = default_engine.split()[0].lower() if default_engine else None
        ast = sqlglot.parse_one(rewritten_sql, read=parse_dialect)
    except Exception as e:
        raise ValueError(f"Failed to parse SQL: {e}") from e

    if not isinstance(ast, exp.Select):
        raise NotImplementedError("federated plan supports only 2-table JOINs in Phase 3/4")

    from_expr = ast.args.get("from_")
    joins = ast.args.get("joins") or []

    if not from_expr or not isinstance(from_expr.this, exp.Table):
        raise NotImplementedError("federated plan supports only 2-table JOINs in Phase 3/4")
        
    if len(joins) != 1:
        raise NotImplementedError("federated plan supports only 2-table JOINs in Phase 3/4")
        
    join_expr = joins[0]
    if not isinstance(join_expr.this, exp.Table):
        raise NotImplementedError("federated plan supports only 2-table JOINs in Phase 3/4")
        
    if not join_expr.args.get("on"):
        raise NotImplementedError("federated plan supports only 2-table JOINs in Phase 3/4")

    table_a = from_expr.this
    table_b = join_expr.this

    table_a_name = ".".join(p.name for p in table_a.parts)
    table_b_name = ".".join(p.name for p in table_b.parts)

    def resolve_table(name: str) -> tuple[str, str]:
        for _, m in mappings.items():
            if m.physical_table == name:
                return m.query_engine, m.physical_table
        if name in mappings:
            m = mappings[name]
            return m.query_engine, m.physical_table or name
        return registry._default_engine, name

    engine_a, phys_a = resolve_table(table_a_name)
    engine_b, phys_b = resolve_table(table_b_name)

    a_names = {table_a.name}
    if table_a.alias:
        a_names.add(table_a.alias)

    b_names = {table_b.name}
    if table_b.alias:
        b_names.add(table_b.alias)

    # 1. Collect predicates
    a_predicates = []
    b_predicates = []
    shared_predicates = []
    
    def get_conjuncts(node):
        if isinstance(node, exp.And):
            yield from get_conjuncts(node.left)
            yield from get_conjuncts(node.right)
        elif node:
            yield node

    where = ast.args.get("where")
    if where:
        for c in get_conjuncts(where.this):
            cols = {col.table for col in c.find_all(exp.Column)}
            if not cols:
                shared_predicates.append(c.copy())
            elif cols.issubset(a_names):
                a_predicates.append(c.copy())
            elif cols.issubset(b_names):
                b_predicates.append(c.copy())
            else:
                shared_predicates.append(c.copy())

    # 2. Collect columns
    a_cols = set()
    b_cols = set()
    has_unqualified = False

    def collect_columns(node):
        nonlocal has_unqualified
        if not node: return
        for col in node.find_all(exp.Column):
            if not col.table:
                has_unqualified = True
            elif col.table in a_names:
                a_cols.add(col.name)
            elif col.table in b_names:
                b_cols.add(col.name)
            else:
                has_unqualified = True # Unknown qualifier, treat as unqualified for safety

    collect_columns(join_expr.args.get("on"))
    for e in ast.expressions:
        collect_columns(e)
    for p in shared_predicates:
        collect_columns(p)

    # 3. Build Subqueries
    def get_safe_dialect(engine_name):
        try:
            raw_dialect = registry.get(engine_name).dialect
            d = raw_dialect.split()[0].lower() if raw_dialect else None
            import sqlglot.dialects
            if d in sqlglot.dialects.DIALECTS:
                return d
            # Also handle common names that might map differently
            if d == "postgresql": return "postgres"
            return None
        except Exception:
            return None

    dialect_a = get_safe_dialect(engine_a)
    dialect_b = get_safe_dialect(engine_b)

    if has_unqualified:
        logger.info("Unqualified columns found in query, falling back to SELECT * for both sides")
        sq0_sql = f"SELECT * FROM {phys_a}"
        sq1_sql = f"SELECT * FROM {phys_b}"
        # We also need to leave all predicates in the final SQL because pushdown might be unsafe
        # No, wait. Predicates without unqualified columns are ALREADY classified to a/b and copied!
        # But if we fallback, do we push down predicates anyway? 
        # The prompt says: "if they unambiguously belong to one side's schema you cannot know that here — so fall back to SELECT * for BOTH sides (conservative) in that case, and emit a logger.info(...) noting why pushdown was skipped."
        # If we fallback to SELECT *, we can STILL push down predicates that were unambiguously assigned. 
        # Wait! "For unqualified columns in any of the above... you cannot know that here — so fall back to SELECT * for BOTH sides (conservative) in that case, and emit a logger.info(...) noting why pushdown was skipped."
        # Let's read this carefully. "noting why pushdown was skipped." Does it mean projection pushdown is skipped, or ALL pushdown?
        # Let's assume ONLY PROJECTION pushdown is skipped, since predicate pushdown is already known to be safe for those predicates.
        # But wait, what if `WHERE status = 'active'` is present? It's an unqualified column. It goes into `shared_predicates`.
        # That's perfectly fine, the joiner will execute it.
        # So we just fallback the projections to `*`.
        selects_a = ["*"]
        selects_b = ["*"]
    else:
        selects_a = list(a_cols) if a_cols else ["*"]
        selects_b = list(b_cols) if b_cols else ["*"]

    def strip_qualifiers(preds):
        for p in preds:
            for col in p.find_all(exp.Column):
                col.set("table", None)
                col.set("db", None)
                col.set("catalog", None)

    strip_qualifiers(a_predicates)
    strip_qualifiers(b_predicates)

    def build_sq_sql(phys_table, selects, preds, dialect):
        sq = exp.Select().from_(phys_table)
        for s in selects:
            if s == "*":
                sq = sq.select("*")
            else:
                sq = sq.select(exp.Column(this=exp.Identifier(this=s)))
        if preds:
            sq = sq.where(exp.and_(*preds))
        return sq.sql(dialect=dialect)

    try:
        sq0_sql = build_sq_sql(phys_a, selects_a, a_predicates, dialect_a)
        sq1_sql = build_sq_sql(phys_b, selects_b, b_predicates, dialect_b)
    except Exception as e:
        logger.warning(f"Failed to build pushdown SQL, falling back to Phase 3.1: {e}")
        sq0_sql = f"SELECT * FROM {phys_a}"
        sq1_sql = f"SELECT * FROM {phys_b}"
        # Restore WHERE clause if pushdown failed
        shared_predicates = [c.copy() for c in get_conjuncts(where.this)] if where else []

    sq0 = SubQuery(engine=engine_a, sql=sq0_sql, projected_columns=selects_a)
    sq1 = SubQuery(engine=engine_b, sql=sq1_sql, projected_columns=selects_b)

    # 4. Rewrite final query
    table_a.set("this", exp.Identifier(this="sub_0"))
    table_a.set("alias", exp.TableAlias(this=exp.Identifier(this="sub_0")))
    table_a.set("db", None)
    table_a.set("catalog", None)

    table_b.set("this", exp.Identifier(this="sub_1"))
    table_b.set("alias", exp.TableAlias(this=exp.Identifier(this="sub_1")))
    table_b.set("db", None)
    table_b.set("catalog", None)

    for col in ast.find_all(exp.Column):
        if col.table in a_names:
            col.set("table", exp.Identifier(this="sub_0"))
            col.set("db", None)
            col.set("catalog", None)
        elif col.table in b_names:
            col.set("table", exp.Identifier(this="sub_1"))
            col.set("db", None)
            col.set("catalog", None)

    if not shared_predicates:
        ast.set("where", None)
    else:
        # shared_predicates are already copied, but they still have the original table aliases
        # which we just rewrote using the global ast.find_all loop! 
        # WAIT! If they are copies, they WON'T be updated by `ast.find_all(exp.Column)` above!
        # Because we copied them, they are detached from `ast`. We need to rewrite them too!
        for p in shared_predicates:
            for col in p.find_all(exp.Column):
                if col.table in a_names:
                    col.set("table", exp.Identifier(this="sub_0"))
                    col.set("db", None)
                    col.set("catalog", None)
                elif col.table in b_names:
                    col.set("table", exp.Identifier(this="sub_1"))
                    col.set("db", None)
                    col.set("catalog", None)
        ast.set("where", exp.Where(this=exp.and_(*shared_predicates)))

    final_sql = ast.sql(dialect="duckdb")

    return QueryPlan(
        kind="federated",
        sub_queries=[sq0, sq1],
        join_spec=JoinSpec(
            sub_aliases=["sub_0", "sub_1"],
            final_sql=final_sql,
        ),
    )
