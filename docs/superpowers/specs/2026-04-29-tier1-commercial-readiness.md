# Tier 1 商业准入架构设计 — 多租户 / 评估 / 连接器 / 长期记忆

**日期:** 2026-04-29
**状态:** Draft
**依赖:**
- `2026-04-20-ontology-provider-physical-mapping.md`（OntologyProvider 抽象）
- `2026-04-20-langfuse-observability.md`（trace/span 基础设施）
- `2026-04-21-federation-layer.md`（QueryPlanner / 多 Executor 路由）

---

## 1. 背景与动机

到目前为止（2026-04-29），项目已经具备一个本体驱动的 agentic data agent 的功能骨架：意图分类、SQL 生成/校验、多源联邦查询、DECIDE/OPERATE 模式、Langfuse 观测。

但要把它从「跑得通的 MVP」升级为**商业级 agentic ontology agent**，仍有四类**入场券**级别（blockers）能力缺失。它们的共性是：客户在 PoC 之后不会因为「准不准」而拒绝合作，但**一定会**因为以下问题拒绝采购：

| 维度 | 现状 | 客户立刻会问 |
|------|------|--------------|
| 安全合规 | `permissions.read=auto` 全局粗粒度，无身份传递 | 「不同部门看到的数据怎么隔离？审计日志在哪？」 |
| 准确率证据 | 无 golden set，无回归基线 | 「你说 90% 准确率，怎么验证？换 prompt 会不会回退？」 |
| 连接器 | 只有 SQLite + StarRocks 占位 | 「我们用 Snowflake/BigQuery，能接吗？」 |
| 状态记忆 | 每轮 stateless，意图分类从零开始 | 「同一个用户问第二次会更聪明吗？术语别名能学吗？」 |

**Tier 1 的目标：把以上四个「立刻拒绝」转化为「可以谈」，使产品具备进入商业 PoC → 付费试点的资格。**

非目标（属于 Tier 2/3）：Plan-Critic 自验证、本体演进、流式响应、tool 扩展超越 SQL。

---

## 2. 设计原则与不变量

贯穿四个模块的统一原则，写在最前面以防各模块各自跑偏：

1. **零信任的依赖注入**：`tenant_id` / `principal` / `audit_logger` / `memory` 都是显式参数，永远**不**从全局/线程局部变量隐式读取。这条与 `LLMClient`/`BaseExecutor` 的注入模式一致。
2. **节点函数签名稳定性**：四个模块的引入**不得**改变 `def my_node(state, dep) -> dict` 的现有 LangGraph 节点签名。所有新依赖通过 `build_graph(...)` 闭包注入。
3. **可降级**：每个模块都必须有 no-op 实现（`InMemoryAuditLogger`、`NullPolicyEngine`、`NullMemoryStore`），单机/OSS 用户不付任何复杂度成本。
4. **本体是单一事实源**：RBAC 策略、连接器配置、记忆中的术语映射，都尽可能挂在 RDF 注解上，而不是另起一份 YAML/数据库 schema。
5. **可观测性优先**：每一类新能力都必须在 Langfuse trace 上留下可识别的 span（`auth.check`、`memory.recall`、`connector.<name>.execute`、`eval.run`）。
6. **配置外置 / 密钥与配置分离**：所有密钥、连接串、域名、租户标识必须可通过环境变量或 `.env` 注入；`config.yaml` 仅含结构与默认值，**不得**含任何密钥。详见 §9。

---

## 3. 模块一：多租户 + RBAC + 审计

### 3.1 现状与问题

`config.yaml` 当前的 `permissions` 块是**全局 + 操作级**的：
```yaml
permissions: { read: auto, write: confirm, delete: confirm, admin: deny }
```
这只回答了「写操作要不要确认」，没有回答：
- 谁在问？（无 principal）
- TA 能看哪些表/哪些行？（无 RLS）
- 哪些列必须脱敏？（无 column-level masking）
- 什么时间被谁问了什么？（无审计）

合规客户（金融/医疗/政企）任何一项缺失都会立即一票否决。

### 3.2 核心抽象

引入五个新概念，全部位于 `src/security/`：

| 抽象 | 文件 | 职责 |
|------|------|------|
| `Principal` | `principal.py` | 表示「谁在问」：`tenant_id`、`user_id`、`roles`、`attrs`（claims） |
| `PrincipalProvider` | `principal.py` ABC | 各入口（CLI/Web/API）构造 Principal 的统一抽象 |
| `PolicyEngine` | `policy.py` ABC | 决策器：给定 `(principal, sql, referenced_entities)` → `AuthDecision` |
| `AuditLogger` | `audit.py` ABC | 落地 `AuditEvent`（who/when/what/result/sql/row_count） |
| `SecurityContext` | `context.py` | 把上面四者打包成节点函数能接收的依赖 |

**MVP `PrincipalProvider` 实现**：
- `EnvPrincipalProvider`（CLI 默认）：从 `DEFAULT_TENANT_ID` / `USER` 环境变量读
- `StreamlitSessionPrincipalProvider`（Web）：从 `st.session_state` 读，缺省回退到 env
- `JWTPrincipalProvider`（占位，Phase A 不做）：未来 REST API 用

**默认 no-op**：`build_graph(policy=None, audit=None)` 在缺省时使用 `NullPolicyEngine`（恒 allow）+ `NullAuditLogger`（丢弃事件）。**这条是死规矩**——否则现有 180 个不构造 Principal 的测试全部要改，会无意义地放大变更面。

### 3.3 Principal 数据模型

```python
@dataclass(frozen=True)
class Principal:
    tenant_id: str              # 多租户硬隔离的最小单位
    user_id: str
    roles: frozenset[str]       # e.g. {"analyst", "finance"}
    attrs: dict[str, str]       # 自定义 claim（region=APAC、cost_center=...）
    session_id: str             # 关联 memory + audit
```

`Principal` 在请求入口（CLI/Web/API）构造一次，全程只读传递。**禁止节点函数自行修改或重新构造 Principal**。

### 3.4 RDF 注解扩展（策略即本体）

在本体层声明默认策略，避免再起一份独立配置：

```turtle
ex:Order a owl:Class ;
    aip:physicalTable "orders" ;
    aip:queryEngine "starrocks" ;
    aip:requiresRole "analyst" ;                    # 看这张表至少要 analyst
    aip:rowFilter "tenant_id = $principal.tenant_id" ;  # 强制行过滤
    aip:maskColumns "ssn,email" .                   # 列脱敏

ex:Order/customer_email a owl:DatatypeProperty ;
    aip:maskAs "hash" .                             # 列级脱敏算法：hash/redact/null
```

`OntologyProvider.context` 增加：
```python
@dataclass
class SecurityPolicy:
    required_roles: frozenset[str]
    row_filter_template: str | None    # 含 $principal.* 占位
    masked_columns: dict[str, str]     # column -> mask method
```
`PhysicalMapping` 增字段 `policy: SecurityPolicy | None`。

**`row_filter_template` 安全规则**（必须严格遵守，防 SQL 注入）：

1. **白名单的占位语法**：仅以下形式可被解析；其余视为字面字符串：
   - `$principal.tenant_id`
   - `$principal.user_id`
   - `$principal.attrs.<key>`（仅 `[A-Za-z_][A-Za-z0-9_]*` 字符）
2. **插值方式**：每个占位的值**必须**经 `sqlglot.exp.Literal.string(value)` 序列化为安全字面量后再注入 AST，**禁止字符串拼接**生成最终 SQL
3. **失败语义**：占位变量在 Principal 中不存在 → `ConfigError`（启动时）或 `AuthDecision(allowed=False, reason=...)`（运行时）
4. **类型限制**：仅支持字符串/数字 attr；嵌套 dict/list 不能用作行过滤值（避免 JSON 注入）

### 3.5 PolicyEngine 接口

```python
class PolicyEngine(ABC):
    @abstractmethod
    def authorize(
        self,
        principal: Principal,
        sql: str,
        referenced_entities: list[str],   # 由调用方解析（不要求 PolicyEngine 理解 QueryPlan）
    ) -> AuthDecision: ...

class AuthOutcome(Enum):
    ALLOW = "allow"
    DENY = "deny"
    NEEDS_USER_APPROVAL = "needs_user_approval"   # 与现有 write confirm 流复用

@dataclass
class AuthDecision:
    outcome: AuthOutcome
    reason: str
    rewritten_sql: str | None        # 注入了 row filter / column mask 后的 SQL
    masked_columns: dict[str, str]   # 给执行结果做后处理
```

**与 QueryPlanner 的执行顺序**：`authorize` 节点在 `generate_sql` 之后、`QueryPlanner.plan` 之前，对**未拆分的 unified SQL** 做 sqlglot 改写。改写后的 SQL 再交给 planner 拆 SubQuery —— 每个 SubQuery 自动继承 row filter / mask，**联邦查询天然受 RBAC 保护**，不需要让 PolicyEngine 理解 QueryPlan 结构。

**SQL 改写策略（必须遵守）**：
- 使用 sqlglot 解析 SQL → AST
- 对 AST 中**每一个** `Table` 节点（包括 UNION / JOIN / 子查询里的）查询其对应 entity 的 `SecurityPolicy`
- 若该 entity 有 `row_filter_template`：**包装该表**为 `(SELECT * FROM <table> WHERE <filter>) AS <alias>`，**不能**只在最外层 `WHERE` 加条件（恶意 UNION 会绕过）
- 占位值通过 `sqlglot.exp.Literal.string(...)` / `Literal.number(...)` 注入，禁止字符串拼接

**列脱敏策略（MVP）**：
- **执行后 Python 后处理**（dialect-agnostic，简单）：`execute_sql_node` 在返回 rows 前对 `masked_columns` 列做 hash/redact/null 变换
- 不在 SQL 改写阶段做脱敏（依赖方言函数，留给 Tier 2）
- 审计 SQL 与原始 SQL 都不含敏感数据（仅元数据），脱敏只影响最终展示给用户的行

MVP 提供两种实现：
- `OntologyPolicyEngine`：从 RDF 注解读取，足够覆盖 80% 场景，OSS 用户开箱即用
- `OPAClient`（Open Policy Agent，可选）：复杂客户走 Rego DSL，未来插入
- `NullPolicyEngine`：所有请求 `outcome=ALLOW, rewritten_sql=None`；测试与 OSS 默认

### 3.6 AuditLogger 接口

```python
@dataclass
class AuditEvent:
    timestamp: datetime
    principal: Principal
    intent: str                     # READ/WRITE/ANALYZE/DECIDE/OPERATE
    sql: str | None
    plan_summary: dict | None       # 子查询数、引擎、estimated rows
    decision: AuthDecision
    result_rows: int | None
    error: str | None
    trace_id: str | None            # Langfuse trace 关联

class AuditLogger(ABC):
    @abstractmethod
    def emit(self, event: AuditEvent) -> None: ...
```

实现：
- `JsonlAuditLogger`：本地文件（默认）
- `PostgresAuditLogger`：生产部署
- `NullAuditLogger`：测试用

**审计记录是写后即不可改的事实**——不允许节点函数修改已 emit 的事件，也不能为了「整洁」聚合多条事件。

**Audit 失败语义（fail-closed）**：
- 生产部署：`AuditLogger.emit` 抛异常 = **拒绝该次查询**（fail-closed），返回 `AuthDecision(outcome=DENY, reason="audit_unavailable")`
- MVP 默认：`JsonlAuditLogger` 写文件失败时 warn-and-continue（fail-open），但日志中显著告警
- 生产部署的 fail-closed 由 config 开关控制：`audit.fail_mode: closed | open`，默认 `closed` 当 `audit.backend != jsonl`

**Audit 内容边界**：仅记录元数据（who/when/what/SQL/row_count），**不记录 row data**——脱敏后的 row 也不行。这是合规线，不可让步。

### 3.7 与 LangGraph 的集成

新增节点 `authorize`，插在 `generate_sql` 与 `execute_sql` 之间。authorize 节点输出三态路由：

```
classify_intent → generate_sql → authorize ──allow──→ execute_sql → format_result
                                     │
                                     ├──deny──────→ audit.emit(deny)  → END
                                     │
                                     └──needs_user_approval──→ END(等用户外部确认)
```

`authorize` 节点行为：
1. 解析 `state["sql"]` 出 `referenced_entities`（用 sqlglot；这里**不**调 planner，避免循环依赖）
2. 调 `policy.authorize(principal, sql, referenced_entities)`
3. `outcome=ALLOW` → 把 `decision.rewritten_sql` 写回 `state["sql"]`、`state["masked_columns"] = decision.masked_columns`，继续
4. `outcome=DENY` → 写 audit、设置 `state["error"] = decision.reason`，路由到 END
5. `outcome=NEEDS_USER_APPROVAL` → **复用既有 write confirm 路径**（CLI 弹确认 / Web 弹按钮），与现有 `_route_after_execute` 中的 `needs_approval` 分支汇合

`execute_sql` 节点：
- 入口接收 `state["principal"]`、`state["masked_columns"]`
- 出口**无论成功失败**都调 `audit.emit(...)`
- 返回 rows 前应用 `masked_columns` 后处理（hash/redact/null）

`AgentState` 新增字段（均 `total=False`）：
```python
principal: Principal | None        # 由 PrincipalProvider 在入口注入
auth_decision: AuthDecision | None # 由 authorize 节点写入，audit/format 节点读取
masked_columns: dict[str, str]     # 由 authorize 节点写入，execute 节点消费
```

### 3.8 测试策略

- **单元**（`OntologyPolicyEngine`）：无角色 / 角色不够 / 角色够 / 行过滤注入正确性 / 列脱敏后处理 / 跨租户访问拒绝 / 三态 outcome 路由 / null engine 默认 allow
- **集成**：完整 `principal=tenantA + 查 tenantB 数据` → 期待 `AuthDecision.outcome=DENY`，audit 留痕
- **安全回归**（关键）：
  - `SELECT * FROM orders UNION SELECT * FROM other_tenant.orders` → rewrite 必须**两个表**都注入 row filter
  - `SELECT * FROM (SELECT * FROM orders) AS sub` → 子查询里的 `orders` 也要注入
  - `SELECT * FROM orders WHERE id IN (SELECT id FROM other_tenant.users)` → 内层 `users` 也要注入
  - `attrs.region = 'APAC'); DROP TABLE orders;--` → `Literal.string` 转义后**不**被解释为 SQL 控制流
- **180 baseline 不变**：所有现有测试在 `policy=None / audit=None` 默认下保持 180 绿

---

## 4. 模块二：评估体系

### 4.1 现状与问题

当前测试只覆盖**代码正确性**（pytest 164 用例），不覆盖**Agent 行为正确性**：
- 改 prompt 之后意图分类是否回退？无人知道
- 换 LLM 提供商（Vertex → OpenRouter）是否准确率掉了 5%？无数据
- 客户问「为什么这个查询错了」时，没有可复现的失败 case 库

商业客户会要求「准确率报告」「回归基线」「改 prompt 不破坏既有行为」的证据。

### 4.2 核心抽象

`src/evaluation/` 新模块：

| 抽象 | 职责 |
|------|------|
| `EvalCase` | 一条 golden case：自然语言问题 + 期望意图 + 期望 SQL（或结果断言）+ 标签 |
| `EvalDataset` | 一组 EvalCase，按域（ecommerce/finance/healthcare）和等级（smoke/full）分层 |
| `EvalRunner` | 跑全部 case，输出每条 pass/fail + 聚合指标 |
| `Judge` | 判定器：精确 SQL 匹配 / 结果集等价 / LLM-as-judge / 子串包含 |
| `EvalReport` | 结构化报告：accuracy by intent、by domain、回归 diff、失败 case 详情 |

### 4.3 EvalCase 数据模型

```python
@dataclass
class EvalCase:
    id: str                                # "ecommerce-001"
    question: str
    domain: str                            # 对应 ontologies/<domain>.rdf
    expected_intent: str                   # READ/WRITE/...
    expected: ExpectedAnswer               # SQL / 行集 / 子串 / LLM-judge
    tags: frozenset[str]                   # smoke, regression, security, federation, ...
    skip_reason: str | None = None
```

存储为 YAML（人可读、git 友好）：`tests/eval/datasets/<domain>.yaml`。

### 4.4 Judge 策略（按成本递增）

| Judge | 适用 | 成本 |
|-------|------|------|
| `IntentJudge` | 只校验意图分类 | 0 LLM call |
| `SQLEquivalenceJudge` | 用 sqlglot canonicalize 后比较 AST | 0 LLM call |
| `ResultSetJudge` | 真实跑 SQL，对比行集（顺序无关、列名归一化） | 1 SQL exec |
| `LLMJudge` | 复杂 ANALYZE/DECIDE 的自然语言回答 | 1 LLM call |

**默认走前三种**，LLMJudge 仅用于自然语言 ANALYZE 输出。

### 4.5 运行模式

```bash
# Smoke：~30 case，CI 每次跑，<60s
python -m src.evaluation.run --suite smoke

# Full：~300 case，nightly 跑，5–10 分钟
python -m src.evaluation.run --suite full

# Diff：比较两次报告（PR vs main baseline）
python -m src.evaluation.run --diff baseline.json head.json
```

输出 `eval-report.json` + Markdown 摘要；CI 在 PR 上自动评论 diff。

### 4.6 与 Langfuse 集成

每个 EvalCase 在 Langfuse 中开一个 trace，tag=`eval`、`suite=smoke`、`case_id=...`。这样：
- 失败 case 直接点链接看完整 trace
- 不同 prompt 版本的 trace 可侧向对比

### 4.7 关键约束

- **Eval 数据集必须 git tracked**，绝不放 `.gitignore`
- **判定逻辑只用决定性算法**（除 LLMJudge 外）；不允许「随机抽样判定」
- **失败 case 必须可独立复现**：`python -m src.evaluation.run --case ecommerce-001 --verbose` 必须打印完整输入/中间状态/输出

### 4.8 与现有 pytest 的边界

- `pytest`：代码单元/集成正确性（FakeLLM、不打真实 LLM）
- `evaluation`：行为/准确率（**真打 LLM**，要求 LLM_API_KEY）

两者不交叉，避免「跑 pytest 居然在花 token」这种隐患。

---

## 5. 模块三：连接器生态

### 5.1 现状与问题

`BaseExecutor` 抽象设计是干净的，但实际只有 `SQLiteExecutor` 完整实现。客户问「能接 Snowflake 吗」时回答「我们设计上支持」是不够的，需要：
- 至少 3 个生产级实现（一个云数仓 + 一个分布式 OLAP + 一个湖格式）
- 统一的认证/连接池/超时/重试策略
- 方言差异（CTE、窗口、JSON 函数）的优雅降级

### 5.2 优先级矩阵

| 连接器 | 商业价值 | 实现难度 | 优先级 |
|--------|---------|---------|--------|
| **StarRocks** | 高（项目已有占位） | 低（MySQL 协议） | P0 |
| **Snowflake** | 极高（北美标配） | 中 | P0 |
| **BigQuery** | 高（GCP 客户） | 中 | P1 |
| **Iceberg + Trino** | 高（湖仓） | 高 | P1 |
| **PostgreSQL** | 中（既有数仓常见） | 低 | P0 |
| **DuckDB** | 中（已被 Joiner 用） | 0（暴露而已） | P0 |
| **ClickHouse** | 高（区块链 / 可观测性 / 实时分析常用；CryptoHouse 等公开端点可作 PoC） | 低（HTTP + clickhouse-connect） | P0 |

P0 第一批：StarRocks、Snowflake、PostgreSQL、DuckDB、**ClickHouse**（5 个 first-class executor）。ClickHouse 优先做的额外动机：CryptoHouse 公开端点免费、零鉴权门槛，是验证「外部数据源 + Engine Alias + 列级映射」全链路的最佳 PoC（详见附录 A）。

### 5.3 BaseExecutor 增量需求

现有 `BaseExecutor` 只要求 `execute(sql, approved)` 和 `dialect`。生产级需要补齐：

```python
class BaseExecutor(ABC):
    @property
    @abstractmethod
    def dialect(self) -> str: ...

    @abstractmethod
    def execute(self, sql: str, approved: bool = False,
                principal: Principal | None = None) -> SQLResult: ...

    # 新增 ↓
    @abstractmethod
    def schema_snapshot(self) -> SchemaSnapshot: ...   # 给 schema drift 检测和本体生成用

    @property
    def capabilities(self) -> ExecutorCapabilities: ...   # 默认 conservative
```

```python
@dataclass(frozen=True)
class ExecutorCapabilities:
    supports_cte: bool = True
    supports_window: bool = True
    supports_json_path: bool = False
    max_in_list_size: int = 1000
    transactional_writes: bool = False
```

`QueryPlanner` 在生成 SubQuery 时读 capabilities 做兼容性裁剪——比如目标 executor 不支持窗口函数，则 fallback 到 DuckDB join 后做。

### 5.4 连接 / 认证 / 超时

每个 connector 在 `config.yaml` 下有独立 block：

```yaml
connectors:
  snowflake_prod:
    type: snowflake
    account: xxx.us-east-1
    auth: { kind: keypair, key_path: ${SNOWFLAKE_KEY_PATH} }
    warehouse: AGENT_WH
    role: ANALYST_RO
    pool: { min: 2, max: 10, idle_timeout: 300 }
    query_timeout: 60
  starrocks_prod:
    type: starrocks
    host: ...
    auth: { kind: password, user: agent, password: ${SR_PASSWORD} }
```

**强约束**：所有连接器**只读默认**，写权限必须在 `auth.role` 显式声明，且与 `permissions` 块二次确认。

### 5.5 工厂与注册表

```python
# src/database/registry.py
def build_executor(cfg: dict) -> BaseExecutor:
    kind = cfg["type"]
    return {
        "sqlite": SQLiteExecutor,
        "duckdb": DuckDBExecutor,
        "postgres": PostgresExecutor,
        "starrocks": StarRocksExecutor,
        "snowflake": SnowflakeExecutor,
        "bigquery": BigQueryExecutor,
        "clickhouse": ClickHouseExecutor,
    }[kind].from_config(cfg)
```

Federation 的 `ExecutorRegistry` 现已存在，本节只是把 `build_executor` 接进去。

### 5.6 测试策略

**关键问题**：CI 跑不起真实云数仓。解决方案分层：

| 测试层 | 跑法 | 频率 |
|--------|------|------|
| Unit | mock connection / 验证 SQL 文本生成 | 每次 PR |
| Contract | testcontainers 起 Postgres/StarRocks | 每次 PR |
| Live | 真实 Snowflake/BigQuery 测试账号 | nightly |

每个新 connector 必须三层都有覆盖。

### 5.7 Engine Alias 层（本体到 Connector 的解耦）

**问题**：现有 `aip:queryEngine` 直接写引擎类型字符串（`starrocks` / `clickhouse`）。这导致两个问题：
- 同类型多实例无法区分（dev ClickHouse vs prod CryptoHouse vs 内部 ClickHouse）
- 本体不可移植（换部署环境就要改本体文件）

**方案**：引入 `engines` 配置块，作为本体 → connector 的间接层：

```yaml
connectors:
  clickhouse_crypto:        # connector 实例（含密钥与 host）
    type: clickhouse
    host: ${CRYPTOHOUSE_HOST}
    ...
  clickhouse_internal:
    type: clickhouse
    host: ${INTERNAL_CH_HOST}
    ...
  snowflake_prod:
    type: snowflake
    ...

engines:                    # 本体引用的 alias → connector
  blockchain: clickhouse_crypto
  observability: clickhouse_internal
  warehouse: snowflake_prod
```

本体里**只引用 alias**：

```xml
<aip:queryEngine>blockchain</aip:queryEngine>
```

效果：
- 同一份本体，`config.yaml` 改两行就从 dev 切到 prod
- `ExecutorRegistry` 在初始化时按 alias 索引，路由不变
- alias 名字承载语义意图（`blockchain` 比 `clickhouse_crypto` 更可读）

**向后兼容**：alias 缺失时回退为「按 connector 名匹配」，已有本体不破。

### 5.8 列级映射（`aip:column` 注解）

**问题**：物理表的列名/单位与本体语义层永远不一致——`output_value`(satoshi) ≠ 用户问的「金额」(BTC)。当前架构把这件事丢给 LLM 自己拼 SQL，错误率高且不可控。

**新增三类列级注解**（声明在 `owl:Class` 内）：

```xml
<aip:column ont:logical="amount"      ont:physical="output_value"
            ont:transform="value / 1e8" ont:unit="BTC"/>
<aip:column ont:logical="txHash"      ont:physical="hash"/>
<aip:column ont:logical="blockHeight" ont:physical="block_number"/>
<aip:column ont:logical="timestamp"   ont:physical="block_timestamp"/>
```

| 字段 | 必需 | 含义 |
|------|------|------|
| `ont:logical` | ✅ | 本体（用户/LLM 看到的）列名 |
| `ont:physical` | ✅ | 物理表实际列名 |
| `ont:transform` | 可选 | 投影时的表达式（`value / 1e8`）；`$col` 引用物理列 |
| `ont:unit` | 可选 | 结果列的单位标注（用于 format 与防止 LLM 单位错误） |

**作用点（QueryPlanner 中）**：
1. **LLM 提示阶段**：`schema_for_llm` 暴露 logical 列名 + unit 注释，LLM 不见 physical 列
2. **SQL 改写阶段**：QueryPlanner 把生成的 SQL 中 logical 列替换为 `physical AS logical` 或 `transform AS logical`
3. **结果格式化阶段**：format_result 节点能把 unit 拼进自然语言回答（「412 BTC」而非「412」）

**安全**：`transform` 表达式必须经 sqlglot 解析校验，禁止包含子查询、函数白名单受控（`/`、`*`、`+`、`-`、`CAST`、`COALESCE` 等），防止注入。

### 5.9 ClickHouseExecutor 实现要点

```python
# src/database/connectors/clickhouse_executor.py
import clickhouse_connect

class ClickHouseExecutor(BaseExecutor):
    DIALECT = "clickhouse"

    @property
    def dialect(self) -> str: return self.DIALECT

    @property
    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            supports_cte=True,
            supports_window=True,
            supports_json_path=True,
            max_in_list_size=10_000,
            transactional_writes=False,    # ClickHouse 无事务
        )

    @classmethod
    def from_config(cls, cfg: dict) -> "ClickHouseExecutor":
        client = clickhouse_connect.get_client(
            host=cfg["host"],
            username=cfg.get("user", "default"),
            password=cfg.get("password", ""),
            database=cfg.get("database", "default"),
            connect_timeout=cfg.get("query_timeout", 60),
        )
        return cls(client, cfg)

    def execute(self, sql: str, approved=False, principal=None) -> SQLResult:
        level = detect_permission_level(sql)
        if level != "auto" and not approved:
            return SQLResult(operation="write",
                             error="ClickHouse write requires approval")
        try:
            r = self._client.query(sql)
            rows = [dict(zip(r.column_names, row)) for row in r.result_rows]
            return SQLResult(operation="read", rows=rows)
        except Exception as e:
            return SQLResult(operation="read", error=str(e))

    def schema_snapshot(self) -> SchemaSnapshot:
        # 反查 system.tables / system.columns
        ...
```

依赖：`clickhouse-connect>=0.7`（HTTP 协议，无原生客户端编译依赖，CI 友好）。

---

## 6. 模块四：长期记忆

### 6.1 现状与问题

每次 `classify_intent` 都是冷启动：
- 用户上轮叫「客户」这次叫「买家」，要重新猜
- 同一个 user 上周问过「上月销售」，本周再问得不到任何加速
- DECIDE 给出的阈值，下次类似问题不复用

商业客户期望 agent 「越用越聪明」，无 memory 等于无 agentic claim。

### 6.2 三层记忆模型

借鉴常见 agentic memory 分层：

| 层 | 范围 | 内容 | 存储 | TTL |
|----|------|------|------|-----|
| **Working Memory** | 单次 session 内（多轮对话） | 最近问答、当前关注的 entity、未确认操作 | Redis / 进程内 | 1 小时 |
| **Episodic Memory** | 跨 session，单 user | 历史 query、用户偏好、术语别名 | Postgres | 永久（可清） |
| **Semantic Memory** | 跨 user，单 tenant | 共识词汇、常见意图模板、本体扩展提案 | Postgres + 向量索引 | 永久 |

### 6.3 抽象接口

```python
class MemoryStore(ABC):
    @abstractmethod
    def recall(self, principal: Principal, query: str,
               layers: frozenset[str] = frozenset({"working", "episodic"}),
               top_k: int = 5) -> list[MemoryRecord]: ...

    @abstractmethod
    def remember(self, principal: Principal, record: MemoryRecord) -> None: ...

    @abstractmethod
    def forget(self, principal: Principal,
               filter: MemoryFilter) -> int: ...   # 返回删除数
```

```python
@dataclass
class MemoryRecord:
    layer: str                          # working / episodic / semantic
    kind: str                           # qa, alias, preference, decision, intent_template
    content: str                        # 自然语言或 JSON 序列化
    vector: list[float] | None          # 可为空（小型部署用 BM25）
    metadata: dict                      # tenant_id, session_id, created_at, importance
    ttl: timedelta | None
```

### 6.4 与 LangGraph 的集成

新增节点 `recall_memory`，插在 `load_context` 之后、`classify_intent` 之前：

```
load_context → recall_memory → classify_intent → ...
                                                   ↓
                                                 [end of graph]
                                                   ↓
                                              persist_memory  (新)
```

- `recall_memory`：从三层取 top-k 注入 `state["memory_context"]`，prompt 拼接时优先消费
- `persist_memory`：在每个终结分支前写入新的 episodic 记录（问题、最终 SQL、是否成功、用户反馈如有）

新增 `AgentState` 字段：
```python
class AgentState(TypedDict, total=False):
    memory_context: list[MemoryRecord]      # 本轮注入的记忆
    memory_writes: list[MemoryRecord]       # 待写出的新记忆
```

### 6.5 关键应用场景（first-class）

1. **术语别名学习**：用户两次说"buyer"被成功映射为 `Customer` 后，写入 `kind=alias`；下次直接命中
2. **失败模式记忆**：某查询历史失败 → 触发更保守的 SQL 生成路径
3. **DECIDE 阈值复用**：上次「VIP=订单数>50」被用户接受 → 下次类似问题默认沿用并提示
4. **意图分类加速**：相似历史问题命中 → 跳过 LLM 调用直接给意图（带置信度阈值）

### 6.6 隐私与合规

- 所有 memory 必须包含 `tenant_id`，跨租户**不可达**（在 `recall` 实现里硬约束）
- `forget(principal, kind="all")` 必须可被 GDPR/删除请求触发
- Working memory 永远不写盘（仅 Redis with TTL），避免敏感问答永久化

### 6.7 测试策略

- Unit：`InMemoryMemoryStore` 验证 recall/remember/forget 正确性
- 跨租户：`tenantA.remember(...)` → `tenantB.recall(...)` 必须返回空
- 容量：episodic 超过限额时按 importance + recency 淘汰

---

## 7. 实施阶段（独立可 ship，8–12 周）

| 阶段 | 周数 | 内容 | 退出标准 |
|------|------|------|----------|
| **Phase 0** | 1 | 配置外置基础设施：`.env` 加载、占位符替换、密钥白名单校验、`.env.example` 模板 | 现有 `vertex.credentials` / `langfuse.*` / LLM key 全部走环境变量；硬编码密钥启动报错 |
| **Phase A** | 1–2 | Module 1 骨架：`Principal` / `OntologyPolicyEngine` / `JsonlAuditLogger` / `authorize` 节点 | 跨租户访问被强制拒绝；audit 落 jsonl |
| **Phase B** | 1 | Module 2：EvalCase 模型 + IntentJudge + SQLEquivalenceJudge + smoke suite (30 case) | CI 自动跑 smoke，PR 评论 diff |
| **Phase C** | 2–3 | Module 3：先落 **ClickHouse**（CryptoHouse PoC，验证 Engine Alias + 列级映射全链路）→ 再落 PostgreSQL → StarRocks → Snowflake；同步实现 `engines` alias 层与 `aip:column` 注解解析 | ClickHouse PoC 跑通区块链跨链查询；PostgreSQL/StarRocks/Snowflake contract test 全绿 |
| **Phase D** | 1–2 | Module 4：MemoryStore 抽象 + InMemory + Postgres 实现 + recall/persist 节点 | 别名学习场景跑通；跨租户测试通过 |
| **Phase E** | 1 | 模块拼装 + 端到端验收：一个完整客户场景跑全链路 | 演示 demo 可被销售直接用 |
| **Phase F** | 1 | 模块五：Pipeline 阶段契约（§12）。建 `contracts.py`，把 5 个核心节点改成 stage handler + 节点适配层 | 所有 LangGraph 节点变薄到只做 state ↔ contract 适配；契约 schema 快照纳入 CI |

每个 Phase 都遵循 federation-layer 的「独立 PR、各自合并」节奏。

---

## 8. 配置与密钥管理（外置配置）

### 8.1 现状与问题

当前配置只支持两层：`config.yaml`（默认，git tracked）+ `config.local.yaml`（本地覆盖，gitignored）。但 Tier 1 引入了大量**敏感字段**：

- RBAC：OPA endpoint、JWT secret、tenant 鉴权 key
- Connectors：Snowflake account/keypair、StarRocks 密码、BigQuery service account JSON 路径
- Memory：Redis URL、Postgres DSN
- Audit：审计落库 DSN
- Eval：跑 full suite 时的 LLM API key

把这些塞进 `config.local.yaml` 在以下场景都不工作：
- **容器化部署**（k8s / Docker）：image 里不应有密钥；`local.yaml` 不是云原生标准
- **CI/CD**：GitHub Actions / Jenkins 注入秘密的标准方式是环境变量
- **多环境**（dev / staging / prod）：同一份 image 切环境，只换环境变量
- **合规审计**：密钥与配置同文件不利于隔离扫描

### 8.2 三层配置优先级

引入清晰的优先级链，**高优先级覆盖低优先级**：

```
环境变量 / .env 文件
        ↑   覆盖
config.local.yaml （本地开发覆盖，gitignored）
        ↑   覆盖
config.yaml       （默认值与结构骨架，git tracked，无密钥）
```

加载顺序（在 `src/config.py::load_config`）：
1. 加载 `config.yaml` → base dict
2. 若存在 `config.local.yaml` → 深合并覆盖
3. 加载 `.env`（若存在）→ 注入 `os.environ`
4. 对最终 dict 做 **变量替换**：所有 `${VAR}` / `${VAR:-default}` 占位符从环境变量解析
5. 对**敏感字段白名单**强制要求来自环境变量；硬编码值直接报错

### 8.3 占位符语法

YAML 内统一使用 shell 风格占位符（与 Docker Compose、k8s 一致，降低运维心智负担）：

```yaml
connectors:
  snowflake_prod:
    type: snowflake
    account: ${SNOWFLAKE_ACCOUNT}                  # 必须存在，否则启动失败
    auth:
      kind: keypair
      key_path: ${SNOWFLAKE_KEY_PATH:-/run/secrets/snowflake_key}   # 默认值兜底
    warehouse: ${SNOWFLAKE_WAREHOUSE:-AGENT_WH}
    role: ANALYST_RO                                # 非密，可硬编码

audit:
  backend: postgres
  dsn: ${AUDIT_PG_DSN}                              # 强制环境变量

memory:
  episodic:
    backend: postgres
    dsn: ${MEMORY_PG_DSN}
  working:
    backend: redis
    url: ${REDIS_URL:-redis://localhost:6379/0}

langfuse:
  enabled: ${LANGFUSE_ENABLED:-false}
  public_key: ${LANGFUSE_PUBLIC_KEY}
  secret_key: ${LANGFUSE_SECRET_KEY}
  host: ${LANGFUSE_HOST:-https://cloud.langfuse.com}
```

**语法规则**：
- `${VAR}` — 必须存在，缺失时启动报错
- `${VAR:-default}` — 缺失时用 default
- `${VAR:?msg}` — 缺失时报错并打印 msg（用于敏感字段提示）
- 不递归解析、不支持嵌套 `${${...}}`，避免注入面

### 8.4 .env 加载策略

借助 `python-dotenv`（已是事实标准），但有约束：

- **加载点统一**：仅在 `src/config.py::load_config` 入口加载一次，禁止业务代码自行 `load_dotenv()`
- **路径优先级**：`./.env.local` > `./.env` > 系统环境变量（**已存在的环境变量不被 .env 覆盖**，避免容器内被本地遗留覆盖）
- **`.env` 必须 gitignored**，仓库根目录提供 `.env.example` 模板（git tracked），列出所有支持的变量及其说明
- **生产部署不依赖 .env 文件**：k8s/Docker 直接传环境变量，`.env` 只服务本地开发

### 8.5 敏感字段白名单与启动校验

新增 `src/config/schema.py`，声明哪些字段**必须来自环境变量**：

```python
SECRET_FIELDS = {
    "connectors.*.auth.password",
    "connectors.*.auth.private_key",
    "audit.dsn",
    "memory.*.dsn",
    "memory.*.url",
    "langfuse.secret_key",
    "vertex.credentials",
    "openai.api_key",
    "openrouter.api_key",
}
```

`load_config` 在替换占位符之后，对每个匹配字段检查值不是「裸字符串」，必须是从 `os.environ` 解析而来。检测到硬编码密钥 → 启动失败并打印明确提示：

```
[CONFIG ERROR] connectors.snowflake_prod.auth.private_key contains a literal value.
Sensitive fields must be injected via environment variable. Use:
  connectors.snowflake_prod.auth.private_key: ${SNOWFLAKE_PRIVATE_KEY}
```

### 8.6 .env.example 模板（仓库根目录）

```bash
# === LLM Providers ===
OPENAI_API_KEY=
OPENROUTER_API_KEY=
VERTEX_CREDENTIALS=/absolute/path/to/sa.json

# === Connectors ===
SNOWFLAKE_ACCOUNT=
SNOWFLAKE_KEY_PATH=
SNOWFLAKE_WAREHOUSE=AGENT_WH
STARROCKS_HOST=
STARROCKS_PASSWORD=
POSTGRES_DSN=postgresql://user:pass@host:5432/db

# === Memory & Audit Backends ===
REDIS_URL=redis://localhost:6379/0
MEMORY_PG_DSN=
AUDIT_PG_DSN=

# === Observability ===
LANGFUSE_ENABLED=false
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com

# === Tenancy (single-tenant default) ===
DEFAULT_TENANT_ID=default
```

### 8.7 多环境配置（dev / staging / prod）

不引入 `config.dev.yaml` / `config.prod.yaml` 之类多文件方案——**容易漂移**。改用环境变量切换：

```bash
ENV=staging .env  # 不同环境用不同 .env 文件
ENV=prod   k8s configmap + secret
```

`config.yaml` 中可以用 `ENV` 做条件，但**慎用**——大多数情况下「一份 yaml + 不同环境变量」就够。

### 8.8 与四个 Tier 1 模块的对接清单

| 模块 | 必须外置的字段 |
|------|----------------|
| RBAC | `OPA_URL`、`JWT_SECRET`、`DEFAULT_TENANT_ID` |
| Audit | `AUDIT_PG_DSN`、`AUDIT_BACKEND`(jsonl/postgres) |
| Connectors | 见 §8.6，每个 connector 凭据全部环境变量 |
| Memory | `REDIS_URL`、`MEMORY_PG_DSN`、`MEMORY_VECTOR_BACKEND` |
| Eval | `EVAL_LLM_API_KEY`（与生产 LLM key 隔离，避免 eval 误烧生产配额） |
| Langfuse | `LANGFUSE_*` 已存在，纳入新校验 |

### 8.9 测试策略

- 单元：`load_config` 处理各种占位符（缺失/默认/硬编码密钥被拒）
- 集成：用 `monkeypatch.setenv` 模拟环境变量，验证全链路注入
- 安全回归：强制扫描 `config.yaml` / `config.local.yaml.example`，检测裸密钥（CI 步骤）

---

## 9. 风险与取舍

| 风险 | 影响 | 缓解 |
|------|------|------|
| RBAC 注入 SQL 时 sqlglot 改写出错 → 漏数据 | 高（合规事故） | 对每条 rewritten_sql 做 dry-run 解析校验 + audit 中持久化 before/after |
| Eval 数据集成本（人工标注） | 中 | 第一批 30 条人工，后续从生产 trace 抽样 + LLM 协助生成 → 人工 review |
| 真实云数仓 CI 成本 | 中 | live 测试只在 nightly 和 release 跑，PR 阶段只跑 contract |
| Memory 引入幻觉（错误别名被记住） | 中 | 仅在用户**显式确认**或查询成功后才写 episodic；带 confidence + decay |
| 范围漂移：4 个模块同时上易失控 | 高 | 严格按 Phase A→E 串行；每 Phase 不通过不进下一阶段 |

**关键非取舍**：以上四个模块**都要做**。任何一个砍掉都意味着 Tier 1 没完成，仍然进不了商业 PoC。可调整的是顺序与深度（比如 Snowflake 可以先只支持 keypair 不支持 OAuth），但不能跳过整块。

---

## 10. 不变量（Architectural Invariants）

完成 Tier 1 后，以下事实必须永远成立：

1. **任何到达 Executor 的 SQL，都已经过 PolicyEngine 授权**——没有「绕过授权」的代码路径
2. **任何 Executor 调用，都伴随一条 AuditEvent**——成功失败都不例外
3. **任何 Memory recall，都受 `tenant_id` 硬隔离**——跨租户读取在数据访问层就不可能
4. **任何 prompt 改动，都要先过 smoke eval**——CI 强制门禁
5. **任何新 connector，都实现 capabilities 声明**——Planner 不允许假设能力
6. **`Principal` 是不可变值对象，全程只读传递**——不允许节点函数构造或修改
7. **所有密钥仅来自环境变量 / .env**——`config.yaml` 和 git 历史中永不出现裸密钥；启动时强制校验
8. **配置加载是单一入口的纯函数**——`load_config()` 是唯一的配置真源，业务代码禁止直接读 `os.environ` 或自行 `load_dotenv()`
9. **节点函数不含业务逻辑**——只做 `AgentState` ↔ stage contract 的适配（详见 §12）
10. **stage handler 是纯函数 + 注入依赖**——不读全局变量、不持有可变状态
11. **任何阶段间数据流必须经过 contract**——禁止节点 A 直接写 state 字段被节点 B 隐式消费
12. **生产部署的 audit 失败 = fail-closed**——`AuditLogger.emit` 抛异常时该次查询必须被拒绝；MVP 的 `JsonlAuditLogger` 可放宽为 warn-and-continue 但 prod backend（Postgres）默认 fail-closed
13. **行过滤值仅经 sqlglot Literal 注入**——禁止字符串拼接 SQL；`row_filter_template` 中 `$principal.*` 占位的最终字面量必须由 `sqlglot.exp.Literal` 序列化

---

## 11. 成功标准

Tier 1 完成的客观信号：

- [ ] 销售可以拿出一份「合规清单」回答金融客户的尽调问卷（RBAC、审计、租户隔离、删除权）
- [ ] 准确率报告每周自动产出，可附在客户月报里
- [ ] 三个生产连接器（Snowflake / StarRocks / Postgres）有真实客户数据 PoC 跑通
- [ ] 同一用户连续 5 轮对话，第 5 轮明显比第 1 轮更准/更快（memory 生效的可观测证据）
- [ ] Langfuse 上能完整看到 `auth.check → memory.recall → classify → plan → connector.execute → audit.emit` 的 span 链路
- [ ] 同一份 image / 代码包可以通过切换环境变量在 dev / staging / prod 三套环境跑通，配置文件中无任何密钥

满足以上 5 项即可宣布 **Tier 1 商业准入达成**，进入 Tier 2 议程（Plan-Critic、本体演进、Tool 扩展超越 SQL）。

---

## 12. 模块五：Pipeline 阶段契约（Stage Contracts）

### 12.1 现状与问题

当前 Pipeline 概念上是五段：

```
chat → ontology mapping → generate sql → execute → report/analytics
```

LangGraph 的节点抽象已经把"步骤"分离了，但**阶段间契约**仍然是松的：

- `AgentState` 是 `TypedDict(total=False)`，所有字段可选，类型检查不强制
- 「`generate_sql` 之后 state 一定有 `sql`」无法被静态验证
- 想替换某段实现（换 SQL 生成策略 / 换 report 风格）必须读全部下游节点，确认它依赖了哪些 state 字段
- 单元测试单段节点时，需要构造完整 AgentState，很多字段无关

直接的负面后果：
- **迭代摩擦**：每改一个节点都担心打到下游
- **定位问题难**：bug 出在哪段？没有清晰的输入/输出快照
- **不可换实现**：本来想 A/B 两个 SQL 生成器对比，必须同时改下游消费方

但**不应该把它拆成多 agent**——五段全是顺序、确定性 pipeline，没有自主决策需求。多 agent 带来的 LLM round-trip 和协议开销不成比例。

### 12.2 核心思路：强类型阶段契约（不动 LangGraph）

每个阶段定义 **Pydantic 输入/输出模型**，节点函数依旧是 LangGraph 节点（不改签名），但内部委托给纯函数 stage handler：

```python
# src/agent/stages/contracts.py — 单一文件、易索引
from pydantic import BaseModel
from typing import Literal

class IntentInput(BaseModel):
    question: str
    memory_context: list["MemoryRecord"] = []
    clarify_count: int = 0

class IntentOutput(BaseModel):
    intent: Literal["READ","WRITE","ANALYZE","DECIDE","OPERATE","UNCLEAR"]
    confidence: float
    rationale: str

class OntologyMappingInput(BaseModel):
    intent: str
    question: str
    ontology_context: str

class OntologyMappingOutput(BaseModel):
    referenced_entities: list[str]    # 命中的本体类
    semantic_filters: dict            # 解析出的语义条件
    rationale: str

class SQLGenInput(BaseModel):
    intent: str
    question: str
    referenced_entities: list[str]
    ontology_context: str
    dialect: str

class SQLGenOutput(BaseModel):
    sql: str
    permission_level: Literal["auto","confirm","deny"]
    referenced_tables: list[str]

class ExecuteInput(BaseModel):
    sql: str
    permission_level: str
    approved: bool = False
    principal: "Principal | None" = None

class ExecuteOutput(BaseModel):
    rows: list[dict]
    row_count: int
    error: str | None = None
    operation: Literal["read","write","decide","operate"]

class ReportInput(BaseModel):
    question: str
    intent: str
    rows: list[dict]
    referenced_entities: list[str]
    units: dict[str, str] = {}        # 列 → 单位（来自 aip:column ont:unit）

class ReportOutput(BaseModel):
    response: str                      # 自然语言回答
    visual_hints: list[dict] = []      # 可选：图表建议
```

### 12.3 Stage Handler — 纯函数

每个 stage 是 **`(Input, Deps) -> Output` 的纯函数**，与 LangGraph state 无关：

```python
# src/agent/stages/sql_generator.py
from src.agent.stages.contracts import SQLGenInput, SQLGenOutput

class SQLGenerator:
    def __init__(self, llm: LLMClient): self._llm = llm

    def run(self, inp: SQLGenInput) -> SQLGenOutput:
        prompt = self._build_prompt(inp)
        raw = self._llm.chat([{"role": "user", "content": prompt}])
        sql = clean_sql(raw)
        return SQLGenOutput(
            sql=sql,
            permission_level=detect_permission_level(sql),
            referenced_tables=self._parse_referenced(sql),
        )
```

### 12.4 节点适配层（保持 LangGraph 不变）

节点函数变成「读 state → 构造 Input → 调 stage → 序列化输出回 state」的薄壳：

```python
# src/agent/nodes/read_write.py
def generate_sql(state: AgentState, llm: LLMClient, db_dialect: str) -> dict:
    inp = SQLGenInput(
        intent=state["intent"],
        question=state["question"],
        referenced_entities=state.get("referenced_entities", []),
        ontology_context=state["ontology_context"],
        dialect=db_dialect,
    )
    out = SQLGenerator(llm).run(inp)
    return out.model_dump()    # state 自动获得 sql / permission_level / referenced_tables
```

**关键约束**：节点函数**只做适配**，不做业务逻辑。所有逻辑在 stage handler 里。这条是死规矩。

### 12.5 模块布局

```
src/agent/
├── stages/
│   ├── __init__.py
│   ├── contracts.py          # 全部 Input/Output Pydantic 模型，单一事实源
│   ├── intent_classifier.py
│   ├── ontology_mapper.py
│   ├── sql_generator.py
│   ├── executor_stage.py     # 包装 BaseExecutor，注入 RBAC + audit
│   └── report_generator.py
├── nodes/                    # 现有目录不变；只是变薄
│   ├── read_write.py
│   ├── analyze.py
│   └── decide_operate.py
└── graph.py                  # 不变
```

stages 是**水平复用层**：一个 `SQLGenerator` 同时被 READ / WRITE / DECIDE 三个意图节点复用，不再每个节点各写一遍。

### 12.6 收益清单

| 维度 | 收益 |
|------|------|
| **类型安全** | 改 contract 时 mypy/Pydantic 立刻报错下游断点 |
| **可独立测试** | `SQLGenerator(fake_llm).run(inp)` 不需要 AgentState |
| **可独立替换实现** | A/B 两个 SQL 生成器 → 节点适配层选其一即可 |
| **Langfuse 单阶段 span** | stage 名（`stage.sql_gen`）作 span，trace 自然分层 |
| **未来拆 agent 零成本** | 任意 stage handler 可被替换为「内部循环的 sub-agent」 |
| **新人上手** | 看 `contracts.py` 一文件即可理解全局数据流 |

### 12.7 哪些 stage 未来值得升格成 sub-agent（Tier 2 议程）

不是全部。只有需要**自主性**（循环/调多个 tool）的才升格：

| Stage | 升格？ | 理由 |
|-------|--------|------|
| Intent classifier | ❌ | 单次 LLM call 够 |
| Ontology mapper | ❌ | 检索 + 校验，无循环 |
| **SQL generator + critic** | ✅ | 需要「生成 → 验证 → 修正」自循环（Plan-Critic-Repair） |
| Executor | ❌ | 纯代码 |
| **Report/Analytics** | ✅ | 多步分析，可能调多个 tool（图表/统计/二次查询） |

升格的具体形态：handler 内部跑自己的小 LangGraph，对外契约不变（`Input → Output`），调用方无感。

### 12.8 与已有节点的迁移路径

不一次性重写。按 stage 渐进迁移：

1. 先建 `contracts.py`，定义全部 5 个 stage 的 Input/Output（小 PR）
2. 把 `generate_sql` 一个节点迁过去（验证范式）
3. 同期 PR 迁 `classify_intent`、`format_result`
4. 最后迁 `execute_sql`（最重，依赖 Federation Layer）

每个 PR 独立 ship，pytest 全绿才合并。

### 12.9 与 Tier 1 其他模块的协同

| 与谁 | 关系 |
|------|------|
| RBAC | `ExecuteInput.principal` 是契约的一部分，没有 principal 不能跑 |
| Audit | stage handler 入口/出口自动 emit 事件（不在节点里散落） |
| Memory | `IntentInput.memory_context` 显式声明依赖，不再隐式从 state 读 |
| Eval | `EvalCase.expected` 可针对单个 stage 的输出断言（不必跑完整 pipeline） |
| 配置外置 | stage handler 通过 `__init__` 接收依赖，配置仍由 `build_graph` 注入 |

### 12.10 测试策略

- **Stage 单元测试**：`tests/agent/stages/test_<stage>.py`，FakeLLM/FakeExecutor + Pydantic 输入构造
- **契约回归**：单独一个 `tests/agent/stages/test_contracts.py`，固化每个 stage 的 schema（json schema 快照对比），任何契约变更都被 review 看到
- **节点适配层测试**：保留现有 `tests/agent/test_*.py`，仅校验 state ↔ contract 的双向转换正确性

### 12.11 不变量（追加到 §10）

完成模块五后，新增以下不变量：

7. **节点函数不含业务逻辑**——只做 state ↔ stage contract 的适配
8. **stage handler 是纯函数 + 注入依赖**——不读全局变量，不持有可变状态
9. **任何阶段间数据流必须经过 contract**——禁止节点 A 直接写 state 字段被节点 B 隐式消费

### 12.12 成本估算

约 **1 周**（在 Tier 1 末尾追加，编号 Phase F）：
- 1 天：写 contracts.py + 5 个 stage handler 骨架
- 2 天：迁移现有 5 个核心节点
- 1 天：补 stage 单元测试 + 契约 schema 快照
- 1 天：Langfuse span 命名调整 + 文档更新



本附录用一个**端到端可落地**的样例，验证 §5.7（Engine Alias）、§5.8（列级映射）、§5.9（ClickHouseExecutor）三件事在真实数据源上跑通。

### A.1 数据源

- **CryptoHouse**：ClickHouse 官方提供的免费区块链分析端点（`crypto.clickhouse.com`），覆盖 Bitcoin / Ethereum / Solana / Polygon / Arbitrum / Near 等链的 transactions / blocks / logs / token_transfers
- **接入方式**：HTTPS + ClickHouse SQL；公开访客凭据，零鉴权门槛
- **PoC 价值**：免费、跨链、表名/列名命名风格异于本体语义层，**完美压测**本节三个新机制

### A.2 配置（`config.local.yaml` + `.env`）

```yaml
connectors:
  clickhouse_crypto:
    type: clickhouse
    host: ${CRYPTOHOUSE_HOST:-https://crypto.clickhouse.com}
    user: ${CRYPTOHOUSE_USER:-explorer}
    password: ${CRYPTOHOUSE_PASSWORD:-}
    database: bitcoin                       # 默认库；查询可跨库
    pool: { min: 1, max: 5, idle_timeout: 300 }
    query_timeout: 60

engines:
  blockchain: clickhouse_crypto             # 本体引用 alias
```

`.env`：
```bash
CRYPTOHOUSE_HOST=https://crypto.clickhouse.com
CRYPTOHOUSE_USER=explorer
CRYPTOHOUSE_PASSWORD=
```

### A.3 本体（`ontologies/blockchain.rdf` 节选）

```xml
<owl:Class rdf:about=".../Transaction/Bitcoin">
    <rdfs:label>Bitcoin Transaction</rdfs:label>
    <rdfs:comment>A confirmed Bitcoin on-chain transaction</rdfs:comment>
    <aip:physicalTable>bitcoin.transactions</aip:physicalTable>
    <aip:queryEngine>blockchain</aip:queryEngine>
    <aip:partitionKeys>block_date</aip:partitionKeys>
    <aip:column ont:logical="txHash"      ont:physical="hash"/>
    <aip:column ont:logical="blockHeight" ont:physical="block_number"/>
    <aip:column ont:logical="amount"      ont:physical="output_value"
                ont:transform="value / 1e8" ont:unit="BTC"/>
    <aip:column ont:logical="timestamp"   ont:physical="block_timestamp"/>
</owl:Class>

<owl:Class rdf:about=".../Transaction/Ethereum">
    <rdfs:label>Ethereum Transaction</rdfs:label>
    <aip:physicalTable>ethereum.transactions</aip:physicalTable>
    <aip:queryEngine>blockchain</aip:queryEngine>
    <aip:partitionKeys>block_date</aip:partitionKeys>
    <aip:column ont:logical="amount" ont:physical="value"
                ont:transform="value / 1e18" ont:unit="ETH"/>
    <aip:column ont:logical="timestamp" ont:physical="block_timestamp"/>
</owl:Class>

<!-- 跨链虚拟视图（联邦层 isVirtual 已支持） -->
<owl:Class rdf:about=".../Transaction/AnyChain">
    <aip:isVirtual>true</aip:isVirtual>
    <aip:basedOn>Transaction/Bitcoin,Transaction/Ethereum</aip:basedOn>
    <rdfs:comment>Union view across all chains</rdfs:comment>
</owl:Class>
```

### A.4 PoC 验收用例（写入 `tests/eval/datasets/blockchain.yaml`）

| 用例 | 自然语言问题 | 期望意图 | 验证点 |
|------|--------------|---------|--------|
| **bc-001** | 过去 24 小时比特币有多少笔交易？ | READ | 单源路由 + 时间过滤下推 |
| **bc-002** | 昨天比特币交易总金额是多少 BTC？ | READ | `output_value/1e8` transform 正确生效，结果带 BTC 单位 |
| **bc-003** | 过去 24 小时 BTC 和 ETH 各多少笔交易？ | READ | 跨库 UNION，单 connector |
| **bc-004** | 过去一周交易额最大的 10 个比特币地址 | ANALYZE | 分组聚合 + ORDER BY + 分区裁剪 |
| **bc-005** | 我们的 VIP 客户钱包（内部表）最近一周链上活动 | READ | **跨 connector 联邦**：CryptoHouse + 内部 SQLite/Postgres，DuckDB stitch |
| **bc-006** | 把交易额降序排前 100 的比特币地址加入观察名单（内部表） | OPERATE | 跨 connector 读 + 写：read CryptoHouse → write 内部 |

bc-005 / bc-006 是**真正的客户价值证明**——单一 connector 任何 BI 工具都能做，跨 connector 联邦才是本项目的差异化。

### A.5 验收标准

PoC 完成的客观信号：
- [ ] 6 条用例在 eval suite 中全绿（IntentJudge + ResultSetJudge）
- [ ] Langfuse trace 可见 `connector.clickhouse_crypto.execute` span，带 SQL + 行数
- [ ] bc-002 的 `1e8` transform 在 trace 的 SQL 文本中可见，回答里出现「BTC」单位
- [ ] bc-005 的 trace 显示两条并行 `federation.sub_*` span（CryptoHouse + 内部库）和一条 `federation.join[duckdb]`
- [ ] 不修改本体文件，仅调整 `engines.blockchain` 指向，能切换到本地 docker ClickHouse 实例（验证 Engine Alias 解耦）

### A.6 PoC 之后的扩展面

CryptoHouse 跑通后，下列商业场景的边际成本极低：
- **观测性数据**：Loki/SigNoz/Uptrace 都用 ClickHouse → 复用 connector
- **用户行为分析**：PostHog/Plausible → 复用
- **金融市场数据**：QuestDB/ClickHouse 时序场景 → 仅换 connector 实例
- **自有区块链产品**：Web3 / DeFi / NFT 客户的内部 ClickHouse → 直接对接

**Engine Alias + 列级映射两件事一次做对，区块链场景的所有客户都能复用同一套基础设施。**
