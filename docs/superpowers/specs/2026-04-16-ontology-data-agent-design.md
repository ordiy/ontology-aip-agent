# Ontology-Driven Data Agent MVP — Design Spec

**Date:** 2026-04-16
**Status:** Draft
**Approach:** Standalone Python project, reusing Ontology-Playground's RDF/OWL files as data input

## 1. Overview

A Palantir AIP-style data agent that uses ontology definitions to understand domain semantics, auto-generates a SQLite database with mock data, and enables users to query and operate on data through natural language via a LangGraph-based agent.

### Core Principle

**Operations = Decisions.** The agent is not a read-only report tool. It can propose and execute write operations (with user approval), aligning with Palantir AIP's philosophy that agents should be able to take action.

### MVP Scope

- Natural language → ontology understanding → SQL generation → execution → formatted result
- Read operations execute automatically; write operations require user confirmation
- CLI interface with domain selection and conversational interaction
- Vertex AI Gemini as LLM, with abstraction layer for future Ollama support

### Out of Scope (MVP)

- Multi-step analysis and trend detection
- Visualization (charts/graphs)
- Report generation
- Web UI (post-MVP)
- Real database/API connections (post-MVP)

## 2. Architecture

```
┌─────────────────────────────────────────────────────┐
│                     User (CLI)                       │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│              LangGraph Agent                         │
│                                                      │
│  load_ontology_context                               │
│       → classify_intent (LLM)                        │
│            → generate_sql (LLM)                      │
│                 → [human_approval if WRITE]           │
│                      → execute_sql                   │
│                           → format_result (LLM)      │
└────────┬──────────────┬─────────────────────────────┘
         │              │
         ▼              ▼
┌──────────────┐  ┌──────────────┐
│  Ontology    │  │   SQLite     │
│  Store       │  │   Database   │
│  (RDF/OWL)   │  │  (mock data) │
└──────────────┘  └──────────────┘
```

### Core Components

| Component | Responsibility |
|-----------|---------------|
| **Ontology Store** | Parse RDF/OWL files, extract classes/properties/relationships, provide semantic context |
| **SQLite DB** | Auto-create tables from ontology schema, populate with Faker-generated mock data |
| **LangGraph Agent** | Stateful graph: intent classification → SQL generation → approval → execution → formatting |
| **LLM Abstraction** | Protocol-based interface, swappable between Vertex AI Gemini and Ollama |
| **CLI Interface** | Domain selection, natural language conversation, system commands |

## 3. Technology Stack

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| LLM | `google-cloud-aiplatform` + `gemini-3.1-pro-preview` | User's available Vertex AI access |
| Agent | `langgraph` | Stateful graph orchestration |
| Ontology parsing | `rdflib` | Python standard for RDF/OWL |
| Database | `sqlite3` (stdlib) | Zero deployment, built into Python |
| Mock data | `Faker` | Realistic data generation by type |
| CLI output | `rich` | Tables, syntax highlighting, formatted output |
| Config | `pyyaml` | Human-readable configuration |

## 4. LLM Abstraction Layer

### Interface

```python
from typing import Protocol

class LLMClient(Protocol):
    def chat(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        temperature: float = 0.0,
    ) -> str: ...

    def get_model_name(self) -> str: ...
```

### Implementations

- **MVP:** `VertexGeminiClient` — uses `google-cloud-aiplatform` SDK
- **Future:** `OllamaClient` — uses Ollama REST API at `localhost:11434`

### Configuration

Provider selected via `config.yaml` or environment variable `LLM_PROVIDER`. Agent nodes depend only on `LLMClient` protocol, never on concrete implementations.

## 5. LangGraph Agent Design

### State Definition

```python
class AgentState(TypedDict):
    messages: list              # Conversation history
    ontology_context: str       # Ontology semantic description (classes/properties/relationships)
    user_query: str             # User's natural language question
    intent: str                 # READ / WRITE / UNCLEAR
    generated_sql: str          # LLM-generated SQL
    permission_level: str       # auto / confirm / deny
    approved: bool | None       # User approval result
    query_result: list[dict]    # SQL execution result
    response: str               # Final response text to user
```

### Graph Nodes

| Node | Input | Output | LLM Call |
|------|-------|--------|----------|
| `load_ontology_context` | Selected domain | `ontology_context` | No |
| `classify_intent` | `user_query` + `ontology_context` | `intent` | Yes |
| `generate_sql` | `user_query` + `ontology_context` + `intent` | `generated_sql` + `permission_level` | Yes |
| `human_approval` | `generated_sql` | `approved` | No (waits for user) |
| `execute_sql` | `generated_sql` | `query_result` | No |
| `format_result` | `query_result` + `user_query` | `response` | Yes |
| `clarify_question` | `user_query` | Follow-up question | Yes |

### Graph Flow

```
START
  → load_ontology_context
    → classify_intent
      ├─ [READ]    → generate_sql → execute_sql (auto) → format_result → END
      ├─ [WRITE]   → generate_sql → human_approval
      │                              ├─ [approve] → execute_sql → format_result → END
      │                              └─ [reject]  → format_result (cancelled) → END
      └─ [UNCLEAR] → clarify_question → classify_intent (loop back, max 2 retries then give up)
```

**3 LLM calls per query** (intent, SQL, formatting). Clarification loop capped at 2 retries to prevent infinite loops. All other nodes are pure logic.

## 6. Permission Layer

### Permission Levels

| Level | Operations | Default Approval |
|-------|-----------|-----------------|
| READ | SELECT | auto |
| WRITE | INSERT, UPDATE | confirm (user y/n) |
| DELETE | DELETE | confirm (user y/n) |
| ADMIN | DROP, CREATE, ALTER | deny |

### Approval Modes

- **auto:** Agent executes immediately
- **confirm:** Display SQL to user, wait for y/n confirmation
- **deny:** Refuse execution, explain why

Configurable per level in `config.yaml`. MVP defaults shown above.

## 7. Ontology Parsing

### Input

RDF/OWL files from Ontology-Playground's catalogue (6 domains: Retail, E-Commerce, Healthcare, Finance, Manufacturing, Education).

### Output: OntologySchema

```python
@dataclass
class OntologyProperty:
    name: str
    data_type: str      # string, integer, float, date, boolean

@dataclass
class OntologyRelationship:
    source: str
    target: str
    name: str
    cardinality: str    # 1:1, 1:N, M:N

@dataclass
class OntologyClass:
    name: str
    properties: list[OntologyProperty]

@dataclass
class OntologySchema:
    domain: str
    classes: list[OntologyClass]
    relationships: list[OntologyRelationship]
```

### Ontology → LLM Context

Parsed schema is converted to a structured text block injected into the LLM system prompt:

```
Domain: Retail
Tables: customers, orders, products, order_items

Table: customers
  Columns: id (INTEGER PK), name (TEXT), email (TEXT), age (INTEGER)

Table: orders
  Columns: id (INTEGER PK), customer_id (INTEGER FK→customers),
           order_date (TEXT), total_amount (REAL), status (TEXT)

Relationships:
  - customers 1:N orders (via orders.customer_id)
  - orders N:M products (via order_items)
```

## 8. Data Layer

### OWL → SQLite Mapping

| OWL Concept | SQLite Mapping |
|-------------|---------------|
| Class | TABLE |
| DatatypeProperty (string) | TEXT |
| DatatypeProperty (integer) | INTEGER |
| DatatypeProperty (float) | REAL |
| DatatypeProperty (date/dateTime) | TEXT (ISO 8601) |
| DatatypeProperty (boolean) | INTEGER (0/1) |
| ObjectProperty (1:N) | Foreign key column `{class}_id` on the N-side table |
| ObjectProperty (M:N) | Junction table `{class_a}_{class_b}` |

Every table gets an auto-increment `id INTEGER PRIMARY KEY`.

### Mock Data Generation

Property name heuristics map to appropriate Faker methods:

| Property name pattern | Faker method |
|----------------------|-------------|
| `*name*` | `faker.name()` |
| `*email*` | `faker.email()` |
| `*phone*` | `faker.phone_number()` |
| `*price*`, `*amount*` | `faker.pyfloat(min_value=0, max_value=10000)` |
| `*date*` | `faker.date_this_year()` |
| `*status*` | `faker.random_element(["active", "pending", "completed"])` |
| `*address*` | `faker.address()` |
| Default string | `faker.text(max_nb_chars=50)` |
| Default integer | `faker.random_int(min=0, max=1000)` |
| Default float | `faker.pyfloat(min_value=0, max_value=10000)` |
| Default boolean | `faker.boolean()` |

**Data volume:** 50-200 rows per table (configurable). Foreign keys are auto-linked to existing records.

### SQL Executor

- Parses SQL to determine operation type (SELECT/INSERT/UPDATE/DELETE/DDL)
- Maps operation to permission level
- Returns `list[dict]` for SELECT, affected row count for writes
- Query timeout: 5 seconds

## 9. CLI Interface

### Startup Flow

```
$ python -m src.cli.app

🔧 Initializing Ontology Data Agent...

Available domains:
  [1] Retail
  [2] E-Commerce
  [3] Healthcare
  [4] Finance
  [5] Manufacturing
  [6] Education

Select domain: 1

📦 Loading Retail ontology...
🗄️  Creating SQLite database (retail.db)...
📊 Generating mock data: 150 customers, 500 orders, 80 products...
✅ Ready.

retail> _
```

### Interaction Modes

**Natural language** (default): Any input without `.` prefix goes to the agent.

**System commands** (`.` prefix):

| Command | Function |
|---------|----------|
| `.switch <domain>` | Switch ontology domain |
| `.tables` | List all tables in current domain |
| `.schema <table>` | Show table structure |
| `.ontology` | Show ontology relationships (text) |
| `.history` | Show conversation history |
| `.quit` | Exit |

### READ Operation Display

```
retail> 哪个客户的总消费金额最高？

🤔 Intent: READ
📝 SQL:
   SELECT c.name, SUM(o.total_amount) as total_spent
   FROM customers c JOIN orders o ON o.customer_id = c.id
   GROUP BY c.id ORDER BY total_spent DESC LIMIT 1

┌──────────┬─────────────┐
│ name     │ total_spent │
├──────────┼─────────────┤
│ Zhang San│ 28,450.00   │
└──────────┴─────────────┘

💬 Zhang San is the highest-spending customer with a total of 28,450.00.
```

### WRITE Operation Display

```
retail> 把所有逾期订单标记为取消

🤔 Intent: WRITE
📝 SQL:
   UPDATE orders SET status = 'cancelled' WHERE status = 'overdue'
   -- Estimated affected rows: 12

⚠️  This is a WRITE operation. Execute? [y/n]: y

✅ Updated 12 records.
```

## 10. Configuration

### config.yaml

```yaml
llm:
  provider: vertex          # vertex / ollama
  model: gemini-3.1-pro-preview
  temperature: 0.0

vertex:
  project: mydevproject20260304
  location: global
  credentials: /home/openclaw/.gemini/mydevproject20260304-78c063107af2.json

ollama:
  host: http://localhost:11434
  model: llama3

database:
  path: ./data/
  mock_rows_per_table: 100

permissions:
  read: auto
  write: confirm
  delete: confirm
  admin: deny
```

### Environment Variable Override

Priority: environment variable > config.yaml > defaults.

```bash
export LLM_PROVIDER=ollama
export LLM_MODEL=qwen2
export GOOGLE_CLOUD_PROJECT=mydevproject20260304
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/credentials.json
```

## 11. Error Handling

| Scenario | Handling |
|----------|---------|
| RDF file parse failure | Skip file, list available ontologies |
| Gemini generates invalid SQL | Catch SQLite exception, feed error back to LLM, retry once |
| SQL retry still fails | Show error details, do not loop infinitely |
| SQL execution timeout (>5s) | Kill query, prompt user to simplify |
| Vertex AI unreachable | Show network/auth error with troubleshooting steps |
| Query unrelated to ontology | Agent responds "out of scope for current domain", suggest `.switch` |

## 12. Project Structure

```
dev_aip_ontology_agent/
├── config.yaml
├── pyproject.toml
├── README.md
├── ontologies/                # RDF/OWL files from Ontology-Playground
│   ├── retail.rdf
│   ├── ecommerce.rdf
│   ├── healthcare.rdf
│   ├── finance.rdf
│   ├── manufacturing.rdf
│   └── education.rdf
├── data/                      # SQLite DBs (runtime generated, gitignored)
├── src/
│   ├── __init__.py
│   ├── config.py              # Config loading (yaml + env)
│   ├── ontology/
│   │   ├── __init__.py
│   │   ├── parser.py          # RDF/OWL → OntologySchema
│   │   └── context.py         # OntologySchema → LLM prompt context
│   ├── database/
│   │   ├── __init__.py
│   │   ├── schema.py          # OntologySchema → SQLite DDL
│   │   ├── mock_data.py       # Faker mock data generation
│   │   └── executor.py        # SQL execution + permission check
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── graph.py           # LangGraph state graph definition
│   │   ├── nodes.py           # Node implementations
│   │   ├── state.py           # AgentState type definition
│   │   └── tools.py           # Agent-callable tools
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── base.py            # LLMClient Protocol
│   │   └── vertex.py          # VertexGeminiClient
│   └── cli/
│       ├── __init__.py
│       └── app.py             # CLI entry point + conversation loop
└── tests/
    ├── test_parser.py
    ├── test_schema.py
    ├── test_executor.py
    └── test_agent.py
```

## 13. Post-MVP Roadmap

1. **Web UI** — Streamlit/Chainlit chat interface
2. **Ollama support** — Local model deployment via `OllamaClient`
3. **Multi-step analysis** — Agent chains multiple queries for complex questions
4. **Visualization** — matplotlib/plotly chart generation
5. **Real data sources** — PostgreSQL, CSV upload, API connectors
6. **Report generation** — Auto-generated summaries and insights
