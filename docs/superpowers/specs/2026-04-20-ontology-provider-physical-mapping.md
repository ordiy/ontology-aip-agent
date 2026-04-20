# OntologyProvider 抽象层 + 物理存储映射 Spec

**日期:** 2026-04-20
**状态:** Draft
**依赖:** `2026-04-20-decide-operate-pattern-d.md`（aip: namespace 基础）
**探索工具:** Gemini CLI（`gemini` 命令）

---

## 1. 背景与动机

### 1.1 现有问题

当前 `build_graph()` 接收一个 `ontology_context: str`，这个字符串：

1. **只含语义名**（如 `Order`），不含物理表名（如 `iceberg_catalog.retail.orders`）
2. **无执行后端感知**：LLM 生成的 SQL 不知道目标引擎是 StarRocks、Trino 还是 BigQuery
3. **无接口约束**：ontology 解析逻辑直接散落在 `parser.py`，无法替换或 mock

### 1.2 目标场景

```
用户 Prompt → LangGraph DAG
                 ├─ ontology_context（含 iceberg_catalog.retail.orders）
                 └─ executor → StarRocks → 查询 Iceberg/Hive
```

- Ontology 语义层（RDF）定义实体和规则
- 每个实体通过 `aip:physicalTable` 映射到物理存储路径
- Agent 生成的 SQL 直接包含物理表名，StarRocks 可直接执行
- DAG 节点代码**零改动**即可切换存储后端

### 1.3 核心设计原则

> **LangGraph 节点只依赖接口（OntologyProvider + BaseExecutor），不依赖实现。**
> 物理映射在 RDF 注解中声明，由 Provider 在加载时解析并渲染给 LLM。

---

## 2. 新增 RDF 注解（aip: namespace 扩展）

### 2.1 三个新属性

在现有 `aip:` namespace 下新增：

| 属性 | 类型 | 含义 | 示例 |
|------|------|------|------|
| `aip:physicalTable` | string | 完整物理表路径（含 catalog/schema） | `iceberg_catalog.retail.orders` |
| `aip:queryEngine` | string | 推荐查询引擎标识 | `starrocks` / `trino` / `bigquery` |
| `aip:partitionKeys` | string | 逗号分隔的分区键，提示 LLM 加过滤条件 | `order_date,region` |

### 2.2 RDF 示例（retail.rdf / Order 类）

```xml
<!-- 现有 AnnotationProperty 声明块中追加 -->
<owl:AnnotationProperty rdf:about="http://aip.example.org/rules#physicalTable"/>
<owl:AnnotationProperty rdf:about="http://aip.example.org/rules#queryEngine"/>
<owl:AnnotationProperty rdf:about="http://aip.example.org/rules#partitionKeys"/>

<!-- Order 类新增注解 -->
<owl:Class rdf:about="http://example.org/ontology/retail-store/Order">
    <rdfs:label>Order</rdfs:label>
    <!-- 现有 aip: 业务规则注解保持不变 -->
    <aip:decisionRule>IF status='overdue' AND days_since_due > 30 THEN recommend_cancel</aip:decisionRule>
    <aip:operationSteps>1:verify_overdue_orders,2:notify_affected_customers,3:update_order_status_to_cancelled,4:restore_product_stock</aip:operationSteps>
    <aip:requiresApproval>user</aip:requiresApproval>
    <aip:rollbackable rdf:datatype="http://www.w3.org/2001/XMLSchema#boolean">true</aip:rollbackable>
    <aip:overridable rdf:datatype="http://www.w3.org/2001/XMLSchema#boolean">true</aip:overridable>
    <aip:severity>medium</aip:severity>
    <!-- 新增物理映射 -->
    <aip:physicalTable>iceberg_catalog.retail.orders</aip:physicalTable>
    <aip:queryEngine>starrocks</aip:queryEngine>
    <aip:partitionKeys>order_date</aip:partitionKeys>
</owl:Class>
```

### 2.3 各领域物理表映射参考

| 领域 | Ontology 类 | 物理表（示例） | 分区键 |
|------|------------|--------------|--------|
| retail | Order | `iceberg_catalog.retail.orders` | `order_date` |
| retail | Customer | `iceberg_catalog.retail.customers` | — |
| retail | Product | `iceberg_catalog.retail.products` | `category_id` |
| finance | Transaction | `iceberg_catalog.finance.transactions` | `tx_date,currency` |
| healthcare | Patient | `iceberg_catalog.healthcare.patients` | — |
| manufacturing | WorkOrder | `iceberg_catalog.mfg.work_orders` | `plant_id,scheduled_date` |
| ecommerce | CartItem | `iceberg_catalog.ecomm.cart_items` | `session_date` |
| education | Enrollment | `iceberg_catalog.edu.enrollments` | `semester` |

---

## 3. 新增接口与数据结构

### 3.1 `PhysicalMapping` 数据类

```python
# src/ontology/provider.py

@dataclass
class PhysicalMapping:
    physical_table: str        # 完整物理路径，如 iceberg_catalog.retail.orders
    query_engine: str = ""     # starrocks | trino | bigquery | sqlite
    partition_keys: list[str] = field(default_factory=list)
```

### 3.2 `OntologyContext` 数据类

```python
@dataclass
class OntologyContext:
    schema_for_llm: str                              # 给 LLM 的 schema 描述（含物理表名）
    rules: dict[str, EntityRule]                     # aip: 业务规则（Pattern D）
    physical_mappings: dict[str, PhysicalMapping]    # entity_name → 物理路径
```

### 3.3 `OntologyProvider` 抽象基类

```python
from abc import ABC, abstractmethod

class OntologyProvider(ABC):
    """
    解耦 LangGraph 节点与 ontology 加载实现。
    Node 只依赖此接口，不关心 RDF 文件、数据库元数据或 mock。
    """

    @abstractmethod
    def load(self) -> OntologyContext:
        """加载并解析 ontology，返回 OntologyContext。结果应被缓存。"""
        ...

    @property
    def context(self) -> OntologyContext:
        """带惰性缓存的访问入口。"""
        if not hasattr(self, "_cache"):
            self._cache = self.load()
        return self._cache
```

---

## 4. `RDFOntologyProvider` 实现

### 4.1 职责

- 接收一个或多个 `.rdf` 文件路径
- 使用 `rdflib` 解析，提取：
  - 现有 `OntologySchema`（classes, relationships, rules）via 现有 `parser.py`
  - 新增 `physical_mappings`（`aip:physicalTable` / `queryEngine` / `partitionKeys`）
- 调用 `_render_schema_for_llm()` 生成 LLM 可用的 schema 字符串

### 4.2 `_render_schema_for_llm` 输出格式

```
Domain: Retail

Table: iceberg_catalog.retail.orders  -- entity: Order
  Columns: id(integer) PK, customer_id(integer), status(string), total_amount(float), order_date(date)
  Partitioned by: order_date
  [Decision Rule]: IF status='overdue' AND days_since_due > 30 THEN recommend_cancel

Table: iceberg_catalog.retail.customers  -- entity: Customer
  Columns: id(integer) PK, name(string), email(string), phone(string)
```

**关键约束：**
- `Table:` 行始终使用物理表名（含 catalog 前缀）
- 若无 `aip:physicalTable`，回退到 `entity_name.lower() + "s"` 保持向后兼容
- `Partitioned by:` 行仅在有分区键时输出（提示 LLM 加过滤条件减少全表扫描）
- Decision Rule 仅在有 `aip:decisionRule` 时输出

### 4.3 文件位置

```
src/ontology/provider.py       # ABC + PhysicalMapping + OntologyContext
src/ontology/rdf_provider.py   # RDFOntologyProvider 实现
```

---

## 5. `build_graph` 接口变更

### 5.1 签名变更

```python
# 旧
def build_graph(llm: LLMClient, executor: BaseExecutor, ontology_context: str) -> CompiledGraph:

# 新
def build_graph(llm: LLMClient, executor: BaseExecutor, ontology: OntologyProvider) -> CompiledGraph:
```

### 5.2 内部变更

```python
def build_graph(llm, executor, ontology: OntologyProvider):
    ctx = ontology.context   # 惰性加载，仅解析一次

    graph.add_node("load_context", lambda state: {
        "ontology_context": ctx.schema_for_llm,
        "rdf_rules": ctx.rules,
    })
    # 其余节点 lambda 闭包不变
    # executor.dialect 继续用于 db_dialect 注入
```

**`state["ontology_context"]` 语义不变**，节点代码无需修改。

### 5.3 `cli/app.py` 组装示例

```python
from src.ontology.rdf_provider import RDFOntologyProvider

# 开发/测试（SQLite 本地）
ontology = RDFOntologyProvider(["ontologies/retail.rdf"])
executor = SQLiteExecutor(db_path, permissions=config.permissions)

# 生产（StarRocks → Iceberg）
ontology = RDFOntologyProvider(["ontologies/retail.rdf", "ontologies/finance.rdf"])
executor = StarRocksExecutor(host=..., catalog="iceberg_catalog", permissions=...)

graph = build_graph(llm=llm, executor=executor, ontology=ontology)
```

---

## 6. 测试要求

| 测试文件 | 覆盖内容 |
|---------|---------|
| `tests/test_rdf_provider.py` | `RDFOntologyProvider.load()` 返回正确 `physical_mappings` |
| `tests/test_rdf_provider.py` | `schema_for_llm` 包含物理表名而非语义名 |
| `tests/test_rdf_provider.py` | 无 `aip:physicalTable` 时回退到默认命名 |
| `tests/test_rdf_provider.py` | `context` 属性惰性缓存（load 只调用一次） |
| `tests/test_graph_provider.py` | `build_graph` 接受 `OntologyProvider` 实例 |
| `tests/test_graph_provider.py` | `MockOntologyProvider` 可替换 `RDFOntologyProvider` |

### MockOntologyProvider（测试用）

```python
class MockOntologyProvider(OntologyProvider):
    def __init__(self, schema_text: str = "Table: orders\n  Columns: id(integer) PK"):
        self._schema = schema_text

    def load(self) -> OntologyContext:
        return OntologyContext(
            schema_for_llm=self._schema,
            rules={},
            physical_mappings={},
        )
```

---

## 7. 需要修改的文件

| 文件 | 修改类型 | 内容摘要 |
|------|---------|---------|
| `ontologies/*.rdf`（6个） | 追加注解 | 新增 `aip:physicalTable` / `queryEngine` / `partitionKeys` 属性声明和各类注解 |
| `src/ontology/provider.py` | **新建** | `PhysicalMapping`, `OntologyContext`, `OntologyProvider` ABC |
| `src/ontology/rdf_provider.py` | **新建** | `RDFOntologyProvider` 完整实现 |
| `src/agent/graph.py` | 改签名 | `ontology_context: str` → `ontology: OntologyProvider`，内部用 `ontology.context` |
| `src/cli/app.py` | 改组装 | 用 `RDFOntologyProvider` 替换直接的字符串传入 |
| `tests/test_rdf_provider.py` | **新建** | Provider 单元测试（含 mock RDF fixture） |
| `tests/test_graph_provider.py` | **新建** | graph 集成测试，用 `MockOntologyProvider` |

---

## 8. 实现顺序

```
阶段 1：RDF 注解扩展
  → 6 个 .rdf 文件添加属性声明 + 各类物理映射注解

阶段 2：接口层
  → src/ontology/provider.py（PhysicalMapping, OntologyContext, OntologyProvider）

阶段 3：RDF Provider 实现
  → src/ontology/rdf_provider.py（RDFOntologyProvider）

阶段 4：接口替换
  → src/agent/graph.py 签名变更
  → src/cli/app.py 组装变更

阶段 5：测试
  → tests/test_rdf_provider.py
  → tests/test_graph_provider.py
```

---

## 9. 与现有架构兼容性

| 维度 | 现有行为 | 变更后 |
|------|---------|--------|
| 节点代码（nodes.py） | 读 `state["ontology_context"]` | **不变** |
| AgentState 字段 | `ontology_context: str` | **不变**（内容变为含物理名的字符串） |
| BaseExecutor 接口 | `execute(sql, approved)` | **不变** |
| `build_graph` 调用方 | 传 `ontology_context=str` | 改为传 `ontology=RDFOntologyProvider(...)` |
| 测试 mock | 直接传字符串 | 改为传 `MockOntologyProvider(schema_text=...)` |
