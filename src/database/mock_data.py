import sqlite3
import random
from faker import Faker
from src.ontology.parser import OntologySchema
from src.ontology.context import table_name, _fk_col_name

fake = Faker()


def _get_faker_value(prop_name: str, data_type: str):
    """Generate a realistic fake value based on property name and type."""
    name_lower = prop_name.lower()

    if data_type == "string":
        if "email" in name_lower:
            return fake.email()
        elif "name" in name_lower:
            return fake.name()
        elif "phone" in name_lower:
            return fake.phone_number()
        elif "address" in name_lower:
            return fake.address().replace("\n", ", ")
        elif "status" in name_lower:
            return fake.random_element(["active", "pending", "completed", "cancelled", "overdue"])
        elif "id" in name_lower:
            return fake.uuid4()[:8]
        elif "category" in name_lower:
            return fake.random_element(["Electronics", "Clothing", "Books", "Food", "Sports"])
        elif "method" in name_lower or "shipping" in name_lower:
            return fake.random_element(["Standard", "Express", "Overnight", "Free"])
        elif "tier" in name_lower or "level" in name_lower:
            return fake.random_element(["Bronze", "Silver", "Gold", "Platinum"])
        elif "title" in name_lower:
            return fake.sentence(nb_words=5)
        elif "body" in name_lower or "comment" in name_lower or "description" in name_lower:
            return fake.text(max_nb_chars=100)
        elif "sku" in name_lower:
            return fake.bothify("???-####").upper()
        else:
            return fake.text(max_nb_chars=50)
    elif data_type == "integer":
        if "age" in name_lower:
            return fake.random_int(min=18, max=80)
        elif "qty" in name_lower or "count" in name_lower or "quantity" in name_lower:
            return fake.random_int(min=0, max=500)
        elif "rating" in name_lower:
            return fake.random_int(min=1, max=5)
        else:
            return fake.random_int(min=0, max=1000)
    elif data_type == "float":
        if "price" in name_lower or "amount" in name_lower or "total" in name_lower or \
           "cost" in name_lower or "spent" in name_lower or "subtotal" in name_lower:
            return round(fake.pyfloat(min_value=1, max_value=10000, right_digits=2), 2)
        else:
            return round(fake.pyfloat(min_value=0, max_value=10000, right_digits=2), 2)
    elif data_type == "date":
        return fake.date_this_year().isoformat()
    elif data_type == "datetime":
        return fake.date_time_this_year().isoformat()
    elif data_type == "boolean":
        return 1 if fake.boolean() else 0
    else:
        return fake.text(max_nb_chars=50)


def _topological_sort(classes, fk_info, class_to_table):
    """Return classes in topological order (parents before children) using Kahn's algorithm."""
    table_to_class = {v: k for k, v in class_to_table.items()}
    class_map = {c.name: c for c in classes}
    class_names = list(class_map.keys())

    deps: dict[str, set[str]] = {name: set() for name in class_names}
    children: dict[str, set[str]] = {name: set() for name in class_names}

    for cls in classes:
        tbl = class_to_table[cls.name]
        for _, parent_table in fk_info.get(tbl, []):
            parent_name = table_to_class.get(parent_table)
            if parent_name and parent_name in class_map:
                deps[cls.name].add(parent_name)
                children[parent_name].add(cls.name)

    in_degree = {name: len(deps[name]) for name in class_names}
    queue = [name for name in class_names if in_degree[name] == 0]
    result = []

    while queue:
        node = queue.pop(0)
        result.append(class_map[node])
        for child in children[node]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    result_names = {c.name for c in result}
    result += [class_map[name] for name in class_names if name not in result_names]
    return result


def generate_mock_data(db_path: str, schema: OntologySchema, rows_per_table: int = 100):
    """Generate mock data for all tables based on ontology schema."""
    class_to_table = {c.name: table_name(c.name) for c in schema.classes}

    # Build FK info: fk_info[table] = [(fk_col, parent_table)]
    fk_info: dict[str, list[tuple[str, str]]] = {}
    for rel in schema.relationships:
        source_table = class_to_table.get(rel.source, "")
        target_table = class_to_table.get(rel.target, "")
        if not source_table or not target_table:
            continue
        if rel.cardinality == "one-to-many":
            fk_col = _fk_col_name(source_table)
            fk_info.setdefault(target_table, []).append((fk_col, source_table))
        elif rel.cardinality == "many-to-one":
            fk_col = _fk_col_name(target_table)
            fk_info.setdefault(source_table, []).append((fk_col, target_table))

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        # Insert rows for entity tables, parents first (topological order)
        ordered_classes = _topological_sort(schema.classes, fk_info, class_to_table)

        table_ids: dict[str, list[int]] = {}
        for cls in ordered_classes:
            tbl = class_to_table[cls.name]
            fk_deps = fk_info.get(tbl, [])

            col_names = [fk_col for fk_col, _ in fk_deps] + [prop.name for prop in cls.properties]
            # Quote column names to match schema.py quoting
            quoted_cols = [f'"{c}"' for c in col_names]
            placeholders = ", ".join(["?"] * len(col_names))
            col_str = ", ".join(quoted_cols)

            ids = []
            for _ in range(rows_per_table):
                values = []
                for fk_col, parent_table in fk_deps:
                    parent_ids = table_ids.get(parent_table, [1])
                    values.append(random.choice(parent_ids))
                for prop in cls.properties:
                    values.append(_get_faker_value(prop.name, prop.data_type))

                cursor = conn.execute(
                    f'INSERT INTO "{tbl}" ({col_str}) VALUES ({placeholders})',
                    values,
                )
                ids.append(cursor.lastrowid)

            table_ids[tbl] = ids

        # Insert junction table rows for M:N
        for rel in schema.relationships:
            if rel.cardinality == "many-to-many":
                source_table = class_to_table.get(rel.source, "")
                target_table = class_to_table.get(rel.target, "")
                if not source_table or not target_table:
                    continue
                junction = f"{source_table}_{target_table}"
                src_fk = _fk_col_name(source_table)
                tgt_fk = _fk_col_name(target_table)

                source_ids = table_ids.get(source_table, [])
                target_ids = table_ids.get(target_table, [])

                if source_ids and target_ids:
                    pairs: set[tuple[int, int]] = set()
                    num_links = min(rows_per_table * 2, len(source_ids) * len(target_ids))
                    attempts = 0
                    while len(pairs) < num_links and attempts < num_links * 10:
                        pairs.add((random.choice(source_ids), random.choice(target_ids)))
                        attempts += 1

                    for src_id, tgt_id in pairs:
                        conn.execute(
                            f'INSERT INTO "{junction}" ("{src_fk}", "{tgt_fk}") VALUES (?, ?)',
                            (src_id, tgt_id),
                        )

        conn.commit()
    finally:
        conn.close()
