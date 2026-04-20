# DECIDE/OPERATE 意图 + 模式 D 架构设计 Spec

**日期:** 2026-04-20
**状态:** Draft
**依赖:** `2026-04-16-ontology-data-agent-design.md`（现有架构基础）
**探索工具:** Gemini CLI（`gemini` 命令）

---

## 1. 背景与动机

### 1.1 现有意图的局限

当前系统支持四种意图：`READ / WRITE / ANALYZE / UNCLEAR`。这已覆盖了简单的查询与写入场景，但缺少以下能力：

| 缺失能力 | 典型用户需求 |
|---------|-------------|
| **基于业务规则的决策** | "哪些订单需要取消？" → 需要对照规则判断，不只是查询 |
| **编排式多步操作** | "处理月末对账" → 多张表、多步骤、需要回滚保障 |
| **规则的动态覆盖** | "今天特殊情况，跳过自动取消" → 规则在运行时可以被 Prompt 覆盖 |

### 1.2 模式 D 的核心思想

> **RDF 注解 = 默认业务规则；用户 Prompt = 运行时覆盖。**

```
RDF 本体文件（静态）              用户自然语言 Prompt（动态）
─────────────────────────       ──────────────────────────────────
aip:decisionRule               "今天例外，不要自动取消"
aip:operationSteps             "只执行前两步，后面我手动"
aip:requiresApproval           "跳过审批，直接执行"
aip:overridable = true  ←───── LLM 提取 user_overrides，合并后执行
```

- RDF 注解以 `aip:` 前缀写在 OWL 文件中，由 `ontology/parser.py` 在启动时解析
- Agent 在 DECIDE/OPERATE 路径上先读取这些默认规则，再用 LLM 提取用户覆盖意图，最后合并决策

---

## 2. 两种新意图定义

### DECIDE 意图

**触发条件：** 用户想让 Agent 代替或辅助做出一个业务判断，而不只是查询数据。

| 特征 | 说明 |
|------|------|
| 问句形式 | "哪些…需要…"、"该不该…"、"推荐…"、"是否应当…" |
| 依赖规则 | 必须对照 RDF `aip:decisionRule` 或领域知识来判断 |
| 输出 | 推荐动作 + 理由 + 受影响实体列表；可选：触发后续 OPERATE |
| 典型例子 | "哪些客户应该升级为 VIP？"、"哪些库存需要补货？" |

### OPERATE 意图

**触发条件：** 用户想执行一个由多步 SQL 组成的业务操作。

| 特征 | 说明 |
|------|------|
| 问句形式 | "处理…"、"执行…"、"完成…"、"批量…" |
| 依赖规则 | RDF `aip:operationSteps` 提供默认步骤脚手架，LLM 生成具体 SQL |
| 输出 | 每步执行结果 + 最终汇总；失败时触发回滚 |
| 典型例子 | "处理所有逾期订单"、"完成月末库存对账" |

---

## 3. RDF 注解扩展（aip: namespace）

### 3.1 新增注解属性定义

在每个 `.rdf` 文件头部添加 `aip:` namespace 和属性声明：

```xml
<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns:owl="http://www.w3.org/2002/07/owl#"
    xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
    xmlns:xsd="http://www.w3.org/2001/XMLSchema#"
    xmlns:ont="http://example.org/ontology/retail-store/"
    xmlns:aip="http://aip.example.org/rules#">    <!-- 新增 -->

    <!-- 注解属性声明 -->
    <owl:AnnotationProperty rdf:about="http://aip.example.org/rules#decisionRule"/>
    <owl:AnnotationProperty rdf:about="http://aip.example.org/rules#operationSteps"/>
    <owl:AnnotationProperty rdf:about="http://aip.example.org/rules#requiresApproval"/>
    <owl:AnnotationProperty rdf:about="http://aip.example.org/rules#rollbackable"/>
    <owl:AnnotationProperty rdf:about="http://aip.example.org/rules#overridable"/>
    <owl:AnnotationProperty rdf:about="http://aip.example.org/rules#severity"/>
```

### 3.2 类级别注解示例（retail.rdf / Order 类）

```xml
<owl:Class rdf:about="http://example.org/ontology/retail-store/Order">
    <rdfs:label>Order</rdfs:label>

    <!-- 决策规则：当订单状态为 overdue 且超过30天，推荐取消 -->
    <aip:decisionRule>IF status='overdue' AND days_since_due > 30 THEN recommend_cancel</aip:decisionRule>

    <!-- 操作步骤：处理逾期订单的标准流程 -->
    <aip:operationSteps>
        1:verify_overdue_orders,
        2:notify_affected_customers,
        3:update_order_status_to_cancelled,
        4:restore_product_stock
    </aip:operationSteps>

    <!-- 默认需要用户确认；值：auto | user | admin -->
    <aip:requiresApproval>user</aip:requiresApproval>

    <!-- 支持回滚 -->
    <aip:rollbackable rdf:datatype="http://www.w3.org/2001/XMLSchema#boolean">true</aip:rollbackable>

    <!-- 此规则可被用户 Prompt 覆盖 -->
    <aip:overridable rdf:datatype="http://www.w3.org/2001/XMLSchema#boolean">true</aip:overridable>

    <!-- 影响级别：low | medium | high | critical -->
    <aip:severity>medium</aip:severity>
</owl:Class>
```

### 3.3 属性语义说明

| 属性 | 类型 | 含义 | 可覆盖 |
|------|------|------|--------|
| `aip:decisionRule` | string | IF-THEN 决策规则（自然语言或结构化） | 是 |
| `aip:operationSteps` | string | 逗号分隔的操作步骤序列 | 是（步骤子集） |
| `aip:requiresApproval` | enum | 执行前的审批级别 | 是 |
| `aip:rollbackable` | boolean | 是否支持自动回滚 | 否（安全硬约束） |
| `aip:overridable` | boolean | 是否允许用户 Prompt 覆盖 | 否（元规则） |
| `aip:severity` | enum | 操作影响严重程度 | 否 |

---

## 4. 使用 Gemini CLI 的实现探索

本节展示用 `gemini` CLI 对核心 Prompt 设计进行快速探索验证的方法，在写代码之前先锁定 Prompt 效果。

### 4.1 探索：意图分类扩展

验证 LLM 能否正确区分 DECIDE/OPERATE 与现有意图：

```bash
gemini -p "
你是一个数据 Agent 的意图分类器。
给定用户查询和数据库 Schema，将意图分类为以下之一：
  READ    - 简单查询，单条 SELECT
  WRITE   - 修改数据，INSERT/UPDATE/DELETE
  ANALYZE - 多步只读分析，需要多次查询做比较/趋势
  DECIDE  - 基于业务规则做出判断和推荐，可能涉及写操作
  OPERATE - 执行编排式多步业务操作（有步骤序列、可能需回滚）
  UNCLEAR - 模糊或与数据库无关

Schema: Retail 领域（orders, customers, products, order_items, categories 表）
Business Rules: Order[decisionRule='IF status=overdue AND days>30 THEN recommend_cancel']

测试用例（每行一个，只输出意图词）：
1. 哪些订单的总金额最高？
2. 把所有逾期订单标记为取消
3. 哪些客户本月消费下降超过50%？
4. 哪些订单应该被取消？
5. 处理所有逾期订单（验证、通知客户、取消、恢复库存）
6. 给我看看天气
"
# 期望输出: READ, WRITE, ANALYZE, DECIDE, OPERATE, UNCLEAR
```

### 4.2 探索：从用户 Prompt 提取覆盖意图

验证 LLM 能否从自然语言中精确提取规则覆盖：

```bash
gemini -p "
从以下用户 Prompt 中提取对业务规则的覆盖意图。
以 JSON 格式输出，包含字段：
  skip_approval: bool         - 是否跳过审批
  skip_steps: list[str]       - 要跳过的步骤名称列表
  override_rules: list[str]   - 要覆盖的具体规则描述
  reason: str                 - 用户给出的理由（若有）

测试 Prompt：
1. '正常处理，按规则来'
2. '今天特殊情况，跳过发通知这一步'
3. '这批紧急，不需要我确认，直接执行'
4. '只处理前两步，后面我手动操作'
5. '跳过审批，但保留回滚保障'
"
# 期望输出：结构化 JSON，如 {skip_approval: false, skip_steps: [], ...}
```

### 4.3 探索：决策推理生成

验证 LLM 能否结合规则 + 查询数据 + 用户覆盖，生成有据可查的推荐：

```bash
gemini -p "
你是一个业务决策助手。基于以下信息生成结构化决策推荐。

[业务规则（来自 RDF 本体）]
IF Order.status = 'overdue' AND days_since_due > 30 THEN recommend_cancel
requiresApproval = user
overridable = true

[用户覆盖]
skip_approval = false
override_rules = []

[查询到的数据]
overdue_orders = [
  {id: 101, customer: 'Zhang San', days_overdue: 45, amount: 580.0},
  {id: 102, customer: 'Li Si',     days_overdue: 33, amount: 120.0},
  {id: 103, customer: 'Wang Wu',   days_overdue: 28, amount: 890.0},  # 未满30天
]

输出 JSON：
{
  decision: 'cancel' | 'hold' | 'partial',
  affected_entities: [订单ID列表],
  excluded_entities: [不满足规则的订单ID及原因],
  reasoning: '推理说明（引用规则）',
  requires_approval: true/false,
  confidence: 0.0-1.0
}
"
# 期望：decision=cancel, affected=[101,102], excluded=[{id:103, reason:'28天未满30天阈值'}]
```

### 4.4 探索：操作步骤规划

验证 LLM 能否将 RDF 步骤脚手架转化为具体 SQL 操作计划：

```bash
gemini -p "
你是一个操作规划器。将以下 RDF 操作步骤脚手架转化为具体的可执行计划。

[RDF 操作步骤（来自本体注解）]
1:verify_overdue_orders,
2:notify_affected_customers,
3:update_order_status_to_cancelled,
4:restore_product_stock

[目标实体]
cancelled_order_ids = [101, 102]

[数据库 Schema]
orders(id, customer_id, status, total_amount, order_date)
customers(id, name, email, phone)
order_items(id, order_id, product_id, quantity, unit_price)
products(id, name, stock_qty, price)

[用户覆盖]
skip_steps = ['notify_affected_customers']  # 用户说跳过通知

输出 JSON 数组，每个步骤包含：
{
  step_name: str,
  description: str,
  sql: str,          # 具体的 SQL 语句
  skipped: bool,     # 是否因用户覆盖而跳过
  skip_reason: str   # 跳过原因（若适用）
}
"
```

### 4.5 探索：回滚 SQL 生成

验证 LLM 能否基于已执行的操作步骤自动生成回滚 SQL：

```bash
gemini -p "
基于以下已执行操作，生成对应的回滚 SQL。
每条回滚 SQL 必须是幂等的（可重复执行不产生副作用）。

已执行步骤（执行顺序，回滚时逆序）:
1. UPDATE orders SET status='cancelled' WHERE id IN (101, 102)
   之前的状态: [{id:101, old_status:'overdue'}, {id:102, old_status:'overdue'}]

2. UPDATE products SET stock_qty = stock_qty + 3 WHERE id = 5
   之前的值: [{id:5, old_stock_qty: 42}]  -- 恢复至42，不是减3（幂等要求）

输出 JSON 数组（逆序）：
[
  {step: 2, rollback_sql: '...', idempotent: true},
  {step: 1, rollback_sql: '...', idempotent: true}
]
"
```

---

## 5. 扩展后的 Agent 架构

### 5.1 新增节点

| 节点 | 输入 | 输出 | LLM 调用 |
|------|------|------|----------|
| `extract_rdf_rules` | `ontology_context` + OWL 文件 | `rdf_rules: dict` | 否（纯解析） |
| `extract_user_overrides` | `user_query` + `rdf_rules` | `user_overrides: dict` | 是 |
| `apply_decision` | `rdf_rules` + `user_overrides` + 查询数据 | `decision: dict` | 是 |
| `present_decision` | `decision` | `response` (推荐展示给用户) | 否 |
| `plan_operation` | `rdf_rules` + `user_overrides` + `decision` | `operation_plan: list[OperationStep]` | 是 |
| `execute_operation_step` | `operation_plan[current_index]` | `operation_results`, `rollback_stack` | 否 |
| `rollback_operations` | `rollback_stack` | `response` (回滚汇报) | 否 |

### 5.2 完整图流程

```
START
  → load_ontology_context
  → extract_rdf_rules           [新增，非 LLM]
  → classify_intent             [扩展：增加 DECIDE/OPERATE 分类]
    ├─ [READ]     → generate_sql → execute_sql → format_result → END
    ├─ [WRITE]    → generate_sql → human_approval → execute_sql → format_result → END
    ├─ [ANALYZE]  → plan_analysis → execute_analysis_step(×N) → synthesize_results → END
    ├─ [UNCLEAR]  → clarify_question → classify_intent (max 2 retries) → END
    │
    ├─ [DECIDE]   → extract_user_overrides
    │                → generate_sql (READ: 查询判断所需数据)
    │                    → execute_sql (auto)
    │                        → apply_decision         [规则 + 覆盖 + 数据 → 推荐]
    │                            → present_decision   [展示推荐给用户]
    │                                ├─ [user confirms] → plan_operation
    │                                │                      → execute_operation_step(×N)
    │                                │                          ├─ [success] → synthesize_results → END
    │                                │                          └─ [error]   → rollback_operations → END
    │                                └─ [user rejects] → END (show cancelled message)
    │
    └─ [OPERATE]  → extract_user_overrides
                     → plan_operation               [RDF 脚手架 + LLM 生成具体步骤]
                         → execute_operation_step(×N)
                             ├─ [success] → synthesize_results → END
                             └─ [error]   → rollback_operations → END
```

### 5.3 扩展 AgentState

```python
from dataclasses import dataclass, field
from typing import TypedDict

@dataclass
class RDFRule:
    entity: str              # 实体类名，如 "Order"
    decision_rule: str       # IF-THEN 规则文本
    operation_steps: list[str]  # 操作步骤序列
    requires_approval: str   # auto | user | admin
    rollbackable: bool
    overridable: bool
    severity: str            # low | medium | high | critical

@dataclass
class UserOverrides:
    skip_approval: bool = False
    skip_steps: list[str] = field(default_factory=list)
    override_rules: list[str] = field(default_factory=list)
    reason: str = ""

@dataclass
class OperationStep:
    step_name: str
    description: str
    sql: str
    skipped: bool = False
    skip_reason: str = ""
    rollback_sql: str = ""     # 预生成的回滚 SQL（幂等）

@dataclass
class DecisionResult:
    decision: str              # recommend_cancel | hold | partial | ...
    affected_entities: list    # 受影响实体 ID 列表
    excluded_entities: list    # 排除的实体及原因
    reasoning: str
    requires_approval: bool
    confidence: float

class AgentState(TypedDict, total=False):
    # --- 现有字段（保持不变）---
    ontology_context: str
    user_query: str
    intent: str
    generated_sql: str
    permission_level: str
    approved: bool | None
    query_result: list[dict]
    affected_rows: int
    response: str
    error: str | None
    sql_error_message: str | None
    sql_retry_count: int
    clarify_count: int
    conversation_history: list[dict]
    result_summary: str
    analysis_plan: list[str]
    sub_results: list[dict]

    # --- 模式 D 新增字段 ---
    rdf_rules: dict[str, RDFRule]        # 从 RDF 解析的规则，key=entity name
    user_overrides: UserOverrides        # 从用户 Prompt 提取的覆盖意图
    decision: DecisionResult             # DECIDE 意图的决策结果
    operation_plan: list[OperationStep]  # OPERATE 意图的操作计划
    operation_results: list[dict]        # 每步执行结果
    rollback_stack: list[dict]           # 已执行步骤的回滚信息（逆序保存）
    current_op_index: int                # 当前执行到第几步
```

---

## 6. 关键节点实现草图

### 6.1 `extract_rdf_rules`（非 LLM，纯 rdflib 解析）

```python
AIP = Namespace("http://aip.example.org/rules#")

def extract_rdf_rules(state: AgentState, rdf_graph: Graph) -> dict:
    rules = {}
    for cls in rdf_graph.subjects(RDF.type, OWL.Class):
        label = str(rdf_graph.value(cls, RDFS.label) or cls.split("/")[-1])
        rule = RDFRule(
            entity=label,
            decision_rule=str(rdf_graph.value(cls, AIP.decisionRule) or ""),
            operation_steps=_parse_steps(rdf_graph.value(cls, AIP.operationSteps)),
            requires_approval=str(rdf_graph.value(cls, AIP.requiresApproval) or "user"),
            rollbackable=bool(rdf_graph.value(cls, AIP.rollbackable)),
            overridable=bool(rdf_graph.value(cls, AIP.overridable) or True),
            severity=str(rdf_graph.value(cls, AIP.severity) or "medium"),
        )
        if rule.decision_rule or rule.operation_steps:
            rules[label] = rule
    return {"rdf_rules": rules}

def _parse_steps(steps_str) -> list[str]:
    if not steps_str:
        return []
    # "1:verify_overdue,2:notify_customer" → ["verify_overdue", "notify_customer"]
    return [s.split(":", 1)[1].strip() for s in str(steps_str).split(",") if ":" in s]
```

### 6.2 `extract_user_overrides`（LLM）

```python
OVERRIDE_SYSTEM = """
从用户 Prompt 中提取对业务规则的覆盖意图。
输出严格的 JSON，包含以下字段（无其他文字）：
{
  "skip_approval": false,
  "skip_steps": [],
  "override_rules": [],
  "reason": ""
}
"""

def extract_user_overrides(state: AgentState, llm: LLMClient) -> dict:
    relevant_rules = _format_rdf_rules_for_prompt(state.get("rdf_rules", {}))
    messages = [{"role": "user", "content": (
        f"当前业务规则:\n{relevant_rules}\n\n"
        f"用户 Prompt: {state['user_query']}"
    )}]
    response = llm.chat(messages, system_prompt=OVERRIDE_SYSTEM, temperature=0.0)
    overrides = _parse_json_safe(response, UserOverrides())
    return {"user_overrides": overrides}
```

### 6.3 `apply_decision`（LLM）

```python
DECISION_SYSTEM = """
你是一个业务决策助手。基于 RDF 业务规则、用户覆盖设置和查询到的数据，
生成结构化决策推荐。

输出严格的 JSON：
{
  "decision": "recommend_cancel | hold | partial | ...",
  "affected_entities": [实体 ID 列表],
  "excluded_entities": [{"id": ..., "reason": "..."}],
  "reasoning": "引用规则的推理说明",
  "requires_approval": true/false,
  "confidence": 0.0-1.0
}
"""

def apply_decision(state: AgentState, llm: LLMClient) -> dict:
    rules = state.get("rdf_rules", {})
    overrides = state.get("user_overrides", UserOverrides())
    data = state.get("query_result", [])

    messages = [{"role": "user", "content": (
        f"业务规则:\n{_format_rules(rules)}\n\n"
        f"用户覆盖:\n{_format_overrides(overrides)}\n\n"
        f"查询数据:\n{str(data[:20])}\n\n"
        f"原始请求: {state['user_query']}"
    )}]
    response = llm.chat(messages, system_prompt=DECISION_SYSTEM, temperature=0.0)
    decision = _parse_json_safe(response, {})

    # 覆盖：若用户 skip_approval=True 且规则允许覆盖
    affected_rule = _find_relevant_rule(rules, state["user_query"])
    if overrides.skip_approval and affected_rule and affected_rule.overridable:
        decision["requires_approval"] = False

    return {"decision": decision}
```

### 6.4 `plan_operation`（LLM，使用 RDF 脚手架）

```python
PLAN_SYSTEM = """
你是一个操作规划器。将 RDF 操作步骤脚手架转化为具体的可执行计划。
每个步骤必须包含精确的 SQL 语句和对应的幂等回滚 SQL。

输出 JSON 数组：
[
  {
    "step_name": "verify_overdue_orders",
    "description": "查询需要处理的逾期订单",
    "sql": "SELECT ...",
    "skipped": false,
    "skip_reason": "",
    "rollback_sql": ""   // verify 步骤无需回滚
  },
  ...
]
"""

def plan_operation(state: AgentState, llm: LLMClient) -> dict:
    rules = state.get("rdf_rules", {})
    overrides = state.get("user_overrides", UserOverrides())
    decision = state.get("decision", {})

    # 找到相关实体的 RDF 操作步骤
    relevant_rule = _find_relevant_rule(rules, state["user_query"])
    rdf_steps = relevant_rule.operation_steps if relevant_rule else []

    messages = [{"role": "user", "content": (
        f"数据库 Schema:\n{state['ontology_context']}\n\n"
        f"RDF 操作步骤脚手架:\n{', '.join(rdf_steps) or '（无，请根据意图自行规划）'}\n\n"
        f"目标实体:\n{decision.get('affected_entities', [])}\n\n"
        f"用户覆盖（跳过的步骤）:\n{overrides.skip_steps}\n\n"
        f"用户请求: {state['user_query']}"
    )}]
    response = llm.chat(messages, system_prompt=PLAN_SYSTEM, temperature=0.0)
    plan = _parse_json_safe(response, [])
    return {"operation_plan": plan, "current_op_index": 0, "operation_results": [], "rollback_stack": []}
```

### 6.5 `execute_operation_step`（非 LLM，逐步执行）

```python
def execute_operation_step(state: AgentState, executor: BaseExecutor) -> dict:
    plan = state.get("operation_plan", [])
    idx = state.get("current_op_index", 0)
    results = list(state.get("operation_results", []))
    rollback_stack = list(state.get("rollback_stack", []))

    if idx >= len(plan):
        return {}

    step = plan[idx]

    # 跳过被用户覆盖的步骤
    if step.get("skipped"):
        results.append({"step": step["step_name"], "skipped": True, "reason": step["skip_reason"]})
        return {"operation_results": results, "current_op_index": idx + 1}

    try:
        result = executor.execute(step["sql"], approved=True)  # OPERATE 路径已经过整体审批
        if result.error:
            return {"error": result.error, "current_op_index": idx}  # 触发回滚

        results.append({
            "step": step["step_name"],
            "sql": step["sql"],
            "rows": result.rows,
            "affected_rows": result.affected_rows,
        })

        # 推入回滚栈（仅写操作）
        if step.get("rollback_sql"):
            rollback_stack.append({
                "step": step["step_name"],
                "rollback_sql": step["rollback_sql"],
            })

        return {
            "operation_results": results,
            "rollback_stack": rollback_stack,
            "current_op_index": idx + 1,
        }
    except Exception as e:
        return {"error": str(e), "current_op_index": idx}
```

### 6.6 `rollback_operations`（非 LLM，逆序回滚）

```python
def rollback_operations(state: AgentState, executor: BaseExecutor) -> dict:
    rollback_stack = list(state.get("rollback_stack", []))
    error = state.get("error", "未知错误")
    rollback_results = []

    # 逆序执行回滚 SQL
    for entry in reversed(rollback_stack):
        try:
            result = executor.execute(entry["rollback_sql"], approved=True)
            rollback_results.append({
                "step": entry["step"],
                "status": "rolled_back",
                "error": result.error,
            })
        except Exception as e:
            rollback_results.append({
                "step": entry["step"],
                "status": "rollback_failed",
                "error": str(e),
            })

    summary = f"操作在步骤中失败: {error}\n已回滚 {len(rollback_results)} 个步骤。"
    return {"response": summary, "operation_results": rollback_results}
```

---

## 7. 用户交互示例

### DECIDE 路径

```
retail> 哪些订单应该被取消？

🤔 意图: DECIDE
📋 适用规则: Order.decisionRule = "IF status='overdue' AND days_since_due > 30 THEN recommend_cancel"
🔍 查询受影响数据...

📊 决策推荐:
  建议取消 2 笔订单（满足规则）：
    - 订单 #101 (Zhang San, 45天逾期, ¥580.00)
    - 订单 #102 (Li Si, 33天逾期, ¥120.00)
  排除 1 笔（不满足条件）：
    - 订单 #103 (Wang Wu, 28天逾期 — 未满30天阈值)

  置信度: 0.95
  需要审批: 是

执行此决策？[y/n/详情]: y

⚙️  执行操作计划:
  ✅ 步骤1: verify_overdue_orders          → 找到 2 笔
  ✅ 步骤2: notify_affected_customers      → 已记录通知（模拟）
  ✅ 步骤3: update_order_status_to_cancelled → 更新 2 笔
  ✅ 步骤4: restore_product_stock          → 恢复库存 3 件

💬 已按业务规则取消 2 笔逾期订单，1 笔因未满30天阈值保留。
```

### DECIDE + 用户覆盖

```
retail> 跳过发通知这一步，取消所有逾期订单

🤔 意图: DECIDE
📋 检测到规则覆盖:
  ⚠️  跳过步骤: notify_affected_customers（用户指定）

⚙️  执行操作计划:
  ✅ 步骤1: verify_overdue_orders          → 找到 3 笔
  ⏭️  步骤2: notify_affected_customers     → 已跳过（用户覆盖）
  ✅ 步骤3: update_order_status_to_cancelled → 更新 3 笔
  ✅ 步骤4: restore_product_stock          → 恢复库存

💬 已取消 3 笔逾期订单（通知步骤已按您的要求跳过）。
```

### OPERATE + 回滚触发

```
retail> 处理月末库存对账

🤔 意图: OPERATE
📋 规则: Product.operationSteps = "1:audit_low_stock,2:generate_reorder,3:update_reorder_flags"
需要确认: 是

操作计划预览:
  步骤1: audit_low_stock            - SELECT 查询低库存商品
  步骤2: generate_reorder           - INSERT 生成补货记录
  步骤3: update_reorder_flags       - UPDATE products.needs_reorder = true

执行操作？[y/n]: y

⚙️  执行中:
  ✅ 步骤1: audit_low_stock → 发现 8 件低库存
  ✅ 步骤2: generate_reorder → 插入 8 条补货记录
  ❌ 步骤3: update_reorder_flags → 错误: 字段 'needs_reorder' 不存在

↩️  触发回滚:
  ✅ 步骤2: generate_reorder → 已回滚（删除刚插入的 8 条记录）

💬 操作失败（字段缺失）。已安全回滚，数据库未受影响。
```

---

## 8. 模式 D 覆盖优先级

```
优先级（高 → 低）：
┌────────────────────────────────────────────────────────────────┐
│ 1. 安全硬约束（rollbackable=false, ADMIN 级别 SQL）- 不可覆盖    │
│ 2. 用户 Prompt 覆盖（当 overridable=true 时生效）               │
│ 3. RDF 注解默认规则（aip:decisionRule, aip:requiresApproval）   │
│ 4. config.yaml 全局权限（read/write/delete/admin）              │
└────────────────────────────────────────────────────────────────┘
```

**不可覆盖的约束（overridable=false）：**
- `rollbackable=false` 的操作步骤：一旦执行不可撤销，始终强制审批
- `severity=critical` 的实体操作：即使用户说"跳过审批"也需要确认
- ADMIN 级别 SQL（DROP/CREATE/ALTER）：`config.yaml` 中 `admin: deny` 是系统底线

---

## 9. 需要修改的文件

| 文件 | 修改内容 |
|------|---------|
| `ontologies/*.rdf` | 添加 `aip:` namespace 和各类的注解属性 |
| `src/ontology/parser.py` | 扩展 `OntologySchema` 和解析逻辑，提取 `aip:*` 注解 |
| `src/agent/state.py` | 添加 `rdf_rules`, `user_overrides`, `decision`, `operation_plan` 等字段 |
| `src/agent/nodes.py` | 新增 6 个节点函数（见第 6 节） |
| `src/agent/graph.py` | 新增 DECIDE/OPERATE 路径和条件边 |
| `src/cli/app.py` | 处理 `present_decision` 的用户交互（y/n/详情） |
| `src/web/app.py` | 展示决策推荐卡片和操作步骤进度 |
| `tests/test_nodes.py` | 新增 DECIDE/OPERATE 节点的单元测试 |
| `tests/test_integration.py` | 新增 DECIDE/OPERATE 端到端测试 |

---

## 10. 实现顺序建议

```
阶段 1（基础设施）
  → 扩展 retail.rdf 添加 aip: 注解（1 个文件先试点）
  → 扩展 parser.py 解析 aip: 注解
  → 扩展 AgentState 添加新字段

阶段 2（核心节点）
  → extract_rdf_rules（纯解析，无 LLM，先写测试）
  → extract_user_overrides（Gemini CLI 先探索 Prompt，再写节点）
  → apply_decision（Gemini CLI 先探索 Prompt，再写节点）

阶段 3（操作执行）
  → plan_operation（依赖阶段 2 的 decision 结果）
  → execute_operation_step（单步执行 + rollback_stack 构建）
  → rollback_operations（逆序回滚）

阶段 4（图集成）
  → 扩展 classify_intent 识别 DECIDE/OPERATE
  → 在 graph.py 中接入新路径
  → CLI/Web UI 适配交互

阶段 5（扩展其他域）
  → 为其他 5 个 RDF 文件补充 aip: 注解
```

---

## 11. 与现有架构的差异对比

| 维度 | 现有架构 | 模式 D 扩展 |
|------|---------|------------|
| 规则来源 | `config.yaml`（全局、静态） | RDF 注解（实体级、领域感知）|
| 用户覆盖 | 不支持 | Prompt 自然语言 → LLM 提取 → 合并执行 |
| 操作粒度 | 单条 SQL | 多步编排 + 回滚栈 |
| 决策透明度 | 无（直接执行） | 推理链可视化（reasoning + confidence）|
| 错误恢复 | 重试一次 SQL | 多步原子性 + 自动回滚 |
| 注解扩展性 | N/A | 新增注解属性 = 新增行为，无需改代码 |
