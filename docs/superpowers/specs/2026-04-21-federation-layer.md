# Federation Layer — 跨源查询编排与虚拟视图 Spec

**日期:** 2026-04-21
**状态:** Draft
**依赖:** `2026-04-20-ontology-provider-physical-mapping.md`（`OntologyProvider` 抽象与物理映射）
**探索工具:** Gemini CLI（`gemini` 命令）

---

## 1. 背景与动机

本项目聚焦两个核心问题：

1. **如何让数据在一个组织内高效共享和互操作？**
2. **如何让传统的数据清洗和数据模型更灵活地适应 AI Agent 的需求？**

现有架构把 Ontology 作为语义契约已经解决了"Agent 可读"的一半问题，但**查询仍被单个 Executor 所约束**：

- 一条问题只能对一个后端产生一条 SQL（`generate_sql` → `execute_sql` → 单一 `BaseExecutor`）
- 跨源问题（"CRM 里的 VIP 客户在订单库里的最近订单"）只能靠预建宽表或手工 ETL
- `aip:physicalTable` / `aip:queryEngine` 已在本体里声明，但运行时没被用来做路由决策

**Federation Layer 的目标：** 让 Ontology 声明的物理分布在**运行时**生效，允许一条用户问题被自动拆分到多个 Executor 并 join 回一个结果集，从而：

- 不再需要为每个新问题预建宽表（取代一类传统 ETL）
- 不同团队的数据留在自己的存储里就能互操作（Q1 的答案）
- Agent 能自然地跨域组合数据（Q2 的答案）

---

## 2. 核心概念

### 2.1 三个关键抽象

| 抽象 | 定位 | 当前状态 |
|------|------|---------|
| `OntologyProvider` | 语义层：描述有哪些实体、它们的物理位置 | ✅ 已实现 |
| `BaseExecutor` | 存储层：对单一后端执行 SQL | ✅ 已实现 |
| **`QueryPlanner`**（新） | 联邦层：把逻辑查询拆成多个子查询、路由到 executor、合并结果 | ❌ 本 spec 要建 |

### 2.2 执行路径对比

**现在：**
```
User Query
  → LLM (generate_sql) → single SQL
  → BaseExecutor.execute(sql) → rows
```

**本 spec 完成后：**
```
User Query
  → LLM (generate_sql) → single SQL (对"虚拟统一 schema")
  → QueryPlanner.plan(sql)
      → 解析引用的 entity
      → 查 ontology 的 physical_mappings
      → 如果所有 entity 同源 → 单子查询下推
      → 如果跨源 → 拆分 + broadcast/hash join
  → [Executor₁, Executor₂, ...] 并行
  → Joiner (DuckDB in-process)
  → rows
```

---

## 3. MVP 范围

### 3.1 In-Scope（本 spec 要做）

| # | 功能 | 说明 |
|---|------|------|
| 1 | **查询路由** | 根据 entity 的 `aip:queryEngine` 注解选择对应 Executor |
| 2 | **多 Executor 注册** | `build_graph` 接收 `executors: dict[str, BaseExecutor]` 而非单个 executor |
| 3 | **同源检测 & 下推** | 全部 entity 同 engine → 单子查询直接下推（零开销路径）|
| 4 | **跨源 Join（MVP 策略）** | 选小表 broadcast，用 `IN` 列表 或 DuckDB in-memory join 合并 |
| 5 | **Virtual Entity 声明** | Ontology 里声明派生实体（基于已有 entity 加 filter / projection），runtime 展开 |
| 6 | **谓词 / 投影下推** | QueryPlanner 把 `WHERE` 和 `SELECT` 列尽量推到子查询里，避免拉全表 |

### 3.2 Out-of-Scope（本 spec 不做，列出以防范围漂移）

- ❌ 成本式优化器（CBO）/ 统计信息收集
- ❌ 分布式执行（本 spec 所有 join 都在 Agent 进程内用 DuckDB 做）
- ❌ 跨引擎的 JOIN 下推（如 StarRocks 直接 query Iceberg 外表——那是存储层职责）
- ❌ 血缘追踪与行级权限（留给后续 spec）
- ❌ 复杂 SQL 方言转换（仅处理 `SELECT` / `WHERE` / `JOIN` / 聚合函数基础子集）

---

## 4. RDF 注解扩展

### 4.1 Virtual Entity（新）

在 `.rdf` 里声明派生实体，Federation 层在 plan 时展开为基实体 + 额外约束：

```xml
<owl:Class rdf:about="#VIPCustomer">
  <rdfs:label>VIPCustomer</rdfs:label>
  <aip:isVirtual>true</aip:isVirtual>
  <aip:basedOn rdf:resource="#Customer"/>
  <aip:filter>lifetime_value &gt; 10000</aip:filter>
</owl:Class>
```

**不加字段、不加新表**；只在 planner 里展开为 `SELECT * FROM customers WHERE lifetime_value > 10000`。

### 4.2 `owl:sameAs`（标准词汇，无需新增注解）

用于声明两个源里的同一实体（Q1 跨团队共享的关键）：

```xml
<owl:Class rdf:about="#Customer">
  <owl:sameAs rdf:resource="http://crm.example.org/ontology#Client"/>
</owl:Class>
```

本 spec **读取并校验**但不强制做 ID 对齐（留给后续 spec）。

### 4.3 既有注解复用

- `aip:physicalTable` → planner 用于构造子查询的 FROM
- `aip:queryEngine` → planner 用于路由
- `aip:partitionKeys` → planner 用于谓词下推优化

---

## 5. 架构设计

### 5.1 新增模块

```
src/
└── federation/                   ← 新增模块
    ├── __init__.py
    ├── planner.py                ← QueryPlanner 主类
    ├── parser.py                 ← 轻量 SQL 解析（sqlglot）
    ├── rewriter.py               ← Virtual Entity 展开 + 谓词/投影下推
    ├── executor_registry.py      ← dict[engine_name, BaseExecutor]
    └── joiner.py                 ← DuckDB in-process join runner
```

### 5.2 QueryPlanner 接口

```python
class QueryPlanner:
    def __init__(
        self,
        ontology: OntologyProvider,
        executors: dict[str, BaseExecutor],
        default_engine: str = "sqlite",
    ): ...

    def plan(self, sql: str) -> QueryPlan:
        """Parse SQL → resolve entities → produce QueryPlan."""

    def execute(self, plan: QueryPlan, approved: bool = False) -> SQLResult:
        """Run sub-queries on their executors, join results in-process."""
```

```python
@dataclass
class QueryPlan:
    kind: Literal["single", "federated"]
    sub_queries: list[SubQuery]   # len=1 when single
    join_spec: JoinSpec | None    # None when single

@dataclass
class SubQuery:
    engine: str                   # "sqlite" | "starrocks" | ...
    sql: str                      # fully rewritten SQL with physical table names
    projected_columns: list[str]
```

### 5.3 与 LangGraph 的集成

**最小侵入方式：** 保留 `execute_sql_node` 的外观，内部把 `executor.execute(sql)` 换成 `planner.execute(planner.plan(sql))`。

```python
# src/agent/nodes/read_write.py — execute_sql_node 内部变化
def execute_sql_node(state: AgentState, planner: QueryPlanner) -> dict:
    plan = planner.plan(state["generated_sql"])
    result = planner.execute(plan, approved=state.get("approved") is True)
    ...
```

`build_graph` 签名变更：

```python
# 旧
def build_graph(llm, executor: BaseExecutor, ontology): ...

# 新
def build_graph(llm, executors: dict[str, BaseExecutor], ontology): ...
# 内部构造 QueryPlanner(ontology, executors)，替换原来的 executor 闭包
```

向后兼容：`cli/app.py` 和 `web/app.py` 原本只构造一个 `SQLiteExecutor`，改为 `{"sqlite": SQLiteExecutor(...)}`。

### 5.4 LLM 提示的变化

**LLM 仍然生成"单条 SQL over 统一虚拟 schema"**——它不需要知道联邦的存在。`schema_for_llm` 继续按 SQLite dialect（开发环境）或当前默认 engine 的 dialect 渲染实体名。

**一个关键权衡：** 虚拟 schema 用哪套表名？
- 选项 A：继续用简单名（`customers`, `orders`），planner 按 entity → physical_table 映射重写
- 选项 B：用 `<engine>.<schema>.<table>` 全名，planner 按 physical → engine 路由

**决策：选 A**，降低 LLM 生成难度，重写在 planner 侧完成。

---

## 6. Join 策略（MVP）

| 场景 | 策略 |
|------|------|
| 所有 entity 同 `queryEngine` | **零拆分**：原 SQL 透传到该 engine，表名重写为 `physical_table` |
| 跨 engine，一侧结果 ≤ 1000 行（小表） | **Broadcast**：小表全量拉回，作为 `IN (...)` 推到大表侧 |
| 跨 engine，两侧都 > 1000 行 | **DuckDB in-process**：两侧拉回 arrow/pandas，用 DuckDB 做 hash join |
| 跨 engine，两侧都 > 100万行 | **拒绝 + 友好错误**：`"Cross-source join too large (> 1M rows each side). Please add filter predicates."` |

阈值可调（`config.yaml` 新增 `federation.broadcast_threshold` / `federation.join_row_limit`）。

---

## 7. 实施阶段（渐进式，每阶段可独立 ship）

### Phase 1：QueryPlanner 骨架 + 同源路由

**交付：**
- `src/federation/` 模块骨架
- `QueryPlanner` 能解析 SQL（用 sqlglot），识别引用的表，通过 ontology 查出 engine
- 当全部表同源 → 单子查询透传，表名重写为物理表名
- `build_graph` 改为接收 `executors: dict`，`execute_sql_node` 走 planner
- `cli/app.py` / `web/app.py` 更新调用点
- 旧测试全绿（行为等价）

**验证：** 对 SQLite 的单源查询仍正常工作；多 Executor 注册可 import 但不实际切换。

---

### Phase 2：Virtual Entity 支持

**交付：**
- `aip:isVirtual` / `aip:basedOn` / `aip:filter` 在 `RDFOntologyProvider` 中解析
- `rewriter.py` 在 plan 阶段把 Virtual Entity 展开（`SELECT ... FROM VIPCustomer` → `SELECT ... FROM customers WHERE lifetime_value > 10000`）
- 每个 RDF 文件至少添加一个示范 Virtual Entity
- 单元测试覆盖展开逻辑

**验证：** LLM 生成对 `VIPCustomer` 的查询能被正确展开并返回真实结果。

---

### Phase 3：跨源 Join（DuckDB）

**交付：**
- `joiner.py` 用 DuckDB 注册 arrow/pandas 表做 in-process join
- Planner 识别跨源，选 broadcast or DuckDB join
- 添加 `DuckDBExecutor`（仅用于联邦 joiner，不对外作为后端）— 或直接用 `duckdb.connect(":memory:")` 临时 session
- 新增配置：`federation.broadcast_threshold`, `federation.join_row_limit`
- 集成测试：用两个独立 SQLite 库模拟"跨源"场景

**验证：** 两个 SQLite 库分别含 `customers` 和 `orders`，一条查询能正确 join。

---

### Phase 4：谓词 / 投影下推 + 边界控制

**交付：**
- `rewriter.py` 把 `WHERE` / `SELECT` 列推到子查询
- 超限拒绝：两侧都超 `join_row_limit` → 返回友好错误
- 可观测：Langfuse 里每个子查询作为独立 generation 嵌套在父 trace 下

**验证：** 通过 Langfuse trace 能看到子查询时序与大小；超限查询被拒绝。

---

## 8. 测试策略

### 8.1 单元测试

- `tests/federation/test_planner.py`：SQL 解析 → QueryPlan 构造（mock ontology）
- `tests/federation/test_rewriter.py`：Virtual Entity 展开、表名重写、下推
- `tests/federation/test_joiner.py`：DuckDB in-process join 正确性
- `tests/federation/test_executor_registry.py`：路由正确性

### 8.2 集成测试

- `tests/test_federation_integration.py`：
  - 启动两个临时 SQLite 文件（模拟跨源）
  - 在 ontology 里给不同 entity 配不同 `queryEngine`
  - 用真实 Planner 执行跨源查询，断言结果正确

### 8.3 Web / Langfuse 验证

- Playwright 跑一条跨源查询，断言响应正确且无 Traceback
- Langfuse 里能看到子查询的嵌套 generation

### 8.4 回归

- 旧的 `tests/test_agent_graph.py` / `test_rdf_provider.py` 等全部保持绿灯

---

## 9. 风险与取舍

| 风险 | 缓解 |
|------|------|
| SQL 解析不支持复杂查询 | 用成熟的 `sqlglot`，不自己造轮子 |
| DuckDB 作为新依赖 | 已是主流，仅作 in-process 使用，不新增服务 |
| LLM 可能生成带物理表名的 SQL | 在 system prompt 明确"仅使用实体名"；重写层同时支持两种 |
| Virtual Entity 语义歧义（filter 用 SQL 片段） | MVP 限制为 WHERE 子句字符串，不支持嵌套；后续可升级为结构化 |
| Join 超大数据 OOM | `join_row_limit` 硬阈值 + 明确错误信息 |
| 改动 `build_graph` 签名破坏下游调用 | Phase 1 同时改 `cli/app.py` / `web/app.py`，并保留 `build_graph(executor=...)` 单 executor 的便捷构造 |

---

## 10. 成功标准

1. 旧的单源查询行为完全等价（所有既有测试绿灯）
2. 一条自然语言查询跨两个 SQLite"源"能正确返回 join 结果
3. Ontology 里声明的 Virtual Entity 能被 LLM 正常引用，Planner 自动展开
4. Langfuse trace 能看到子查询嵌套结构
5. Federation 相关代码总行数 < 1500，不打破现有抽象

---

## 11. 不变量（Architectural Invariants）

- `OntologyProvider` / `BaseExecutor` 接口签名不变
- `AgentState` 的现有字段不变（可新增）
- LLM 生成的 SQL 仍是"对逻辑统一 schema"，联邦细节对 LLM 透明
- 所有 federation 相关的运行时决策只依赖 `OntologyContext` + 配置，不依赖运行时 introspection 源数据
