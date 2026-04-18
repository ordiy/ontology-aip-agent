"""Streamlit web UI for the ontology-driven data agent.

Provides a chat-style interface to the same LangGraph agent used by the CLI.
The UI is intentionally minimal — it mirrors the CLI's behavior:
  - Domain selection in the sidebar
  - Chat input at the bottom
  - Results (table + natural language) in the main area
  - Write operations show SQL and ask for confirmation

State is managed via st.session_state to persist across Streamlit reruns.
"""

import sys
import streamlit as st
from pathlib import Path
import pandas as pd

# When launched via `streamlit run src/web/app.py`, Streamlit adds the script's
# directory (src/web/) to sys.path instead of the project root. Insert the
# project root explicitly so `from src.xxx import ...` imports resolve correctly.
_PROJECT_ROOT = str(Path(__file__).parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.config import load_config
from src.ontology.parser import parse_ontology
from src.ontology.context import generate_context
from src.database.schema import create_tables
from src.database.mock_data import generate_mock_data
from src.database.executor import SQLExecutor
from src.agent.graph import build_graph


# ─────────────────────────────────────────────
# Page configuration (must be first Streamlit call)
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="Ontology Data Agent",
    page_icon="🧠",
    layout="wide",
)


def _find_ontologies(ontology_dir: str) -> dict[str, str]:
    """Find all .rdf files in the ontology directory.

    Returns a dict mapping domain name (file stem) to file path.
    """
    result = {}
    for f in sorted(Path(ontology_dir).glob("*.rdf")):
        result[f.stem] = str(f)
    return result


@st.cache_resource(show_spinner="Loading domain...")
def _load_domain(domain_name: str, rdf_path: str, config: dict):
    """Load and initialize a domain: parse ontology, create DB, generate mock data.

    Cached with st.cache_resource so domain re-initialization only happens
    when domain_name changes, not on every Streamlit rerun.

    Returns:
        Tuple of (schema, db_path, ontology_context, llm, executor, agent)
    """
    schema = parse_ontology(rdf_path)

    db_dir = Path(config["database"]["path"])
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(db_dir / f"web_{domain_name}.db")

    # Always recreate DB for clean state (web_ prefix avoids conflicts with CLI DB)
    if Path(db_path).exists():
        Path(db_path).unlink()

    create_tables(db_path, schema)
    generate_mock_data(db_path, schema, rows_per_table=config["database"]["mock_rows_per_table"])

    # Initialize LLM based on config provider
    provider = config["llm"].get("provider", "vertex")
    if provider == "ollama":
        from src.llm.ollama import OllamaClient
        llm = OllamaClient(
            host=config["ollama"]["host"],
            model_name=config["ollama"]["model"],
        )
    else:
        from src.llm.vertex import VertexGeminiClient
        llm = VertexGeminiClient(
            project=config["vertex"]["project"],
            location=config["vertex"]["location"],
            model_name=config["llm"]["model"],
            credentials_path=config["vertex"].get("credentials", ""),
        )

    executor = SQLExecutor(db_path, config["permissions"])
    ontology_context = generate_context(schema)
    agent = build_graph(llm, executor, ontology_context)

    return schema, db_path, ontology_context, llm, executor, agent


def _display_results(result: dict):
    """Display agent results in the Streamlit chat UI.

    Shows: intent badge, SQL code block, data table, natural language response.
    Write operations that need approval are handled separately.
    """
    # Intent badge
    intent = result.get("intent", "")
    if intent == "READ":
        # Using markdown colored text as st.badge might not be universally available depending on the version
        st.markdown("**Intent:** :blue[READ]")
    elif intent == "WRITE":
        st.markdown("**Intent:** :orange[WRITE]")

    # Generated SQL
    if result.get("generated_sql"):
        with st.expander("Generated SQL", expanded=False):
            st.code(result["generated_sql"], language="sql")

    # Query results as table
    if result.get("query_result"):
        rows = result["query_result"]
        if rows:
            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True)
            if len(rows) > 50:
                st.caption(f"Showing all {len(rows)} rows")

    # Affected rows for write operations
    if result.get("affected_rows", 0) > 0:
        st.success(f"✅ Affected rows: {result['affected_rows']}")

    # Natural language response
    if result.get("response"):
        st.markdown(f"**{result['response']}**")

    # Error display
    if result.get("error"):
        st.error(f"Error: {result['error']}")


def main():
    """Main Streamlit app entry point.

    Layout:
    - Sidebar: domain selection + ontology info
    - Main: chat history + input + results
    """
    config = load_config()
    ontologies = _find_ontologies("ontologies")

    if not ontologies:
        st.error("No ontology files found in ontologies/ directory.")
        st.stop()

    # ── Sidebar ──────────────────────────────
    with st.sidebar:
        st.title("🧠 Ontology Agent")
        st.divider()

        # Domain selector
        domain_names = list(ontologies.keys())
        selected_domain = st.selectbox(
            "Select Domain",
            domain_names,
            index=0,
            key="selected_domain",
        )

        st.divider()

        # Load the selected domain (cached)
        rdf_path = ontologies[selected_domain]
        schema, db_path, ontology_context, llm, executor, agent = _load_domain(
            selected_domain, rdf_path, config
        )

        # Domain info
        st.subheader(f"📦 {schema.domain}")
        st.caption(f"{len(schema.classes)} entity types")
        for cls in schema.classes:
            st.text(f"• {cls.name} ({len(cls.properties)} fields)")

        st.divider()
        if st.button("🗑️ Clear Chat"):
            st.session_state.chat_history = []
            st.rerun()

    # ── Main area ────────────────────────────
    st.title(f"{schema.domain} — Data Agent")

    # Initialize chat history in session state
    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    # Track pending write approval
    if "pending_write" not in st.session_state:
        st.session_state.pending_write = None

    # Display chat history
    for entry in st.session_state.chat_history:
        with st.chat_message("user"):
            st.write(entry["query"])
        with st.chat_message("assistant"):
            _display_results(entry["result"])

    # ── Pending write approval ────────────────
    if st.session_state.pending_write is not None:
        pending = st.session_state.pending_write
        st.warning(f"⚠️ This is a **{pending['intent']}** operation.")
        st.code(pending["generated_sql"], language="sql")

        col1, col2 = st.columns(2)
        with col1:
            if st.button("✅ Confirm", type="primary"):
                # Execute approved write
                exec_result = executor.execute(pending["generated_sql"], approved=True)
                final_result = {**pending, "affected_rows": exec_result.affected_rows, "error": exec_result.error}
                
                # We optionally format the result just like CLI does, but the specs don't strictly require it.
                # Just saving it to history.
                st.session_state.chat_history.append({
                    "query": pending["user_query"],
                    "result": final_result,
                })
                st.session_state.pending_write = None
                st.rerun()
        with col2:
            if st.button("❌ Cancel"):
                st.session_state.pending_write = None
                st.rerun()
        st.stop()  # Don't show chat input while approval is pending

    # ── Chat input ───────────────────────────
    if user_input := st.chat_input(f"Ask about {schema.domain}..."):
        # Show user message immediately
        with st.chat_message("user"):
            st.write(user_input)

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
        }

        with st.spinner("Thinking..."):
            result = agent.invoke(initial_state)

        # Check if write approval needed
        if result.get("approved") is None and result.get("permission_level") == "confirm":
            # Store pending write for approval UI (shown on next rerun)
            st.session_state.pending_write = {**result, "user_query": user_input}
            st.rerun()

        # Normal result — add to history and display
        with st.chat_message("assistant"):
            _display_results(result)

        st.session_state.chat_history.append({
            "query": user_input,
            "result": result,
        })


if __name__ == "__main__":
    main()
