import sys
import sqlite3
from pathlib import Path
from uuid import uuid4
from rich.console import Console
from rich.table import Table
from rich.syntax import Syntax
from rich.prompt import Prompt, Confirm

from src.observability import ObservabilityClient
from src.config import load_config
from src.ontology.parser import parse_ontology
from src.ontology.context import generate_context
from src.ontology.rdf_provider import RDFOntologyProvider
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


def _initialize_domain(domain_name: str, ontologies: dict, config: dict, llm, obs=None) -> tuple:
    """Initialize schema, DB, mock data and agent for a given domain.

    Args:
        domain_name: Name of the domain (matches key in ontologies dict)
        ontologies: Dict mapping name -> rdf_path
        config: Loaded config dict
        llm: LLM client instance (already initialized)
        obs: Optional ObservabilityClient threaded into the agent for nested
            spans on federated sub-queries.

    Returns:
        Tuple of (schema, db_path, class_to_table, ontology_context, agent)
    """
    rdf_path = ontologies[domain_name]
    
    console.print(f"\n[cyan]Loading {domain_name} ontology...[/cyan]")
    schema = parse_ontology(rdf_path)

    db_dir = Path(config["database"]["path"])
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(db_dir / f"{domain_name}.db")

    # Recreate DB because ontology structure may differ from previous domain
    if Path(db_path).exists():
        Path(db_path).unlink()

    console.print("[cyan]Creating SQLite database...[/cyan]")
    class_to_table = create_tables(db_path, schema)

    rows_per_table = config["database"]["mock_rows_per_table"]
    console.print(f"[cyan]Generating mock data ({rows_per_table} rows per table)...[/cyan]")
    generate_mock_data(db_path, schema, rows_per_table=rows_per_table)

    ontology_context = generate_context(schema)
    executor = SQLExecutor(db_path, config["permissions"])
    ontology = RDFOntologyProvider([rdf_path], executor_dialect=executor.dialect)
    agent = build_graph(
        llm=llm,
        executors=executor,
        ontology=ontology,
        federation_config=config.get("federation"),
        obs=obs,
    )

    return schema, db_path, class_to_table, ontology_context, agent, schema.rules


def _handle_system_command(cmd: str, schema, class_to_table: dict, db_path: str, ontologies: dict = None, config: dict = None, llm = None, history: list = None):
    """Handle dot commands. Returns True if handled, or a dict if action required."""
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
    elif command == ".history":
        if len(parts) > 1 and parts[1].lower() == "clear":
            # Signal main() to clear history
            return {"clear_history": True}
        
        # Display conversation history as a numbered list
        if not history:
            console.print("[dim]No history yet.[/dim]")
            return True
            
        for i, entry in enumerate(history, 1):
            console.print(f"\n[bold cyan][{i}][/bold cyan] [dim]{entry['intent']}[/dim] {entry['query']}")
            if entry.get('sql'):
                sql = entry['sql']
                # Truncate display to avoid terminal clutter
                console.print(f"    [dim]SQL:[/dim] {sql[:80]}{'...' if len(sql) > 80 else ''}")
            if entry.get('response'):
                resp = entry['response']
                # Truncate display to avoid terminal clutter
                console.print(f"    [dim]→[/dim] {resp[:100]}{'...' if len(resp) > 100 else ''}")
        return True
    elif command == ".switch":
        if not ontologies or not config or not llm:
            return True  # handled but can't switch
        if len(parts) < 2:
            # Show available domains
            console.print("[bold]Available domains:[/bold]")
            for name in sorted(ontologies.keys()):
                console.print(f"  {name}")
            return True
        new_domain = parts[1].lower()
        if new_domain not in ontologies:
            available = ", ".join(sorted(ontologies.keys()))
            console.print(f"[red]Domain '{new_domain}' not found. Available: {available}[/red]")
            return True
        # Return special marker so main() knows to switch
        return {"switch_to": new_domain}
    elif command == ".help":
        console.print("  .tables          - List all tables")
        console.print("  .schema <table>  - Show table structure")
        console.print("  .ontology        - Show ontology relationships")
        console.print("  .history         - Show conversation history")
        console.print("  .history clear   - Clear conversation history")
        console.print("  .switch          - List available domains")
        console.print("  .switch <domain> - Switch to domain")
        console.print("  .quit            - Exit")
        return True

    return False


def main():
    config = load_config()
    obs = ObservabilityClient(config.get('langfuse', {}))

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

    console.print("[cyan]Connecting to LLM...[/cyan]")

    # Choose LLM provider based on config
    provider = config["llm"].get("provider", "vertex")
    if provider == "ollama":
        from src.llm.ollama import OllamaClient
        llm = OllamaClient(
            host=config["ollama"]["host"],
            model_name=config["ollama"]["model"],
            timeout=config["ollama"]["timeout"],
        )
        console.print(f"[cyan]Using Ollama: {config['ollama']['model']} at {config['ollama']['host']}[/cyan]")

    elif provider == "openai":
        from src.llm.openai_compat import OpenAICompatClient
        cfg = config["openai"]
        llm = OpenAICompatClient(
            api_key=cfg["api_key"],
            model_name=cfg.get("model", "gpt-4o"),
            base_url=cfg.get("base_url", "https://api.openai.com/v1"),
            provider_name="OpenAI",
        )
        console.print(f"[cyan]Using OpenAI: {cfg.get('model', 'gpt-4o')}[/cyan]")

    elif provider == "openrouter":
        from src.llm.openai_compat import OpenAICompatClient
        cfg = config["openrouter"]
        extra_headers = {}
        if cfg.get("site_url"):
            extra_headers["HTTP-Referer"] = cfg["site_url"]
        if cfg.get("app_name"):
            extra_headers["X-Title"] = cfg["app_name"]
        llm = OpenAICompatClient(
            api_key=cfg["api_key"],
            model_name=cfg.get("model", "anthropic/claude-3.5-sonnet"),
            base_url=cfg.get("base_url", "https://openrouter.ai/api/v1"),
            provider_name="OpenRouter",
            extra_headers=extra_headers,
        )
        console.print(f"[cyan]Using OpenRouter: {cfg.get('model', 'anthropic/claude-3.5-sonnet')}[/cyan]")

    else:
        # Default: Vertex AI Gemini
        llm = VertexGeminiClient(
            project=config["vertex"]["project"],
            location=config["vertex"]["location"],
            model_name=config["llm"]["model"],
            credentials_path=config["vertex"].get("credentials", ""),
        )
        console.print(f"[cyan]Using Vertex AI: {config['llm']['model']}[/cyan]")

    llm = obs.wrap_llm(llm)

    schema, db_path, class_to_table, ontology_context, agent, rdf_rules = _initialize_domain(
        domain_name, ontologies, config, llm, obs=obs
    )

    console.print(f"[green]Ready. Domain: {schema.domain}[/green]\n")

    # Initialize before the conversation loop
    conversation_history = []  # List of {"query": str, "intent": str, "sql": str, "response": str}
    _llm_context_history = []  # Compact history for LLM context
    session_id = str(uuid4())

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
            result = _handle_system_command(
                user_input, schema, class_to_table, db_path,
                ontologies=ontologies, config=config, llm=llm,
                history=conversation_history
            )
            if isinstance(result, dict):
                if "switch_to" in result:
                    new_domain = result["switch_to"]
                    console.print(f"[cyan]Switching to domain: {new_domain}...[/cyan]")
                    schema, db_path, class_to_table, ontology_context, agent, rdf_rules = _initialize_domain(
                        new_domain, ontologies, config, llm, obs=obs
                    )
                    domain_name = new_domain
                    console.print(f"[green]Switched to domain: {domain_name} ({schema.domain})[/green]")
                    continue
                elif "clear_history" in result:
                    conversation_history.clear()
                    _llm_context_history.clear()
                    console.print("[dim]History cleared.[/dim]")
                    continue
            elif result:
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
            "sql_retry_count": 0,
            "sql_error_message": None,
            "analysis_plan": [],
            "sub_results": [],
            "conversation_history": _llm_context_history,
            "rdf_rules": rdf_rules,
            "user_overrides": {},
            "decision": {},
            "operation_plan": [],
            "operation_results": [],
            "rollback_stack": [],
            "current_op_index": 0,
        }

        with obs.start_trace(
            session_id=session_id,
            name="agent-query",
            input={"query": user_input, "domain": domain_name},
            metadata={"domain": domain_name},
        ) as trace:
            result = agent.invoke(initial_state)
            if trace:
                trace.update(output={
                    "intent": result.get("intent", ""),
                    "response": result.get("response", ""),
                })

        # Show intent
        if result.get("intent"):
            console.print(f"[dim]Intent: {result['intent']}[/dim]")

        # Pattern D: Show override info
        overrides = result.get("user_overrides")
        if overrides and (overrides.get("skip_steps") or overrides.get("override_rules")):
            console.print("[yellow]Applied Overrides:[/yellow]")
            if overrides.get("skip_steps"):
                console.print(f"  [dim]Skip Steps:[/dim] {', '.join(overrides['skip_steps'])}")
            if overrides.get("override_rules"):
                console.print(f"  [dim]Rules:[/dim] {', '.join(overrides['override_rules'])}")

        # Pattern D: Show Decision
        if result.get("intent") == "DECIDE" and result.get("decision"):
            decision = result["decision"]
            console.print(f"\n[bold cyan]📊 {decision.get('decision', 'Decision').upper()}[/bold cyan]")
            console.print(f"Reasoning: {decision.get('reasoning')}")
            
            affected = decision.get("affected_entities", [])
            if affected:
                console.print(f"[green]Affected IDs:[/green] {', '.join(map(str, affected))}")
                
            excluded = decision.get("excluded_entities", [])
            if excluded:
                console.print(f"[dim]Excluded:[/dim] {len(excluded)} entities")

            # Wait for approval if required
            if decision.get("requires_approval") and result.get("approved") is None:
                if Confirm.ask("\nExecute this decision?", default=False):
                    result["approved"] = True
                    # Re-invoke to continue to plan_operation
                    result = agent.invoke(result, invoke_config)
                else:
                    console.print("[dim]Cancelled.[/dim]")
                    continue

        # Pattern D: Show Operation Plan and Progress
        if result.get("operation_plan"):
            console.print("\n[bold]⚙️  Operation Plan:[/bold]")
            for step in result["operation_plan"]:
                status = "[green]✓[/green]" if not step.get("skipped") else "[dim]→[/dim]"
                console.print(f"  {status} {step['step_name']}: {step['description']}")

        # Show analysis steps for ANALYZE intent
        if result.get("intent") == "ANALYZE" and result.get("sub_results"):
            for i, sr in enumerate(result["sub_results"], 1):
                console.print(f"\n[dim]Step {i}: {sr['step']}[/dim]")
                console.print(Syntax(sr["sql"], "sql", theme="monokai"))
                if sr.get("rows"):
                    _display_table(sr["rows"])

        # Show SQL
        if result.get("generated_sql"):
            console.print(Syntax(result["generated_sql"], "sql", theme="monokai"))

        # Handle approval if needed for simple WRITE
        if result.get("intent") == "WRITE" and result.get("approved") is None and result.get("permission_level") == "confirm":
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

        # Track conversation history
        conversation_history.append({
            "query": user_input,
            "intent": result.get("intent", ""),
            "sql": result.get("generated_sql", ""),
            "response": result.get("response", ""),
        })

        # Append this turn to LLM context history
        if result.get("generated_sql") and result.get("response"):
            _llm_context_history.append({
                "query": user_input,
                "sql": result.get("generated_sql", ""),
                "result_summary": result.get("result_summary", result.get("response", "")[:150]),
            })
            # Keep only last 10 turns to avoid growing indefinitely
            _llm_context_history = _llm_context_history[-10:]

if __name__ == "__main__":
    main()
