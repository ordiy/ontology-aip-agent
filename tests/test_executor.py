import sqlite3
import pytest
from src.database.executor import SQLExecutor, PermissionDenied


@pytest.fixture
def executor(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")
    conn.execute("INSERT INTO users (name, age) VALUES ('Alice', 30)")
    conn.execute("INSERT INTO users (name, age) VALUES ('Bob', 25)")
    conn.commit()
    conn.close()
    permissions = {"read": "auto", "write": "confirm", "delete": "confirm", "admin": "deny"}
    return SQLExecutor(db_path, permissions)


def test_select_returns_rows(executor):
    result = executor.execute("SELECT name, age FROM users ORDER BY name")
    assert result.rows == [{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]
    assert result.operation == "read"
    assert result.needs_approval is False


def test_classify_select(executor):
    info = executor.classify("SELECT * FROM users")
    assert info.operation == "read"
    assert info.approval_mode == "auto"


def test_classify_update(executor):
    info = executor.classify("UPDATE users SET name = 'Charlie' WHERE id = 1")
    assert info.operation == "write"
    assert info.approval_mode == "confirm"


def test_classify_delete(executor):
    info = executor.classify("DELETE FROM users WHERE id = 1")
    assert info.operation == "delete"
    assert info.approval_mode == "confirm"


def test_classify_drop_is_admin(executor):
    info = executor.classify("DROP TABLE users")
    assert info.operation == "admin"
    assert info.approval_mode == "deny"


def test_admin_operation_raises(executor):
    with pytest.raises(PermissionDenied, match="admin"):
        executor.execute("DROP TABLE users")


def test_update_returns_affected_count(executor):
    result = executor.execute("UPDATE users SET age = 31 WHERE name = 'Alice'", approved=True)
    assert result.affected_rows == 1
    assert result.operation == "write"


def test_write_without_approval_needs_it(executor):
    result = executor.execute("UPDATE users SET age = 99", approved=False)
    assert result.needs_approval is True
    assert result.affected_rows == 0


def test_invalid_sql_raises(executor):
    with pytest.raises(Exception):
        executor.execute("SELECTT * FROM users")

def test_query_timeout(tmp_path):
    """SQLExecutor should return timeout error for queries that exceed 5 seconds."""
    import time
    from unittest.mock import patch
    from src.database.executor import SQLExecutor, QUERY_TIMEOUT_SECONDS

    # Create a real DB
    db_path = str(tmp_path / "timeout_test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()

    executor = SQLExecutor(db_path, {"read": "auto", "write": "confirm", "delete": "confirm", "admin": "deny"})

    # Patch the internal execution to simulate a slow query
    original_inner = executor._execute_sql_inner

    def slow_execute(sql, classification):
        time.sleep(QUERY_TIMEOUT_SECONDS + 1)  # Always exceeds timeout
        return original_inner(sql, classification)

    with patch.object(executor, "_execute_sql_inner", slow_execute):
        start = time.time()
        result = executor.execute("SELECT * FROM t")
        elapsed = time.time() - start

    # Should have returned within ~timeout+1s (not hung forever)
    assert elapsed < QUERY_TIMEOUT_SECONDS + 3
    assert result.error is not None
    assert "timed out" in result.error.lower()


def test_normal_query_completes_within_timeout(tmp_path):
    """Normal queries should complete successfully without triggering timeout."""
    import sqlite3 as _sqlite3
    from src.database.executor import SQLExecutor

    db_path = str(tmp_path / "normal_test.db")
    conn = _sqlite3.connect(db_path)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, val TEXT)")
    conn.execute("INSERT INTO items VALUES (1, 'hello')")
    conn.commit()
    conn.close()

    executor = SQLExecutor(db_path, {"read": "auto", "write": "confirm", "delete": "confirm", "admin": "deny"})
    result = executor.execute("SELECT * FROM items")

    assert result.error is None
    assert len(result.rows) == 1
    assert result.rows[0]["val"] == "hello"
