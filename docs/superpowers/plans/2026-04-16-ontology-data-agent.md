# Ontology Data Agent MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Palantir AIP-style data agent that parses RDF/OWL ontologies, auto-generates SQLite databases with mock data, and answers natural language queries via a LangGraph agent powered by Vertex AI Gemini.

**Architecture:** Standalone Python project with 5 layers — ontology parsing (rdflib), database (sqlite3 + Faker), LLM abstraction (Protocol-based, Vertex AI), LangGraph agent (stateful graph with intent classification, SQL generation, permission-gated execution, result formatting), and CLI (rich). RDF/OWL files sourced from Microsoft Ontology-Playground.

**Tech Stack:** Python 3.11+, langgraph, rdflib, google-cloud-aiplatform, Faker, rich, pyyaml, pytest

---

## File Structure

```
dev_aip_ontology_agent/
├── config.yaml                 # Runtime configuration
├── pyproject.toml              # Project metadata + dependencies
├── ontologies/                 # RDF/OWL files from Ontology-Playground
│   └── ecommerce.rdf          # MVP: start with one domain
├── data/                       # SQLite DBs (runtime, gitignored)
├── src/
│   ├── __init__.py
│   ├── config.py               # Config loading (yaml + env override)
│   ├── ontology/
│   │   ├── __init__.py
│   │   ├── parser.py           # RDF/OWL → OntologySchema dataclasses
│   │   └── context.py          # OntologySchema → LLM prompt text
│   ├── database/
│   │   ├── __init__.py
│   │   ├── schema.py           # OntologySchema → SQLite DDL + create tables
│   │   ├── mock_data.py        # Faker-based mock data generation
│   │   └── executor.py         # SQL execution + permission check
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── state.py            # AgentState TypedDict
│   │   ├── nodes.py            # Graph node implementations
│   │   └── graph.py            # LangGraph state graph wiring
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── base.py             # LLMClient Protocol
│   │   └── vertex.py           # VertexGeminiClient
│   └── cli/
│       ├── __init__.py
│       └── app.py              # CLI entry point + conversation loop
└── tests/
    ├── __init__.py
    ├── conftest.py             # Shared fixtures (sample RDF, temp DB)
    ├── test_parser.py
    ├── test_context.py
    ├── test_schema.py
    ├── test_mock_data.py
    ├── test_executor.py
    ├── test_nodes.py
    └── test_config.py
```

---

### Task 1: Project Scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `config.yaml`
- Create: `src/__init__.py`
- Create: `tests/__init__.py`
- Modify: `.gitignore` (append `data/` directory)

- [ ] **Step 1: Create pyproject.toml**

```toml
[project]
name = "ontology-data-agent"
version = "0.1.0"
description = "Ontology-driven data agent MVP"
requires-python = ">=3.11"
dependencies = [
    "langgraph>=0.4.0",
    "langchain-core>=0.3.0",
    "rdflib>=7.0.0",
    "google-cloud-aiplatform>=1.60.0",
    "Faker>=28.0.0",
    "rich>=13.0.0",
    "pyyaml>=6.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
]

[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.backends._legacy:_Backend"

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
```

- [ ] **Step 2: Create config.yaml**

```yaml
llm:
  provider: vertex
  model: gemini-3.1-pro-preview
  temperature: 0.0

vertex:
  project: mydevproject20260304
  location: global

database:
  path: ./data/
  mock_rows_per_table: 100

permissions:
  read: auto
  write: confirm
  delete: confirm
  admin: deny
```

- [ ] **Step 3: Create src/__init__.py and tests/__init__.py**

Both empty files:
```python
```

- [ ] **Step 4: Append data/ to .gitignore**

Add at the end of `.gitignore`:
```
# Runtime SQLite databases
data/
```

- [ ] **Step 5: Download ecommerce.rdf from Ontology-Playground**

```bash
mkdir -p ontologies
curl -sL https://raw.githubusercontent.com/microsoft/Ontology-Playground/main/catalogue/official/ecommerce/ecommerce.rdf -o ontologies/ecommerce.rdf
```

- [ ] **Step 6: Install dependencies and verify**

```bash
pip install -e ".[dev]"
pytest --co -q
```

Expected: `no tests ran` (no test files yet), no import errors.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml config.yaml src/__init__.py tests/__init__.py .gitignore ontologies/
git commit -m "feat: project scaffolding with dependencies and ecommerce ontology"
```

---

### Task 2: Configuration Loading

**Files:**
- Create: `src/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

`tests/test_config.py`:
```python
import os
from pathlib import Path
from src.config import load_config


def test_load_config_from_yaml(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "llm:\n"
        "  provider: vertex\n"
        "  model: gemini-test\n"
        "  temperature: 0.5\n"
        "vertex:\n"
        "  project: test-project\n"
        "  location: us-central1\n"
        "database:\n"
        "  path: ./data/\n"
        "  mock_rows_per_table: 50\n"
        "permissions:\n"
        "  read: auto\n"
        "  write: confirm\n"
        "  delete: confirm\n"
        "  admin: deny\n"
    )
    config = load_config(str(config_file))
    assert config["llm"]["provider"] == "vertex"
    assert config["llm"]["model"] == "gemini-test"
    assert config["llm"]["temperature"] == 0.5
    assert config["vertex"]["project"] == "test-project"
    assert config["database"]["mock_rows_per_table"] == 50
    assert config["permissions"]["write"] == "confirm"


def test_env_vars_override_yaml(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "llm:\n"
        "  provider: vertex\n"
        "  model: gemini-test\n"
        "  temperature: 0.0\n"
        "vertex:\n"
        "  project: yaml-project\n"
        "  location: global\n"
        "database:\n"
        "  path: ./data/\n"
        "  mock_rows_per_table: 100\n"
        "permissions:\n"
        "  read: auto\n"
        "  write: confirm\n"
        "  delete: confirm\n"
        "  admin: deny\n"
    )
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "llama3")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "env-project")
    config = load_config(str(config_file))
    assert config["llm"]["provider"] == "ollama"
    assert config["llm"]["model"] == "llama3"
    assert config["vertex"]["project"] == "env-project"


def test_load_config_defaults_when_no_file():
    config = load_config("/nonexistent/config.yaml")
    assert config["llm"]["provider"] == "vertex"
    assert config["database"]["mock_rows_per_table"] == 100
    assert config["permissions"]["read"] == "auto"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_config.py -v
```

Expected: `ModuleNotFoundError: No module named 'src.config'`

- [ ] **Step 3: Write minimal implementation**

`src/config.py`:
```python
from pathlib import Path
import os
import yaml


DEFAULTS = {
    "llm": {
        "provider": "vertex",
        "model": "gemini-3.1-pro-preview",
        "temperature": 0.0,
    },
    "vertex": {
        "project": "",
        "location": "global",
    },
    "ollama": {
        "host": "http://localhost:11434",
        "model": "llama3",
    },
    "database": {
        "path": "./data/",
        "mock_rows_per_table": 100,
    },
    "permissions": {
        "read": "auto",
        "write": "confirm",
        "delete": "confirm",
        "admin": "deny",
    },
}

ENV_OVERRIDES = {
    "LLM_PROVIDER": ("llm", "provider"),
    "LLM_MODEL": ("llm", "model"),
    "GOOGLE_CLOUD_PROJECT": ("vertex", "project"),
    "GOOGLE_CLOUD_LOCATION": ("vertex", "location"),
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path: str = "config.yaml") -> dict:
    config = {k: dict(v) for k, v in DEFAULTS.items()}

    path = Path(config_path)
    if path.exists():
        with open(path) as f:
            file_config = yaml.safe_load(f) or {}
        config = _deep_merge(config, file_config)

    for env_var, (section, key) in ENV_OVERRIDES.items():
        value = os.environ.get(env_var)
        if value is not None:
            config[section][key] = value

    return config
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_config.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/config.py tests/test_config.py
git commit -m "feat: config loading with yaml + env override"
```

---

### Task 3: Ontology Parser

**Files:**
- Create: `src/ontology/__init__.py`
- Create: `src/ontology/parser.py`
- Create: `tests/conftest.py`
- Create: `tests/test_parser.py`

- [ ] **Step 1: Create shared test fixture with sample RDF**

`tests/conftest.py`:
```python
import pytest
from pathlib import Path

SAMPLE_RDF = """\
<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF
    xml:base="http://example.org/ontology/test/"
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
    xmlns:owl="http://www.w3.org/2002/07/owl#"
    xmlns:xsd="http://www.w3.org/2001/XMLSchema#"
    xmlns:ont="http://example.org/ontology/test/">

    <owl:Ontology rdf:about="http://example.org/ontology/test/">
        <rdfs:label>Test Store</rdfs:label>
    </owl:Ontology>

    <owl:Class rdf:about="http://example.org/ontology/test/Customer">
        <rdfs:label>Customer</rdfs:label>
        <rdfs:comment>A registered customer</rdfs:comment>
    </owl:Class>

    <owl:Class rdf:about="http://example.org/ontology/test/Order">
        <rdfs:label>Order</rdfs:label>
        <rdfs:comment>A purchase order</rdfs:comment>
    </owl:Class>

    <owl:Class rdf:about="http://example.org/ontology/test/Product">
        <rdfs:label>Product</rdfs:label>
        <rdfs:comment>An item for sale</rdfs:comment>
    </owl:Class>

    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/customer_name">
        <rdfs:label>name</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Customer"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#string"/>
        <ont:propertyType>string</ont:propertyType>
    </owl:DatatypeProperty>

    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/customer_email">
        <rdfs:label>email</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Customer"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#string"/>
        <ont:propertyType>string</ont:propertyType>
    </owl:DatatypeProperty>

    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/customer_age">
        <rdfs:label>age</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Customer"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#integer"/>
        <ont:propertyType>integer</ont:propertyType>
    </owl:DatatypeProperty>

    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/order_orderId">
        <rdfs:label>orderId</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Order"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#string"/>
        <ont:isIdentifier rdf:datatype="http://www.w3.org/2001/XMLSchema#boolean">true</ont:isIdentifier>
        <ont:propertyType>string</ont:propertyType>
    </owl:DatatypeProperty>

    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/order_total">
        <rdfs:label>total</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Order"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#decimal"/>
        <ont:propertyType>decimal</ont:propertyType>
    </owl:DatatypeProperty>

    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/order_date">
        <rdfs:label>orderDate</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Order"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#date"/>
        <ont:propertyType>date</ont:propertyType>
    </owl:DatatypeProperty>

    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/product_name">
        <rdfs:label>name</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Product"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#string"/>
        <ont:propertyType>string</ont:propertyType>
    </owl:DatatypeProperty>

    <owl:DatatypeProperty rdf:about="http://example.org/ontology/test/product_price">
        <rdfs:label>price</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Product"/>
        <rdfs:range rdf:resource="http://www.w3.org/2001/XMLSchema#decimal"/>
        <ont:propertyType>decimal</ont:propertyType>
    </owl:DatatypeProperty>

    <owl:ObjectProperty rdf:about="http://example.org/ontology/test/customer_places">
        <rdfs:label>places</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Customer"/>
        <rdfs:range rdf:resource="http://example.org/ontology/test/Order"/>
        <ont:cardinality>one-to-many</ont:cardinality>
    </owl:ObjectProperty>

    <owl:ObjectProperty rdf:about="http://example.org/ontology/test/order_contains">
        <rdfs:label>contains</rdfs:label>
        <rdfs:domain rdf:resource="http://example.org/ontology/test/Order"/>
        <rdfs:range rdf:resource="http://example.org/ontology/test/Product"/>
        <ont:cardinality>many-to-many</ont:cardinality>
    </owl:ObjectProperty>

</rdf:RDF>
"""


@pytest.fixture
def sample_rdf_path(tmp_path):
    rdf_file = tmp_path / "test.rdf"
    rdf_file.write_text(SAMPLE_RDF)
    return str(rdf_file)
```

- [ ] **Step 2: Write the failing tests**

`tests/test_parser.py`:
```python
from src.ontology.parser import parse_ontology, OntologySchema, OntologyClass, OntologyProperty, OntologyRelationship


def test_parse_extracts_domain_name(sample_rdf_path):
    schema = parse_ontology(sample_rdf_path)
    assert schema.domain == "Test Store"


def test_parse_extracts_all_classes(sample_rdf_path):
    schema = parse_ontology(sample_rdf_path)
    class_names = {c.name for c in schema.classes}
    assert class_names == {"Customer", "Order", "Product"}


def test_parse_extracts_properties_for_class(sample_rdf_path):
    schema = parse_ontology(sample_rdf_path)
    customer = next(c for c in schema.classes if c.name == "Customer")
    prop_names = {p.name for p in customer.properties}
    assert prop_names == {"name", "email", "age"}


def test_parse_property_types(sample_rdf_path):
    schema = parse_ontology(sample_rdf_path)
    customer = next(c for c in schema.classes if c.name == "Customer")
    age_prop = next(p for p in customer.properties if p.name == "age")
    assert age_prop.data_type == "integer"
    email_prop = next(p for p in customer.properties if p.name == "email")
    assert email_prop.data_type == "string"


def test_parse_identifier_property(sample_rdf_path):
    schema = parse_ontology(sample_rdf_path)
    order = next(c for c in schema.classes if c.name == "Order")
    order_id_prop = next(p for p in order.properties if p.name == "orderId")
    assert order_id_prop.is_identifier is True


def test_parse_decimal_maps_to_float(sample_rdf_path):
    schema = parse_ontology(sample_rdf_path)
    order = next(c for c in schema.classes if c.name == "Order")
    total_prop = next(p for p in order.properties if p.name == "total")
    assert total_prop.data_type == "float"


def test_parse_extracts_relationships(sample_rdf_path):
    schema = parse_ontology(sample_rdf_path)
    assert len(schema.relationships) == 2
    rel_names = {r.name for r in schema.relationships}
    assert rel_names == {"places", "contains"}


def test_parse_relationship_details(sample_rdf_path):
    schema = parse_ontology(sample_rdf_path)
    places = next(r for r in schema.relationships if r.name == "places")
    assert places.source == "Customer"
    assert places.target == "Order"
    assert places.cardinality == "one-to-many"


def test_parse_many_to_many_relationship(sample_rdf_path):
    schema = parse_ontology(sample_rdf_path)
    contains = next(r for r in schema.relationships if r.name == "contains")
    assert contains.source == "Order"
    assert contains.target == "Product"
    assert contains.cardinality == "many-to-many"


def test_parse_nonexistent_file_raises():
    import pytest
    with pytest.raises(FileNotFoundError):
        parse_ontology("/nonexistent/file.rdf")
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_parser.py -v
```

Expected: `ModuleNotFoundError: No module named 'src.ontology'`

- [ ] **Step 4: Write implementation**

`src/ontology/__init__.py`:
```python
```

`src/ontology/parser.py`:
```python
from dataclasses import dataclass, field
from pathlib import Path
from rdflib import Graph, Namespace, RDF, RDFS, OWL, XSD


@dataclass
class OntologyProperty:
    name: str
    data_type: str  # string, integer, float, date, datetime, boolean
    is_identifier: bool = False


@dataclass
class OntologyClass:
    name: str
    properties: list[OntologyProperty] = field(default_factory=list)


@dataclass
class OntologyRelationship:
    source: str
    target: str
    name: str
    cardinality: str  # one-to-one, one-to-many, many-to-one, many-to-many


@dataclass
class OntologySchema:
    domain: str
    classes: list[OntologyClass] = field(default_factory=list)
    relationships: list[OntologyRelationship] = field(default_factory=list)


# Normalize XSD / ont:propertyType values to our internal types
_TYPE_MAP = {
    "string": "string",
    "integer": "integer",
    "int": "integer",
    "decimal": "float",
    "float": "float",
    "double": "float",
    "date": "date",
    "datetime": "datetime",
    "dateTime": "datetime",
    "boolean": "boolean",
    "bool": "boolean",
}

# XSD URI fragment → internal type
_XSD_MAP = {
    str(XSD.string): "string",
    str(XSD.integer): "integer",
    str(XSD.int): "integer",
    str(XSD.decimal): "float",
    str(XSD.float): "float",
    str(XSD.double): "float",
    str(XSD.date): "date",
    str(XSD.dateTime): "datetime",
    str(XSD.boolean): "boolean",
}


def _uri_local_name(uri: str) -> str:
    """Extract local name from a URI (after last / or #)."""
    if "#" in uri:
        return uri.rsplit("#", 1)[1]
    return uri.rsplit("/", 1)[1] if "/" in uri else uri


def parse_ontology(rdf_path: str) -> OntologySchema:
    path = Path(rdf_path)
    if not path.exists():
        raise FileNotFoundError(f"RDF file not found: {rdf_path}")

    g = Graph()
    g.parse(str(path), format="xml")

    # Detect ont: namespace from the ontology's base URI
    ont_ns = None
    for s, p, o in g.triples((None, RDF.type, OWL.Ontology)):
        ont_ns = Namespace(str(s))
        break

    # Extract domain name
    domain = "Unknown"
    for s, p, o in g.triples((None, RDF.type, OWL.Ontology)):
        for _, _, label in g.triples((s, RDFS.label, None)):
            domain = str(label)
            break

    # Extract classes
    classes_by_uri: dict[str, OntologyClass] = {}
    for s, p, o in g.triples((None, RDF.type, OWL.Class)):
        uri = str(s)
        label = _uri_local_name(uri)
        for _, _, lbl in g.triples((s, RDFS.label, None)):
            label = str(lbl)
            break
        classes_by_uri[uri] = OntologyClass(name=label)

    # Extract datatype properties
    for s, p, o in g.triples((None, RDF.type, OWL.DatatypeProperty)):
        prop_uri = str(s)

        # Get label
        prop_name = _uri_local_name(prop_uri)
        for _, _, lbl in g.triples((s, RDFS.label, None)):
            prop_name = str(lbl)
            break

        # Get domain (which class this belongs to)
        domain_uri = None
        for _, _, dom in g.triples((s, RDFS.domain, None)):
            domain_uri = str(dom)
            break

        # Get type from ont:propertyType or rdfs:range
        data_type = "string"
        if ont_ns:
            for _, _, pt in g.triples((s, ont_ns.propertyType, None)):
                raw = str(pt)
                data_type = _TYPE_MAP.get(raw, "string")
                break
        if data_type == "string":
            for _, _, rng in g.triples((s, RDFS.range, None)):
                range_uri = str(rng)
                data_type = _XSD_MAP.get(range_uri, "string")
                break

        # Check if identifier
        is_identifier = False
        if ont_ns:
            for _, _, ident in g.triples((s, ont_ns.isIdentifier, None)):
                is_identifier = str(ident).lower() == "true"
                break

        prop = OntologyProperty(name=prop_name, data_type=data_type, is_identifier=is_identifier)

        if domain_uri and domain_uri in classes_by_uri:
            classes_by_uri[domain_uri].properties.append(prop)

    # Extract object properties (relationships)
    relationships = []
    for s, p, o in g.triples((None, RDF.type, OWL.ObjectProperty)):
        rel_uri = str(s)

        rel_name = _uri_local_name(rel_uri)
        for _, _, lbl in g.triples((s, RDFS.label, None)):
            rel_name = str(lbl)
            break

        source_uri = None
        for _, _, dom in g.triples((s, RDFS.domain, None)):
            source_uri = str(dom)
            break

        target_uri = None
        for _, _, rng in g.triples((s, RDFS.range, None)):
            target_uri = str(rng)
            break

        cardinality = "one-to-many"
        if ont_ns:
            for _, _, card in g.triples((s, ont_ns.cardinality, None)):
                cardinality = str(card)
                break

        if source_uri and target_uri:
            source_name = classes_by_uri[source_uri].name if source_uri in classes_by_uri else _uri_local_name(source_uri)
            target_name = classes_by_uri[target_uri].name if target_uri in classes_by_uri else _uri_local_name(target_uri)
            relationships.append(OntologyRelationship(
                source=source_name,
                target=target_name,
                name=rel_name,
                cardinality=cardinality,
            ))

    return OntologySchema(
        domain=domain,
        classes=list(classes_by_uri.values()),
        relationships=relationships,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_parser.py -v
```

Expected: 11 passed.

- [ ] **Step 6: Commit**

```bash
git add src/ontology/ tests/conftest.py tests/test_parser.py
git commit -m "feat: RDF/OWL ontology parser with dataclass output"
```

---

### Task 4: Ontology Context Generator

**Files:**
- Create: `src/ontology/context.py`
- Create: `tests/test_context.py`

- [ ] **Step 1: Write the failing test**

`tests/test_context.py`:
```python
from src.ontology.parser import OntologySchema, OntologyClass, OntologyProperty, OntologyRelationship
from src.ontology.context import generate_context


def _make_schema():
    return OntologySchema(
        domain="Test Store",
        classes=[
            OntologyClass(
                name="Customer",
                properties=[
                    OntologyProperty(name="name", data_type="string"),
                    OntologyProperty(name="email", data_type="string"),
                    OntologyProperty(name="age", data_type="integer"),
                ],
            ),
            OntologyClass(
                name="Order",
                properties=[
                    OntologyProperty(name="orderId", data_type="string", is_identifier=True),
                    OntologyProperty(name="total", data_type="float"),
                    OntologyProperty(name="orderDate", data_type="date"),
                ],
            ),
        ],
        relationships=[
            OntologyRelationship(source="Customer", target="Order", name="places", cardinality="one-to-many"),
        ],
    )


def test_context_contains_domain():
    schema = _make_schema()
    ctx = generate_context(schema)
    assert "Domain: Test Store" in ctx


def test_context_contains_table_names():
    schema = _make_schema()
    ctx = generate_context(schema)
    assert "customers" in ctx
    assert "orders" in ctx


def test_context_contains_column_definitions():
    schema = _make_schema()
    ctx = generate_context(schema)
    assert "name (TEXT)" in ctx
    assert "age (INTEGER)" in ctx
    assert "total (REAL)" in ctx


def test_context_contains_primary_key():
    schema = _make_schema()
    ctx = generate_context(schema)
    assert "id (INTEGER PK)" in ctx


def test_context_contains_foreign_key():
    schema = _make_schema()
    ctx = generate_context(schema)
    assert "customer_id" in ctx
    assert "FK" in ctx


def test_context_contains_relationships():
    schema = _make_schema()
    ctx = generate_context(schema)
    assert "customers 1:N orders" in ctx
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_context.py -v
```

Expected: `ModuleNotFoundError: No module named 'src.ontology.context'`

- [ ] **Step 3: Write implementation**

`src/ontology/context.py`:
```python
from src.ontology.parser import OntologySchema, OntologyRelationship


_TYPE_TO_SQL = {
    "string": "TEXT",
    "integer": "INTEGER",
    "float": "REAL",
    "date": "TEXT",
    "datetime": "TEXT",
    "boolean": "INTEGER",
}

_CARDINALITY_SHORT = {
    "one-to-one": "1:1",
    "one-to-many": "1:N",
    "many-to-one": "N:1",
    "many-to-many": "M:N",
}


def _table_name(class_name: str) -> str:
    """Convert class name to snake_case table name (plural)."""
    import re
    # CamelCase → snake_case
    name = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", class_name)
    name = re.sub(r"(?<=[A-Z])([A-Z][a-z])", r"_\1", name)
    name = name.lower().replace("-", "_").replace(" ", "_")
    # Simple pluralize
    if name.endswith("y") and not name.endswith("ey"):
        name = name[:-1] + "ies"
    elif name.endswith(("s", "sh", "ch", "x", "z")):
        name = name + "es"
    else:
        name = name + "s"
    return name


def generate_context(schema: OntologySchema) -> str:
    class_to_table = {c.name: _table_name(c.name) for c in schema.classes}
    table_names = ", ".join(class_to_table.values())

    lines = [
        f"Domain: {schema.domain}",
        f"Tables: {table_names}",
        "",
    ]

    # Build FK info from relationships
    fk_columns: dict[str, list[str]] = {}  # table_name -> list of FK column defs
    for rel in schema.relationships:
        source_table = class_to_table.get(rel.source, "")
        target_table = class_to_table.get(rel.target, "")
        if rel.cardinality == "one-to-many":
            # FK on the "many" side (target)
            fk_col = f"{source_table[:-1]}_id" if source_table.endswith("s") else f"{source_table}_id"
            fk_columns.setdefault(target_table, []).append(f"{fk_col} (INTEGER FK->{source_table})")
        elif rel.cardinality == "many-to-one":
            # FK on the "many" side (source)
            fk_col = f"{target_table[:-1]}_id" if target_table.endswith("s") else f"{target_table}_id"
            fk_columns.setdefault(source_table, []).append(f"{fk_col} (INTEGER FK->{target_table})")

    for cls in schema.classes:
        table = class_to_table[cls.name]
        cols = ["id (INTEGER PK)"]
        # Add FK columns for this table
        if table in fk_columns:
            cols.extend(fk_columns[table])
        for prop in cls.properties:
            sql_type = _TYPE_TO_SQL.get(prop.data_type, "TEXT")
            cols.append(f"{prop.name} ({sql_type})")
        lines.append(f"Table: {table}")
        lines.append(f"  Columns: {', '.join(cols)}")
        lines.append("")

    # M:N junction tables
    for rel in schema.relationships:
        if rel.cardinality == "many-to-many":
            source_table = class_to_table.get(rel.source, "")
            target_table = class_to_table.get(rel.target, "")
            junction = f"{source_table}_{target_table}"
            src_fk = f"{source_table[:-1]}_id" if source_table.endswith("s") else f"{source_table}_id"
            tgt_fk = f"{target_table[:-1]}_id" if target_table.endswith("s") else f"{target_table}_id"
            lines.append(f"Table: {junction} (junction)")
            lines.append(f"  Columns: id (INTEGER PK), {src_fk} (INTEGER FK->{source_table}), {tgt_fk} (INTEGER FK->{target_table})")
            lines.append("")

    # Relationships section
    if schema.relationships:
        lines.append("Relationships:")
        for rel in schema.relationships:
            source_table = class_to_table.get(rel.source, "")
            target_table = class_to_table.get(rel.target, "")
            short = _CARDINALITY_SHORT.get(rel.cardinality, rel.cardinality)
            if rel.cardinality == "many-to-many":
                junction = f"{source_table}_{target_table}"
                lines.append(f"  - {source_table} {short} {target_table} (via {junction})")
            elif rel.cardinality == "one-to-many":
                fk_col = f"{source_table[:-1]}_id" if source_table.endswith("s") else f"{source_table}_id"
                lines.append(f"  - {source_table} {short} {target_table} (via {target_table}.{fk_col})")
            elif rel.cardinality == "many-to-one":
                fk_col = f"{target_table[:-1]}_id" if target_table.endswith("s") else f"{target_table}_id"
                lines.append(f"  - {source_table} {short} {target_table} (via {source_table}.{fk_col})")
            else:
                lines.append(f"  - {source_table} {short} {target_table}")

    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_context.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/ontology/context.py tests/test_context.py
git commit -m "feat: ontology context generator for LLM prompt injection"
```

---

### Task 5: Database Schema Builder

**Files:**
- Create: `src/database/__init__.py`
- Create: `src/database/schema.py`
- Create: `tests/test_schema.py`

- [ ] **Step 1: Write the failing test**

`tests/test_schema.py`:
```python
import sqlite3
from src.ontology.parser import OntologySchema, OntologyClass, OntologyProperty, OntologyRelationship
from src.database.schema import create_tables


def _make_schema():
    return OntologySchema(
        domain="Test Store",
        classes=[
            OntologyClass(
                name="Customer",
                properties=[
                    OntologyProperty(name="name", data_type="string"),
                    OntologyProperty(name="email", data_type="string"),
                    OntologyProperty(name="age", data_type="integer"),
                ],
            ),
            OntologyClass(
                name="Order",
                properties=[
                    OntologyProperty(name="orderId", data_type="string", is_identifier=True),
                    OntologyProperty(name="total", data_type="float"),
                    OntologyProperty(name="orderDate", data_type="date"),
                ],
            ),
            OntologyClass(
                name="Product",
                properties=[
                    OntologyProperty(name="name", data_type="string"),
                    OntologyProperty(name="price", data_type="float"),
                ],
            ),
        ],
        relationships=[
            OntologyRelationship(source="Customer", target="Order", name="places", cardinality="one-to-many"),
            OntologyRelationship(source="Order", target="Product", name="contains", cardinality="many-to-many"),
        ],
    )


def test_creates_tables_for_each_class(tmp_path):
    db_path = str(tmp_path / "test.db")
    schema = _make_schema()
    create_tables(db_path, schema)

    conn = sqlite3.connect(db_path)
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()

    assert "customers" in tables
    assert "orders" in tables
    assert "products" in tables


def test_creates_junction_table_for_many_to_many(tmp_path):
    db_path = str(tmp_path / "test.db")
    schema = _make_schema()
    create_tables(db_path, schema)

    conn = sqlite3.connect(db_path)
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()

    assert "orders_products" in tables


def test_table_has_id_primary_key(tmp_path):
    db_path = str(tmp_path / "test.db")
    schema = _make_schema()
    create_tables(db_path, schema)

    conn = sqlite3.connect(db_path)
    cursor = conn.execute("PRAGMA table_info(customers)")
    cols = {row[1]: row for row in cursor.fetchall()}
    conn.close()

    assert "id" in cols
    assert cols["id"][5] == 1  # pk flag


def test_table_has_correct_columns(tmp_path):
    db_path = str(tmp_path / "test.db")
    schema = _make_schema()
    create_tables(db_path, schema)

    conn = sqlite3.connect(db_path)
    cursor = conn.execute("PRAGMA table_info(customers)")
    col_names = {row[1] for row in cursor.fetchall()}
    conn.close()

    assert col_names == {"id", "name", "email", "age"}


def test_foreign_key_column_on_many_side(tmp_path):
    db_path = str(tmp_path / "test.db")
    schema = _make_schema()
    create_tables(db_path, schema)

    conn = sqlite3.connect(db_path)
    cursor = conn.execute("PRAGMA table_info(orders)")
    col_names = {row[1] for row in cursor.fetchall()}
    conn.close()

    assert "customer_id" in col_names


def test_junction_table_has_both_foreign_keys(tmp_path):
    db_path = str(tmp_path / "test.db")
    schema = _make_schema()
    create_tables(db_path, schema)

    conn = sqlite3.connect(db_path)
    cursor = conn.execute("PRAGMA table_info(orders_products)")
    col_names = {row[1] for row in cursor.fetchall()}
    conn.close()

    assert "order_id" in col_names
    assert "product_id" in col_names
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_schema.py -v
```

Expected: `ModuleNotFoundError: No module named 'src.database'`

- [ ] **Step 3: Write implementation**

`src/database/__init__.py`:
```python
```

`src/database/schema.py`:
```python
import sqlite3
from src.ontology.parser import OntologySchema, OntologyRelationship
from src.ontology.context import _table_name


_TYPE_TO_SQLITE = {
    "string": "TEXT",
    "integer": "INTEGER",
    "float": "REAL",
    "date": "TEXT",
    "datetime": "TEXT",
    "boolean": "INTEGER",
}


def _fk_column_name(table_name: str) -> str:
    """Derive FK column name: strip trailing 's' and add '_id'."""
    if table_name.endswith("ies"):
        singular = table_name[:-3] + "y"
    elif table_name.endswith("ses") or table_name.endswith("shes") or table_name.endswith("ches") or table_name.endswith("xes") or table_name.endswith("zes"):
        singular = table_name[:-2]
    elif table_name.endswith("s"):
        singular = table_name[:-1]
    else:
        singular = table_name
    return f"{singular}_id"


def create_tables(db_path: str, schema: OntologySchema) -> dict[str, str]:
    """Create SQLite tables from ontology schema. Returns class_name -> table_name mapping."""
    class_to_table = {c.name: _table_name(c.name) for c in schema.classes}

    # Determine FK columns from relationships
    # fk_additions[table_name] = [(fk_col_name, referenced_table)]
    fk_additions: dict[str, list[tuple[str, str]]] = {}
    for rel in schema.relationships:
        source_table = class_to_table.get(rel.source, "")
        target_table = class_to_table.get(rel.target, "")
        if rel.cardinality == "one-to-many":
            fk_col = _fk_column_name(source_table)
            fk_additions.setdefault(target_table, []).append((fk_col, source_table))
        elif rel.cardinality == "many-to-one":
            fk_col = _fk_column_name(target_table)
            fk_additions.setdefault(source_table, []).append((fk_col, target_table))

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    # Create entity tables
    for cls in schema.classes:
        table = class_to_table[cls.name]
        columns = ["id INTEGER PRIMARY KEY AUTOINCREMENT"]

        # Add FK columns
        for fk_col, ref_table in fk_additions.get(table, []):
            columns.append(f"{fk_col} INTEGER REFERENCES {ref_table}(id)")

        # Add data columns
        for prop in cls.properties:
            sql_type = _TYPE_TO_SQLITE.get(prop.data_type, "TEXT")
            columns.append(f"{prop.name} {sql_type}")

        ddl = f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(columns)})"
        conn.execute(ddl)

    # Create junction tables for M:N relationships
    for rel in schema.relationships:
        if rel.cardinality == "many-to-many":
            source_table = class_to_table.get(rel.source, "")
            target_table = class_to_table.get(rel.target, "")
            junction = f"{source_table}_{target_table}"
            src_fk = _fk_column_name(source_table)
            tgt_fk = _fk_column_name(target_table)
            ddl = (
                f"CREATE TABLE IF NOT EXISTS {junction} ("
                f"id INTEGER PRIMARY KEY AUTOINCREMENT, "
                f"{src_fk} INTEGER REFERENCES {source_table}(id), "
                f"{tgt_fk} INTEGER REFERENCES {target_table}(id))"
            )
            conn.execute(ddl)

    conn.commit()
    conn.close()
    return class_to_table
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_schema.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/database/ tests/test_schema.py
git commit -m "feat: ontology schema to SQLite DDL builder"
```

---

### Task 6: Mock Data Generator

**Files:**
- Create: `src/database/mock_data.py`
- Create: `tests/test_mock_data.py`

- [ ] **Step 1: Write the failing test**

`tests/test_mock_data.py`:
```python
import sqlite3
from src.ontology.parser import OntologySchema, OntologyClass, OntologyProperty, OntologyRelationship
from src.database.schema import create_tables
from src.database.mock_data import generate_mock_data


def _make_schema():
    return OntologySchema(
        domain="Test Store",
        classes=[
            OntologyClass(
                name="Customer",
                properties=[
                    OntologyProperty(name="name", data_type="string"),
                    OntologyProperty(name="email", data_type="string"),
                    OntologyProperty(name="age", data_type="integer"),
                ],
            ),
            OntologyClass(
                name="Order",
                properties=[
                    OntologyProperty(name="orderId", data_type="string", is_identifier=True),
                    OntologyProperty(name="total", data_type="float"),
                    OntologyProperty(name="orderDate", data_type="date"),
                ],
            ),
            OntologyClass(
                name="Product",
                properties=[
                    OntologyProperty(name="name", data_type="string"),
                    OntologyProperty(name="price", data_type="float"),
                ],
            ),
        ],
        relationships=[
            OntologyRelationship(source="Customer", target="Order", name="places", cardinality="one-to-many"),
            OntologyRelationship(source="Order", target="Product", name="contains", cardinality="many-to-many"),
        ],
    )


def test_generates_rows_in_each_table(tmp_path):
    db_path = str(tmp_path / "test.db")
    schema = _make_schema()
    create_tables(db_path, schema)
    generate_mock_data(db_path, schema, rows_per_table=20)

    conn = sqlite3.connect(db_path)
    for table in ["customers", "orders", "products"]:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert count == 20, f"Expected 20 rows in {table}, got {count}"
    conn.close()


def test_generates_junction_table_rows(tmp_path):
    db_path = str(tmp_path / "test.db")
    schema = _make_schema()
    create_tables(db_path, schema)
    generate_mock_data(db_path, schema, rows_per_table=20)

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM orders_products").fetchone()[0]
    assert count > 0
    conn.close()


def test_foreign_keys_reference_existing_rows(tmp_path):
    db_path = str(tmp_path / "test.db")
    schema = _make_schema()
    create_tables(db_path, schema)
    generate_mock_data(db_path, schema, rows_per_table=20)

    conn = sqlite3.connect(db_path)
    # Every order.customer_id should exist in customers.id
    orphans = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE customer_id NOT IN (SELECT id FROM customers)"
    ).fetchone()[0]
    assert orphans == 0
    conn.close()


def test_data_types_are_correct(tmp_path):
    db_path = str(tmp_path / "test.db")
    schema = _make_schema()
    create_tables(db_path, schema)
    generate_mock_data(db_path, schema, rows_per_table=10)

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT name, email, age FROM customers LIMIT 1").fetchone()
    assert isinstance(row[0], str)
    assert isinstance(row[1], str)
    assert isinstance(row[2], int)

    row = conn.execute("SELECT total FROM orders LIMIT 1").fetchone()
    assert isinstance(row[0], (int, float))
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_mock_data.py -v
```

Expected: `ModuleNotFoundError: No module named 'src.database.mock_data'`

- [ ] **Step 3: Write implementation**

`src/database/mock_data.py`:
```python
import sqlite3
import random
from faker import Faker
from src.ontology.parser import OntologySchema
from src.ontology.context import _table_name
from src.database.schema import _fk_column_name

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
        if "price" in name_lower or "amount" in name_lower or "total" in name_lower or "cost" in name_lower or "spent" in name_lower or "subtotal" in name_lower:
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


def generate_mock_data(db_path: str, schema: OntologySchema, rows_per_table: int = 100):
    """Generate mock data for all tables based on ontology schema."""
    class_to_table = {c.name: _table_name(c.name) for c in schema.classes}

    # Build FK info: which tables need FK references and to which parent table
    fk_info: dict[str, list[tuple[str, str]]] = {}  # table -> [(fk_col, parent_table)]
    for rel in schema.relationships:
        source_table = class_to_table.get(rel.source, "")
        target_table = class_to_table.get(rel.target, "")
        if rel.cardinality == "one-to-many":
            fk_col = _fk_column_name(source_table)
            fk_info.setdefault(target_table, []).append((fk_col, source_table))
        elif rel.cardinality == "many-to-one":
            fk_col = _fk_column_name(target_table)
            fk_info.setdefault(source_table, []).append((fk_col, target_table))

    conn = sqlite3.connect(db_path)

    # Determine insertion order: tables without FK deps first
    tables_with_deps = set()
    for table, deps in fk_info.items():
        tables_with_deps.add(table)

    ordered_classes = []
    for cls in schema.classes:
        table = class_to_table[cls.name]
        if table not in tables_with_deps:
            ordered_classes.insert(0, cls)
        else:
            ordered_classes.append(cls)

    # Insert rows for entity tables
    table_ids: dict[str, list[int]] = {}
    for cls in ordered_classes:
        table = class_to_table[cls.name]

        # Build column list
        columns = []
        for fk_col, parent_table in fk_info.get(table, []):
            columns.append(fk_col)
        for prop in cls.properties:
            columns.append(prop.name)

        ids = []
        for _ in range(rows_per_table):
            values = []
            for fk_col, parent_table in fk_info.get(table, []):
                parent_ids = table_ids.get(parent_table, [1])
                values.append(random.choice(parent_ids))
            for prop in cls.properties:
                values.append(_get_faker_value(prop.name, prop.data_type))

            placeholders = ", ".join(["?"] * len(values))
            col_str = ", ".join(columns)
            cursor = conn.execute(
                f"INSERT INTO {table} ({col_str}) VALUES ({placeholders})",
                values,
            )
            ids.append(cursor.lastrowid)

        table_ids[table] = ids

    # Insert rows for junction tables (M:N)
    for rel in schema.relationships:
        if rel.cardinality == "many-to-many":
            source_table = class_to_table.get(rel.source, "")
            target_table = class_to_table.get(rel.target, "")
            junction = f"{source_table}_{target_table}"
            src_fk = _fk_column_name(source_table)
            tgt_fk = _fk_column_name(target_table)

            source_ids = table_ids.get(source_table, [])
            target_ids = table_ids.get(target_table, [])

            if source_ids and target_ids:
                pairs = set()
                num_links = min(rows_per_table * 2, len(source_ids) * len(target_ids))
                while len(pairs) < num_links:
                    pair = (random.choice(source_ids), random.choice(target_ids))
                    pairs.add(pair)

                for src_id, tgt_id in pairs:
                    conn.execute(
                        f"INSERT INTO {junction} ({src_fk}, {tgt_fk}) VALUES (?, ?)",
                        (src_id, tgt_id),
                    )

    conn.commit()
    conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_mock_data.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/database/mock_data.py tests/test_mock_data.py
git commit -m "feat: Faker-based mock data generator with FK linkage"
```

---

### Task 7: SQL Executor with Permission Layer

**Files:**
- Create: `src/database/executor.py`
- Create: `tests/test_executor.py`

- [ ] **Step 1: Write the failing test**

`tests/test_executor.py`:
```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_executor.py -v
```

Expected: `ModuleNotFoundError: No module named 'src.database.executor'`

- [ ] **Step 3: Write implementation**

`src/database/executor.py`:
```python
import sqlite3
import re
from dataclasses import dataclass


class PermissionDenied(Exception):
    pass


@dataclass
class SQLClassification:
    operation: str  # read, write, delete, admin
    approval_mode: str  # auto, confirm, deny


@dataclass
class SQLResult:
    operation: str
    rows: list[dict] | None = None
    affected_rows: int = 0
    needs_approval: bool = False
    error: str | None = None


_OPERATION_PATTERNS = [
    (r"^\s*(SELECT|WITH)\b", "read"),
    (r"^\s*(INSERT|UPDATE|REPLACE)\b", "write"),
    (r"^\s*DELETE\b", "delete"),
    (r"^\s*(DROP|CREATE|ALTER|TRUNCATE)\b", "admin"),
]


class SQLExecutor:
    def __init__(self, db_path: str, permissions: dict[str, str]):
        self._db_path = db_path
        self._permissions = permissions

    def classify(self, sql: str) -> SQLClassification:
        sql_upper = sql.strip()
        for pattern, operation in _OPERATION_PATTERNS:
            if re.match(pattern, sql_upper, re.IGNORECASE):
                approval_mode = self._permissions.get(operation, "deny")
                return SQLClassification(operation=operation, approval_mode=approval_mode)
        return SQLClassification(operation="admin", approval_mode="deny")

    def execute(self, sql: str, approved: bool = False) -> SQLResult:
        classification = self.classify(sql)

        if classification.approval_mode == "deny":
            raise PermissionDenied(
                f"Operation '{classification.operation}' is denied by permission policy"
            )

        if classification.approval_mode == "confirm" and not approved:
            return SQLResult(
                operation=classification.operation,
                needs_approval=True,
            )

        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(sql)
            if classification.operation == "read":
                rows = [dict(row) for row in cursor.fetchall()]
                return SQLResult(operation=classification.operation, rows=rows)
            else:
                conn.commit()
                return SQLResult(
                    operation=classification.operation,
                    affected_rows=cursor.rowcount,
                )
        finally:
            conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_executor.py -v
```

Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add src/database/executor.py tests/test_executor.py
git commit -m "feat: SQL executor with permission-gated execution"
```

---

### Task 8: LLM Abstraction Layer

**Files:**
- Create: `src/llm/__init__.py`
- Create: `src/llm/base.py`
- Create: `src/llm/vertex.py`

- [ ] **Step 1: Create LLM protocol and Vertex AI implementation**

`src/llm/__init__.py`:
```python
```

`src/llm/base.py`:
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

`src/llm/vertex.py`:
```python
from google.cloud import aiplatform
from vertexai.generative_models import GenerativeModel, Content, Part
from src.llm.base import LLMClient


class VertexGeminiClient:
    def __init__(self, project: str, location: str, model_name: str):
        aiplatform.init(project=project, location=location)
        self._model_name = model_name
        self._model = GenerativeModel(model_name)

    def chat(
        self,
        messages: list[dict],
        system_prompt: str | None = None,
        temperature: float = 0.0,
    ) -> str:
        contents = []
        for msg in messages:
            role = "user" if msg["role"] == "user" else "model"
            contents.append(Content(role=role, parts=[Part.from_text(msg["content"])]))

        generation_config = {"temperature": temperature, "max_output_tokens": 2048}

        response = self._model.generate_content(
            contents,
            generation_config=generation_config,
            system_instruction=system_prompt,
        )
        return response.text

    def get_model_name(self) -> str:
        return self._model_name
```

- [ ] **Step 2: Verify imports work**

```bash
python -c "from src.llm.base import LLMClient; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/llm/
git commit -m "feat: LLM abstraction layer with Vertex AI Gemini client"
```

---

### Task 9: Agent State and Nodes

**Files:**
- Create: `src/agent/__init__.py`
- Create: `src/agent/state.py`
- Create: `src/agent/nodes.py`
- Create: `tests/test_nodes.py`

- [ ] **Step 1: Create agent state definition**

`src/agent/__init__.py`:
```python
```

`src/agent/state.py`:
```python
from typing import TypedDict


class AgentState(TypedDict, total=False):
    messages: list[dict]
    ontology_context: str
    user_query: str
    intent: str              # READ / WRITE / UNCLEAR
    generated_sql: str
    permission_level: str    # auto / confirm / deny
    approved: bool | None
    query_result: list[dict] | None
    affected_rows: int
    response: str
    clarify_count: int       # track clarification retries (max 2)
    error: str | None
```

- [ ] **Step 2: Write the failing tests for nodes**

`tests/test_nodes.py`:
```python
import pytest
from src.agent.state import AgentState
from src.agent.nodes import (
    load_ontology_context,
    classify_intent,
    generate_sql,
    execute_sql_node,
    format_result,
)


class FakeLLM:
    """Fake LLM that returns canned responses based on call count."""
    def __init__(self, responses: list[str]):
        self._responses = responses
        self._call_index = 0

    def chat(self, messages, system_prompt=None, temperature=0.0):
        resp = self._responses[self._call_index]
        self._call_index += 1
        return resp

    def get_model_name(self):
        return "fake-model"


def test_load_ontology_context():
    state: AgentState = {"ontology_context": "", "messages": []}
    context_text = "Domain: Test\nTables: users"
    result = load_ontology_context(state, context_text)
    assert result["ontology_context"] == context_text


def test_classify_intent_read():
    llm = FakeLLM(["READ"])
    state: AgentState = {
        "user_query": "How many customers are there?",
        "ontology_context": "Domain: Test\nTables: customers",
        "messages": [],
        "clarify_count": 0,
    }
    result = classify_intent(state, llm)
    assert result["intent"] == "READ"


def test_classify_intent_write():
    llm = FakeLLM(["WRITE"])
    state: AgentState = {
        "user_query": "Update all orders to cancelled",
        "ontology_context": "Domain: Test\nTables: orders",
        "messages": [],
        "clarify_count": 0,
    }
    result = classify_intent(state, llm)
    assert result["intent"] == "WRITE"


def test_generate_sql():
    llm = FakeLLM(["SELECT COUNT(*) as total FROM customers"])
    state: AgentState = {
        "user_query": "How many customers?",
        "ontology_context": "Domain: Test\nTables: customers\n\nTable: customers\n  Columns: id (INTEGER PK), name (TEXT)",
        "intent": "READ",
        "messages": [],
    }
    result = generate_sql(state, llm)
    assert "SELECT" in result["generated_sql"]
    assert result["permission_level"] == "auto"


def test_generate_sql_write_gets_confirm():
    llm = FakeLLM(["UPDATE orders SET status = 'cancelled' WHERE status = 'overdue'"])
    state: AgentState = {
        "user_query": "Cancel overdue orders",
        "ontology_context": "Domain: Test\nTables: orders",
        "intent": "WRITE",
        "messages": [],
    }
    result = generate_sql(state, llm)
    assert result["permission_level"] == "confirm"


def test_format_result_read():
    llm = FakeLLM(["There are 42 customers in total."])
    state: AgentState = {
        "user_query": "How many customers?",
        "query_result": [{"total": 42}],
        "affected_rows": 0,
        "intent": "READ",
        "messages": [],
        "generated_sql": "SELECT COUNT(*) as total FROM customers",
    }
    result = format_result(state, llm)
    assert "42" in result["response"]
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_nodes.py -v
```

Expected: `ModuleNotFoundError: No module named 'src.agent'`

- [ ] **Step 4: Write implementation**

`src/agent/nodes.py`:
```python
import re
from src.agent.state import AgentState
from src.llm.base import LLMClient
from src.database.executor import SQLExecutor, SQLResult, PermissionDenied


def load_ontology_context(state: AgentState, context_text: str) -> dict:
    return {"ontology_context": context_text}


def classify_intent(state: AgentState, llm: LLMClient) -> dict:
    system = (
        "You are an intent classifier for a data agent. "
        "Given a user query and database schema, classify the intent as exactly one of: READ, WRITE, UNCLEAR.\n"
        "READ: queries that retrieve/analyze data (SELECT).\n"
        "WRITE: queries that modify data (INSERT, UPDATE, DELETE).\n"
        "UNCLEAR: query is ambiguous or unrelated to the database.\n"
        "Respond with ONLY the single word: READ, WRITE, or UNCLEAR."
    )
    messages = [
        {"role": "user", "content": f"Schema:\n{state['ontology_context']}\n\nUser query: {state['user_query']}"},
    ]
    response = llm.chat(messages, system_prompt=system, temperature=0.0)
    intent = response.strip().upper()
    if intent not in ("READ", "WRITE", "UNCLEAR"):
        intent = "UNCLEAR"
    return {"intent": intent}


def generate_sql(state: AgentState, llm: LLMClient) -> dict:
    system = (
        "You are a SQL generator for a SQLite database. "
        "Given the database schema and a user query, generate ONLY the SQL statement. "
        "Do not include any explanation, markdown, or code fences. "
        "Output ONLY the raw SQL statement."
    )
    messages = [
        {
            "role": "user",
            "content": (
                f"Database schema:\n{state['ontology_context']}\n\n"
                f"User query: {state['user_query']}\n"
                f"Intent: {state['intent']}"
            ),
        },
    ]
    response = llm.chat(messages, system_prompt=system, temperature=0.0)

    # Clean up: remove markdown code fences if present
    sql = response.strip()
    sql = re.sub(r"^```sql\s*", "", sql)
    sql = re.sub(r"^```\s*", "", sql)
    sql = re.sub(r"\s*```$", "", sql)
    sql = sql.strip()

    # Determine permission level
    sql_upper = sql.upper().lstrip()
    if sql_upper.startswith(("SELECT", "WITH")):
        permission_level = "auto"
    elif sql_upper.startswith(("DROP", "CREATE", "ALTER", "TRUNCATE")):
        permission_level = "deny"
    else:
        permission_level = "confirm"

    return {"generated_sql": sql, "permission_level": permission_level}


def execute_sql_node(state: AgentState, executor: SQLExecutor) -> dict:
    sql = state["generated_sql"]
    approved = state.get("approved", False)

    try:
        result = executor.execute(sql, approved=approved or state.get("permission_level") == "auto")
    except PermissionDenied as e:
        return {"error": str(e), "query_result": None, "affected_rows": 0}
    except Exception as e:
        return {"error": str(e), "query_result": None, "affected_rows": 0}

    if result.needs_approval:
        return {"approved": None}  # signal that approval is needed

    return {
        "query_result": result.rows,
        "affected_rows": result.affected_rows,
        "error": None,
    }


def format_result(state: AgentState, llm: LLMClient) -> dict:
    if state.get("error"):
        return {"response": f"Error: {state['error']}"}

    system = (
        "You are a helpful data assistant. Summarize the query result in a clear, "
        "concise natural language response. Be specific with numbers and names."
    )

    if state.get("query_result") is not None:
        result_str = str(state["query_result"][:20])  # limit for context
    else:
        result_str = f"Affected rows: {state.get('affected_rows', 0)}"

    messages = [
        {
            "role": "user",
            "content": (
                f"User asked: {state['user_query']}\n"
                f"SQL executed: {state.get('generated_sql', '')}\n"
                f"Result: {result_str}"
            ),
        },
    ]
    response = llm.chat(messages, system_prompt=system, temperature=0.0)
    return {"response": response.strip()}


def clarify_question(state: AgentState, llm: LLMClient) -> dict:
    system = (
        "You are a helpful data assistant. The user's query is unclear. "
        "Ask a brief clarifying question to understand what they want. "
        "Mention what data tables are available."
    )
    messages = [
        {
            "role": "user",
            "content": (
                f"Schema:\n{state['ontology_context']}\n\n"
                f"User query: {state['user_query']}"
            ),
        },
    ]
    response = llm.chat(messages, system_prompt=system, temperature=0.0)
    count = state.get("clarify_count", 0) + 1
    return {"response": response.strip(), "clarify_count": count}
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_nodes.py -v
```

Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add src/agent/ tests/test_nodes.py
git commit -m "feat: agent state definition and graph node implementations"
```

---

### Task 10: LangGraph State Graph

**Files:**
- Create: `src/agent/graph.py`

- [ ] **Step 1: Write the graph wiring**

`src/agent/graph.py`:
```python
from langgraph.graph import StateGraph, END
from src.agent.state import AgentState
from src.agent.nodes import (
    load_ontology_context,
    classify_intent,
    generate_sql,
    execute_sql_node,
    format_result,
    clarify_question,
)
from src.llm.base import LLMClient
from src.database.executor import SQLExecutor


def _route_after_intent(state: AgentState) -> str:
    intent = state.get("intent", "UNCLEAR")
    if intent == "READ":
        return "generate_sql"
    elif intent == "WRITE":
        return "generate_sql"
    else:
        if state.get("clarify_count", 0) >= 2:
            return "give_up"
        return "clarify"


def _route_after_execute(state: AgentState) -> str:
    if state.get("approved") is None and state.get("error") is None and state.get("query_result") is None:
        # Needs approval — handled externally by CLI
        return "needs_approval"
    return "format_result"


def build_graph(llm: LLMClient, executor: SQLExecutor, ontology_context: str) -> StateGraph:
    graph = StateGraph(AgentState)

    # Node wrappers that close over dependencies
    graph.add_node("load_context", lambda state: load_ontology_context(state, ontology_context))
    graph.add_node("classify_intent", lambda state: classify_intent(state, llm))
    graph.add_node("generate_sql", lambda state: generate_sql(state, llm))
    graph.add_node("execute_sql", lambda state: execute_sql_node(state, executor))
    graph.add_node("format_result", lambda state: format_result(state, llm))
    graph.add_node("clarify", lambda state: clarify_question(state, llm))
    graph.add_node("give_up", lambda state: {"response": "I'm unable to understand your request after multiple attempts. Please try rephrasing, or use .tables to see available data."})

    # Edges
    graph.set_entry_point("load_context")
    graph.add_edge("load_context", "classify_intent")
    graph.add_conditional_edges("classify_intent", _route_after_intent, {
        "generate_sql": "generate_sql",
        "clarify": "clarify",
        "give_up": "give_up",
    })
    graph.add_edge("generate_sql", "execute_sql")
    graph.add_conditional_edges("execute_sql", _route_after_execute, {
        "format_result": "format_result",
        "needs_approval": END,
    })
    graph.add_edge("format_result", END)
    graph.add_edge("clarify", END)
    graph.add_edge("give_up", END)

    return graph.compile()
```

- [ ] **Step 2: Verify graph compiles**

```bash
python -c "
from src.agent.graph import build_graph

class FakeLLM:
    def chat(self, messages, system_prompt=None, temperature=0.0): return 'READ'
    def get_model_name(self): return 'fake'

class FakeExecutor:
    def execute(self, sql, approved=False): pass
    def classify(self, sql): pass

g = build_graph(FakeLLM(), FakeExecutor(), 'test context')
print('Graph compiled OK:', type(g).__name__)
"
```

Expected: `Graph compiled OK: CompiledStateGraph`

- [ ] **Step 3: Commit**

```bash
git add src/agent/graph.py
git commit -m "feat: LangGraph state graph with conditional routing"
```

---

### Task 11: CLI Application

**Files:**
- Create: `src/cli/__init__.py`
- Create: `src/cli/app.py`

- [ ] **Step 1: Write the CLI application**

`src/cli/__init__.py`:
```python
```

`src/cli/app.py`:
```python
import sys
import os
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.syntax import Syntax
from rich.prompt import Prompt, Confirm

from src.config import load_config
from src.ontology.parser import parse_ontology
from src.ontology.context import generate_context, _table_name
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


def _handle_system_command(cmd: str, schema, class_to_table: dict, ontology_dir: str) -> bool:
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
        import sqlite3
        # Find db path from config
        config = load_config()
        db_dir = config["database"]["path"]
        for f in Path(db_dir).glob("*.db"):
            conn = sqlite3.connect(str(f))
            try:
                cursor = conn.execute(f"PRAGMA table_info({table_name})")
                cols = cursor.fetchall()
                if cols:
                    for col in cols:
                        pk = " PK" if col[5] else ""
                        console.print(f"  {col[1]} ({col[2]}{pk})")
                    return True
            finally:
                conn.close()
        console.print(f"[red]Table '{table_name}' not found.[/red]")
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
            if _handle_system_command(user_input, schema, class_to_table, ontology_dir):
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

                # Format the result by calling the node function directly
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
```

- [ ] **Step 2: Verify the module loads without errors**

```bash
python -c "from src.cli.app import _find_ontologies, _display_table; print('CLI module loads OK')"
```

Expected: `CLI module loads OK`

- [ ] **Step 3: Add __main__.py entry point**

Create `src/__main__.py`:
```python
from src.cli.app import main

main()
```

- [ ] **Step 4: Commit**

```bash
git add src/cli/ src/__main__.py
git commit -m "feat: CLI application with domain selection and conversation loop"
```

---

### Task 12: Integration Smoke Test

**Files:**
- Create: `tests/test_integration.py`

- [ ] **Step 1: Write end-to-end test (no real LLM)**

`tests/test_integration.py`:
```python
"""Integration test: ontology → DB → executor → agent nodes, with fake LLM."""
import sqlite3
from src.ontology.parser import parse_ontology
from src.ontology.context import generate_context
from src.database.schema import create_tables
from src.database.mock_data import generate_mock_data
from src.database.executor import SQLExecutor
from src.agent.nodes import classify_intent, generate_sql, execute_sql_node, format_result


class FakeLLM:
    def __init__(self, responses):
        self._responses = iter(responses)

    def chat(self, messages, system_prompt=None, temperature=0.0):
        return next(self._responses)

    def get_model_name(self):
        return "fake"


def test_full_read_pipeline(sample_rdf_path, tmp_path):
    """Test the full pipeline: parse RDF → create DB → mock data → query."""
    # 1. Parse ontology
    schema = parse_ontology(sample_rdf_path)
    assert schema.domain == "Test Store"
    assert len(schema.classes) == 3

    # 2. Create DB + mock data
    db_path = str(tmp_path / "test.db")
    create_tables(db_path, schema)
    generate_mock_data(db_path, schema, rows_per_table=20)

    # Verify data exists
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    assert count == 20
    conn.close()

    # 3. Generate context
    context = generate_context(schema)
    assert "customers" in context

    # 4. Run agent nodes with fake LLM
    executor = SQLExecutor(db_path, {"read": "auto", "write": "confirm", "delete": "confirm", "admin": "deny"})

    state = {
        "messages": [],
        "ontology_context": context,
        "user_query": "How many customers are there?",
        "clarify_count": 0,
    }

    # Classify intent
    llm = FakeLLM(["READ"])
    result = classify_intent(state, llm)
    assert result["intent"] == "READ"
    state.update(result)

    # Generate SQL
    llm = FakeLLM(["SELECT COUNT(*) as total FROM customers"])
    result = generate_sql(state, llm)
    assert "SELECT" in result["generated_sql"]
    state.update(result)

    # Execute SQL
    result = execute_sql_node(state, executor)
    assert result["query_result"][0]["total"] == 20
    state.update(result)

    # Format result
    llm = FakeLLM(["There are 20 customers in the database."])
    result = format_result(state, llm)
    assert "20" in result["response"]


def test_full_write_pipeline_with_approval(sample_rdf_path, tmp_path):
    """Test write pipeline requires approval."""
    schema = parse_ontology(sample_rdf_path)
    db_path = str(tmp_path / "test.db")
    create_tables(db_path, schema)
    generate_mock_data(db_path, schema, rows_per_table=10)

    context = generate_context(schema)
    executor = SQLExecutor(db_path, {"read": "auto", "write": "confirm", "delete": "confirm", "admin": "deny"})

    state = {
        "messages": [],
        "ontology_context": context,
        "user_query": "Set all order totals to 0",
        "clarify_count": 0,
    }

    # Classify as WRITE
    llm = FakeLLM(["WRITE"])
    result = classify_intent(state, llm)
    state.update(result)

    # Generate SQL
    llm = FakeLLM(["UPDATE orders SET total = 0"])
    result = generate_sql(state, llm)
    assert result["permission_level"] == "confirm"
    state.update(result)

    # Execute without approval — should signal needs_approval
    state["approved"] = False
    result = execute_sql_node(state, executor)
    assert result.get("approved") is None  # needs approval

    # Execute with approval
    state["approved"] = True
    state["permission_level"] = "confirm"
    result = execute_sql_node(state, executor)
    assert result["affected_rows"] == 10
```

- [ ] **Step 2: Run integration tests**

```bash
pytest tests/test_integration.py -v
```

Expected: 2 passed.

- [ ] **Step 3: Run all tests**

```bash
pytest tests/ -v
```

Expected: All tests pass (config: 3, parser: 11, context: 6, schema: 6, mock_data: 4, executor: 9, nodes: 6, integration: 2 = ~47 tests).

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration.py
git commit -m "feat: integration smoke tests for full read/write pipeline"
```

---

### Task 13: Manual End-to-End Test

- [ ] **Step 1: Verify the application starts**

```bash
GOOGLE_CLOUD_PROJECT=mydevproject20260304 \
GOOGLE_CLOUD_LOCATION=global \
GOOGLE_APPLICATION_CREDENTIALS=/home/openclaw/.gemini/mydevproject20260304-78c063107af2.json \
python -m src
```

Expected: Domain selection menu appears, database is created, "Ready" message shows.

- [ ] **Step 2: Test a READ query**

```
ecommerce> How many buyers are there?
```

Expected: Shows intent READ, SQL with `SELECT COUNT(*)`, table with result, natural language summary.

- [ ] **Step 3: Test a WRITE query**

```
ecommerce> Set all order statuses to shipped
```

Expected: Shows intent WRITE, SQL with `UPDATE`, asks for confirmation `[y/n]`.

- [ ] **Step 4: Test system commands**

```
ecommerce> .tables
ecommerce> .schema buyers
ecommerce> .ontology
ecommerce> .quit
```

Expected: Each command produces appropriate output.

- [ ] **Step 5: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix: adjustments from manual end-to-end testing"
```

Only commit if changes were made.
