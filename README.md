# Ontology-Driven Data Agent

A [Palantir AIP](https://www.palantir.com/platforms/aip/)-style data agent that uses OWL/RDF ontology definitions to understand domain semantics, auto-generates a SQLite database with realistic mock data, and lets users query and operate on data through natural language.

> **Operations = Decisions.** The agent is not a read-only report tool — it can propose and execute write operations with user approval, aligning with Palantir AIP's philosophy that agents should take action.

---

## Screenshots

### CLI — Domain Selection, READ / ANALYZE / WRITE
![CLI demo](docs/images/cli_demo.png)

### Web UI — Startup (domain selector + entity overview)
![Web UI startup](docs/images/web_ui_query.png)

### Web UI — Query Result with Auto Chart + CSV Export
![Web UI result with chart](docs/images/web_ui_analyze.png)

---

## Purpose

| Goal | Detail |
|------|--------|
| Natural language → SQL | User asks a question in plain English or Chinese; the agent classifies intent, generates SQL, executes it, and returns a formatted answer |
| Ontology-aware | Domain semantics come from OWL/RDF files — the agent understands entity types, properties, and relationships without hard-coding any schema |
| Permission-gated writes | READ queries run automatically; WRITE/DELETE require user confirmation; DDL (DROP/ALTER) is denied |
| Multi-step analysis | Complex questions are decomposed into sub-queries, each executed independently, then synthesized into a unified answer |
| Pluggable LLM backend | Vertex AI Gemini (default) or local Ollama — swap via `config.yaml`, no code changes |

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                  User Interface                               │
│         CLI (rich)              Web UI (Streamlit)            │
└──────────────────┬───────────────────────┬───────────────────┘
                   │                       │
                   └──────────┬────────────┘
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                    LangGraph Agent                            │
│                                                              │
│  load_context → classify_intent                              │
│                    ├─[READ/WRITE]→ generate_sql              │
│                    │                  → execute_sql           │
│                    │                  → format_result         │
│                    ├─[ANALYZE] → plan_analysis               │
│                    │              → execute_analysis_step(×N) │
│                    │              → synthesize_results        │
│                    └─[UNCLEAR] → clarify (max 2) / give_up   │
└──────┬────────────────────┬────────────────────┬────────────┘
       │                    │                    │
       ▼                    ▼                    ▼
┌─────────────┐   ┌──────────────────┐  ┌──────────────────┐
│  Ontology   │   │   LLM Client     │  │  SQL Executor    │
│  Store      │   │  (Vertex Gemini  │  │  (SQLite)        │
│  (RDF/OWL)  │   │   or Ollama)     │  │  + Permission    │
└─────────────┘   └──────────────────┘  └──────────────────┘
```

### Component Overview

| Component | File(s) | Responsibility |
|-----------|---------|----------------|
| **Ontology Parser** | `src/ontology/parser.py` | Parse RDF/OWL → `OntologySchema` dataclass |
| **Context Generator** | `src/ontology/context.py` | Convert schema to plain-text SQL description for LLM prompt |
| **Agent Graph** | `src/agent/graph.py` | LangGraph `StateGraph` with conditional routing |
| **Agent Nodes** | `src/agent/nodes.py` | `classify_intent`, `generate_sql`, `execute_sql_node`, `format_result`, `plan_analysis`, `execute_analysis_step`, `synthesize_results` |
| **SQL Executor** | `src/database/executor.py` | `BaseExecutor` ABC + `SQLiteExecutor`; permission control; 5-second timeout |
| **Mock Data** | `src/database/mock_data.py` | Faker-based data generation with FK linking |
| **LLM Abstraction** | `src/llm/base.py` | `LLMClient` Protocol |
| **Vertex AI Client** | `src/llm/vertex.py` | Gemini via `google-cloud-aiplatform` |
| **Ollama Client** | `src/llm/ollama.py` | Local models via Ollama REST API |
| **CLI** | `src/cli/app.py` | Domain selection, conversation loop, rich output |
| **Web UI** | `src/web/app.py` | Streamlit chat interface |
| **Visualizer** | `src/web/visualizer.py` | Auto chart type detection (bar/line/pie/area/stacked_bar) via Plotly |
| **Connectors** | `src/database/connectors.py` | `DataConnector` ABC + `MockMarketPriceConnector` for external data simulation |

### Intent Routing

```
User query
    │
    ▼
classify_intent (LLM)
    ├─ READ    → single SELECT → format answer
    ├─ WRITE   → INSERT/UPDATE → user confirms → execute
    ├─ ANALYZE → decompose into 2-4 sub-queries → execute each → synthesize
    └─ UNCLEAR → ask clarifying question (max 2 retries)
```

### Permission Levels

| Level | Operations | Default |
|-------|-----------|---------|
| `auto` | SELECT, WITH | executes immediately |
| `confirm` | INSERT, UPDATE, DELETE | prompts user y/n |
| `deny` | DROP, CREATE, ALTER, TRUNCATE | always refused |

---

## Quick Start

### Prerequisites

- Python 3.11+
- One of:
  - Google Cloud project with Vertex AI enabled + service account JSON
  - [Ollama](https://ollama.com) running locally with a model pulled

### Install

```bash
git clone https://github.com/ordiy/dev_aip_ontology_agent.git
cd dev_aip_ontology_agent
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

### Configure

Copy and edit the config:

```bash
cp config.yaml config.local.yaml   # local overrides (gitignored)
```

**Vertex AI (default):**
```yaml
llm:
  provider: vertex
  model: gemini-3.1-pro-preview

vertex:
  project: YOUR_GCP_PROJECT_ID
  location: global
  credentials: /path/to/service-account.json
```

**Ollama (local, no cloud required):**
```yaml
llm:
  provider: ollama
  model: llama3

ollama:
  host: http://localhost:11434
```

**OpenRouter (200+ models — Claude, Gemini, Llama, etc.):**
```yaml
llm:
  provider: openrouter

openrouter:
  api_key: ""        # or export OPENROUTER_API_KEY=sk-or-v1-...
  model: anthropic/claude-3.5-sonnet   # any model at openrouter.ai/models
```

**OpenAI:**
```yaml
llm:
  provider: openai

openai:
  api_key: ""        # or export OPENAI_API_KEY=sk-...
  model: gpt-4o
```

> API keys must never be committed. Put them in `config.local.yaml` (gitignored) or use environment variables.

### Run

**CLI:**
```bash
python -m src
```

**Web UI:**
```bash
streamlit run src/web/app.py
# Opens at http://localhost:8501
```

---

## Test Data

No external database required. On startup, the agent automatically:

1. Parses the selected ontology RDF file
2. Creates a SQLite database from the schema
3. Populates it with Faker-generated mock data (configurable rows per table)

### Ontology Domains

| Domain | File | Entities |
|--------|------|----------|
| **E-Commerce** | `ontologies/ecommerce.rdf` | Buyer, Product, Shopping-Cart, Order, Review |
| **Finance** | `ontologies/finance.rdf` | Account, Transaction, Portfolio, Asset, Statement |
| **Retail** | `ontologies/retail.rdf` | Customer, Product, Store, Order, Inventory |
| **Healthcare** | `ontologies/healthcare.rdf` | Patient, Doctor, Appointment, Prescription, Diagnosis |
| **Manufacturing** | `ontologies/manufacturing.rdf` | Product, Component, Supplier, WorkOrder, Inventory |
| **Education** | `ontologies/education.rdf` | Student, Course, Instructor, Enrollment, Grade |

### Ontology → Database Mapping

| OWL Concept | SQLite Mapping |
|-------------|---------------|
| `owl:Class` | TABLE with auto-increment `id` PK |
| `owl:DatatypeProperty` (string) | TEXT |
| `owl:DatatypeProperty` (integer) | INTEGER |
| `owl:DatatypeProperty` (decimal/float) | REAL |
| `owl:DatatypeProperty` (date/dateTime) | TEXT (ISO 8601) |
| `owl:DatatypeProperty` (boolean) | INTEGER (0/1) |
| `owl:ObjectProperty` (1:N) | FK column `{entity}_id` on the N-side table |
| `owl:ObjectProperty` (M:N) | Junction table `{table_a}_{table_b}` |

### Mock Data Volume

Configurable in `config.yaml`:
```yaml
database:
  mock_rows_per_table: 100   # rows generated per entity table
```

---

## Usage

### CLI Commands

| Command | Description |
|---------|-------------|
| `<natural language>` | Query or instruct in plain English/Chinese |
| `.tables` | List all tables in current domain |
| `.schema <table>` | Show column structure for a table |
| `.ontology` | Print ontology relationships |
| `.history` | Show conversation history |
| `.history clear` | Clear conversation history |
| `.switch <domain>` | Switch to a different ontology domain |
| `.switch` | List available domains |
| `.quit` | Exit |

### Example Queries

```
ecommerce> Which buyer has placed the most orders?
ecommerce> Show me all products with stock below 10
ecommerce> Compare revenue this month vs last month      ← ANALYZE intent
ecommerce> Update order #42 status to shipped            ← WRITE (requires confirm)
```

### Web UI Features

- Chat interface with conversation history
- Auto-rendered data table for query results
- **CSV export** button for every result set
- **Auto chart** — detects best visualization:
  - `bar` — category + single numeric column
  - `pie` — ≤8 categories, all positive values
  - `line` — date/time column + numeric
  - `area` — cumulative time series (monotonic or column name contains `total`/`sum`)
  - `stacked_bar` — 2 category columns + 1 numeric column

---

## Project Structure

```
dev_aip_ontology_agent/
├── config.yaml                  # Main configuration
├── pyproject.toml
├── ontologies/                  # OWL/RDF domain definitions
│   ├── ecommerce.rdf
│   ├── finance.rdf
│   ├── retail.rdf
│   ├── healthcare.rdf
│   ├── manufacturing.rdf
│   └── education.rdf
├── src/
│   ├── config.py                # Config loader (yaml + env var override)
│   ├── ontology/
│   │   ├── parser.py            # RDF/OWL → OntologySchema dataclass
│   │   └── context.py           # Schema → LLM prompt text
│   ├── database/
│   │   ├── schema.py            # Schema → SQLite DDL + table creation
│   │   ├── mock_data.py         # Faker-based data generation
│   │   ├── executor.py          # BaseExecutor ABC + SQLiteExecutor
│   │   └── connectors.py        # DataConnector ABC + MockMarketPriceConnector
│   ├── agent/
│   │   ├── graph.py             # LangGraph StateGraph
│   │   ├── nodes.py             # All agent node functions
│   │   └── state.py             # AgentState TypedDict
│   ├── llm/
│   │   ├── base.py              # LLMClient Protocol
│   │   ├── vertex.py            # Vertex AI Gemini client
│   │   └── ollama.py            # Ollama local model client
│   ├── cli/
│   │   └── app.py               # CLI entry point
│   └── web/
│       ├── app.py               # Streamlit web UI
│       └── visualizer.py        # Plotly chart type detection + rendering
├── tests/                       # 101 tests (pytest)
│   ├── test_parser.py
│   ├── test_schema.py
│   ├── test_executor.py
│   ├── test_agent.py
│   ├── test_web.py
│   └── test_connectors.py
└── docs/
    └── superpowers/
        ├── specs/               # Design spec
        └── plans/               # Implementation plan
```

---

## Running Tests

```bash
pytest
# 101 passed
```

---

## Configuration Reference

| Key | Default | Description |
|-----|---------|-------------|
| `llm.provider` | `vertex` | `vertex` or `ollama` |
| `llm.model` | `gemini-3.1-pro-preview` | Model name |
| `llm.temperature` | `0.0` | LLM temperature |
| `vertex.project` | — | GCP project ID |
| `vertex.location` | `global` | Vertex AI region |
| `vertex.credentials` | — | Path to service account JSON |
| `ollama.host` | `http://localhost:11434` | Ollama server URL |
| `ollama.model` | `llama3` | Ollama model name |
| `ollama.timeout` | `120` | Request timeout (seconds) |
| `database.path` | `./data/` | SQLite database directory |
| `database.mock_rows_per_table` | `100` | Rows generated per table |
| `permissions.read` | `auto` | SELECT permission mode |
| `permissions.write` | `confirm` | INSERT/UPDATE permission mode |
| `permissions.delete` | `confirm` | DELETE permission mode |
| `permissions.admin` | `deny` | DDL permission mode |

Environment variables override `config.yaml`:

```bash
export LLM_PROVIDER=ollama
export LLM_MODEL=qwen2
export GOOGLE_CLOUD_PROJECT=my-project
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/creds.json
```

---

## References

| Resource | Link |
|----------|------|
| Palantir AIP — Ontology & Agents | https://www.palantir.com/platforms/aip/ |
| Microsoft Ontology-Playground (RDF domain files) | https://github.com/microsoft/Ontology-Playground |
| LangGraph documentation | https://langchain-ai.github.io/langgraph/ |
| rdflib (Python RDF library) | https://rdflib.readthedocs.io/ |
| OWL Web Ontology Language (W3C) | https://www.w3.org/OWL/ |
| Vertex AI Gemini API | https://cloud.google.com/vertex-ai/generative-ai/docs |
| OpenRouter (200+ models, OpenAI-compatible) | https://openrouter.ai |
| OpenAI API | https://platform.openai.com/docs/api-reference |
| Ollama | https://ollama.com |
| Streamlit | https://streamlit.io |
| Faker (mock data) | https://faker.readthedocs.io/ |
| Plotly Express | https://plotly.com/python/plotly-express/ |
