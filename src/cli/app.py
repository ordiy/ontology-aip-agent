import sys
import sqlite3
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.syntax import Syntax
from rich.prompt import Prompt, Confirm

from src.config import load_config
from src.ontology.parser import parse_ontology
from src.ontology.context import generate_context
from src.database.schema import create_tables
from src.database.mock_data import generate_mock_data
from src.database.executor import SQLExecutor
from src.llm.vertex import VertexGeminiClient
from src.agent.graph import build_graph

console = Console()


def _find_ontologies(ontology_dir: str) -> dict[str, str]:
    """Find all .rdf files in ontology directory. Returns name -> path."""
    result = {}
    for f in sorted(Path(ontology_dir).glob("*.rdf")):
        result[f.stem] = str(f)
    return result


def _display_table(rows: list[dict]):
    """Display query results as a rich table."""
    if not rows:
        console.print("[dim]No results.[/dim]")
        return
    table = Table(show_header=True, header_style="bold cyan")
    for col in rows[0].keys():
        table.add_column(col)
    for row in rows[:50]:  # limit display
        table.add_row(*[str(v) for v in row.values()])
    if len(rows) > 50:
        console.print(f"[dim]... showing 50 of {len(rows)} rows[/dim]")
    console.print(table)


def _handle_system_command(cmd: str, schema, class_to_table: dict, db_path: str) -> bool:
    """Handle dot commands. Returns True if handled."""
    parts = cmd.strip().split(maxsplit=1)
    command = parts[0].lower()

    if command == ".quit":
        console.print("[dim]Goodbye.[/dim]")
        sys.exit(0)
    elif command == ".tables":
        for cls_name, tbl_name in sorted(class_to_table.items()):
            console.print(f"  {tbl_name} ({cls_name})")
        return True
    elif command == ".schema" and len(parts) > 1:
        table_name = parts[1]
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.execute(f"PRAGMA table_info({table_name})")
            cols = cursor.fetchall()
            if cols:
                for col in cols:
                    pk = " PK" if col[5] else ""
                    console.print(f"  {col[1]} ({col[2]}{pk})")
            else:
                console.print(f"[red]Table '{table_name}' not found.[/red]")
        finally:
            conn.close()
        return True
    elif command == ".ontology":
        console.print(generate_context(schema))
        return True
    elif command == ".help":
        console.print("  .tables          - List all tables")
        console.print("  .schema <table>  - Show table structure")
        console.print("  .ontology        - Show ontology relationships")
        console.print("  .quit            - Exit")
        return True

    return False


def main():
    config = load_config()

    # Find ontologies
    ontology_dir = "ontologies"
    ontologies = _find_ontologies(ontology_dir)
    if not ontologies:
        console.print("[red]No ontology files found in ontologies/ directory.[/red]")
        sys.exit(1)

    # Domain selection
    console.print("\n[bold]Available domains:[/bold]")
    names = list(ontologies.keys())
    for i, name in enumerate(names, 1):
        console.print(f"  [{i}] {name}")

    choice = Prompt.ask("\nSelect domain", default="1")
    try:
        idx = int(choice) - 1
        domain_name = names[idx]
    except (ValueError, IndexError):
        domain_name = names[0]

    rdf_path = ontologies[domain_name]

    # Initialize
    console.print(f"\n[cyan]Loading {domain_name} ontology...[/cyan]")
    schema = parse_ontology(rdf_path)

    db_dir = Path(config["database"]["path"])
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(db_dir / f"{domain_name}.db")

    # Recreate DB each startup for clean state
    if Path(db_path).exists():
        Path(db_path).unlink()

    console.print("[cyan]Creating SQLite database...[/cyan]")
    class_to_table = create_tables(db_path, schema)

    rows_per_table = config["database"]["mock_rows_per_table"]
    console.print(f"[cyan]Generating mock data ({rows_per_table} rows per table)...[/cyan]")
    generate_mock_data(db_path, schema, rows_per_table=rows_per_table)

    console.print("[cyan]Connecting to LLM...[/cyan]")
    llm = VertexGeminiClient(
        project=config["vertex"]["project"],
        location=config["vertex"]["location"],
        model_name=config["llm"]["model"],
    )

    executor = SQLExecutor(db_path, config["permissions"])
    ontology_context = generate_context(schema)
    agent = build_graph(llm, executor, ontology_context)

    console.print(f"[green]Ready. Domain: {schema.domain}[/green]\n")

    # Conversation loop
    while True:
        try:
            user_input = Prompt.ask(f"[bold]{domain_name}[/bold]>")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye.[/dim]")
            break

        if not user_input.strip():
            continue

        if user_input.startswith("."):
            if _handle_system_command(user_input, schema, class_to_table, db_path):
                continue

        # Run agent
        initial_state = {
            "messages": [],
            "ontology_context": ontology_context,
            "user_query": user_input,
            "intent": "",
            "generated_sql": "",
            "permission_level": "",
            "approved": None,
            "query_result": None,
            "affected_rows": 0,
            "response": "",
            "clarify_count": 0,
            "error": None,
        }

        result = agent.invoke(initial_state)

        # Show intent
        if result.get("intent"):
            console.print(f"[dim]Intent: {result['intent']}[/dim]")

        # Show SQL
        if result.get("generated_sql"):
            console.print(Syntax(result["generated_sql"], "sql", theme="monokai"))

        # Handle approval if needed
        if result.get("approved") is None and result.get("permission_level") == "confirm":
            console.print(f"\n[yellow]This is a {result['intent']} operation.[/yellow]")
            if Confirm.ask("Execute?", default=False):
                result["approved"] = True
                exec_result = executor.execute(result["generated_sql"], approved=True)
                if exec_result.rows is not None:
                    result["query_result"] = exec_result.rows
                result["affected_rows"] = exec_result.affected_rows

                # Format the result
                from src.agent.nodes import format_result as format_result_fn
                format_output = format_result_fn({**result, "error": None}, llm)
                result.update(format_output)
            else:
                console.print("[dim]Cancelled.[/dim]")
                continue

        # Show results table
        if result.get("query_result"):
            _display_table(result["query_result"])

        # Show affected rows for writes
        if result.get("affected_rows", 0) > 0:
            console.print(f"[green]Affected rows: {result['affected_rows']}[/green]")

        # Show natural language response
        if result.get("response"):
            console.print(f"\n[bold]{result['response']}[/bold]\n")


if __name__ == "__main__":
    main()
