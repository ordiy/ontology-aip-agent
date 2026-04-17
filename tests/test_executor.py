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
