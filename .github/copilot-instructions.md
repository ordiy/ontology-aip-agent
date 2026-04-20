# Copilot Instructions — ontology-aip-agent

> 本文件是项目的 **AI 编码规范**，所有 AI 助手（GitHub Copilot、Cursor 等）和人工开发者均应遵守。

---

## 项目概览

`ontology-aip-agent` 是一个本体（Ontology）驱动的自然语言数据查询 Agent。  
核心思路：用户以自然语言提问 → LLM 意图分类 → 生成 SQL / 决策 / 操作计划 → 执行并返回自然语言答案。

**运行时栈**：Python 3.12 · LangGraph · RDFLib · SQLite（可扩展至 StarRocks）

---

## 1. 目录结构规范

```
src/
├── agent/
│   ├── graph.py            # LangGraph 图构建 + 路由函数
│   ├── state.py            # AgentState TypedDict（唯一状态定义）
│   └── nodes/
│       ├── __init__.py     # 公共 re-export，保持导入向后兼容
│       ├── _sql_utils.py   # 共享 SQL 工具（clean_sql, detect_permission_level）
│       ├── read_write.py   # READ / WRITE 意图节点
│       ├── analyze.py      # ANALYZE 意图节点
│       └── decide_operate.py  # DECIDE / OPERATE 意图节点 (Pattern D)
├── llm/
│   ├── base.py             # LLMClient Protocol（接口定义，勿改签名）
│   ├── ollama.py
│   ├── openai_compat.py
│   └── vertex.py
├── database/
│   ├── executor.py         # BaseExecutor ABC + SQLiteExecutor
│   ├── connectors.py
│   ├── schema.py
│   └── mock_data.py
└── ontology/
    ├── provider.py         # OntologyProvider（统一入口）
    ├── context.py
    ├── parser.py
    └── rdf_provider.py
tests/                      # pytest 测试，路径镜像 src/
config.yaml                 # 默认配置
config.local.yaml           # 本地覆盖配置（gitignore）
```

**规则**：
- 新增业务逻辑节点 → 放入 `src/agent/nodes/` 对应文件，并在 `__init__.py` 里 re-export。
- 每个文件 **原则上不超过 300 行**；超出时按职责拆分子模块。
- 禁止在 `src/` 内创建 `utils.py` 或 `helpers.py` 等无意义命名的平级文件；改用按领域命名的子包。

---

## 2. Python 编码规范

### 2.1 语言版本与类型注解

```python
# ✅ 正确 — Python 3.10+ 原生风格
from __future__ import annotations   # 每个文件顶部（允许前向引用）

def foo(items: list[str], config: dict | None = None) -> str: ...
```

- **必须**为所有公有函数的参数和返回值添加类型注解。
- 使用 `X | Y` 代替 `Optional[X]` / `Union[X, Y]`。
- 使用内置泛型 `list[...]` `dict[...]` `tuple[...]`，不引入 `typing.List` 等。

### 2.2 文档字符串（Google Style）

```python
def classify_intent(state: AgentState, llm: LLMClient) -> dict:
    """一句话说明函数作用。

    更详细的描述（可选）。

    Args:
        state: 当前 AgentState，必须含 user_query 和 ontology_context。
        llm: LLM 客户端，用于意图分类。

    Returns:
        含 ``intent`` 键的部分状态更新字典。

    Raises:
        ValueError: 当 state 缺少必要键时。
    """
```

- 所有 **公有类** 和 **非平凡函数** 必须有 Google Style docstring。
- 私有/单行辅助函数可以用单行注释代替。

### 2.3 日志与异常

```python
import logging
logger = logging.getLogger(__name__)   # 每个模块顶层声明

# ✅ 具体异常类型 + 日志
try:
    result = executor.execute(sql, approved=True)
except PermissionDenied as exc:
    logger.warning("SQL permission denied: %s", exc)
    return {"error": str(exc)}
except Exception as exc:               # 兜底时加 noqa 注释
    logger.error("Unexpected error in execute_sql_node: %s", exc)
    raise
```

- **禁止** 使用裸 `print()` 输出调试信息，一律改用 `logger`。
- **禁止** 裸 `except: pass`；最低限度 `except Exception as exc: logger.warning(...)`.
- 捕获具体异常（`PermissionDenied`、`json.JSONDecodeError`、`sqlite3.Error`）优先于宽泛 `Exception`。

### 2.4 导入顺序（isort 标准）

```python
# 1. 标准库
from __future__ import annotations
import json
import logging
import re

# 2. 第三方
from langgraph.graph import END, StateGraph

# 3. 本项目（src 路径）
from src.agent.state import AgentState
from src.llm.base import LLMClient
```

- 绝对路径导入（`from src.xxx`），禁止相对导入（`from .xxx`）。
- `import json` 等标准库模块 **必须在文件顶部**，禁止在函数内部 `import`。

---

## 3. Agent / LangGraph 规范

### 3.1 节点函数签名

```python
# 节点函数固定签名：接收 state，返回 dict（部分状态更新）
def my_node(state: AgentState, dep: SomeDependency) -> dict:
    ...
    return {"key": value}   # 只返回变更的键
```

- 所有 LangGraph 节点函数返回 `dict`（部分状态更新），**不要**直接修改 state。
- 依赖（`LLMClient`、`BaseExecutor`）通过 `graph.py` 里的 lambda 闭包注入，节点函数本身不做依赖解析。

### 3.2 AgentState 变更

- `AgentState`（`src/agent/state.py`）是**全局状态合同**；新增字段时要同时更新 docstring 注释，说明字段用途和生命周期。
- 字段类型使用 `total=False`（所有字段可选），节点函数通过 `state.get("key", default)` 安全读取。

### 3.3 新增意图路径

新增一种意图（如 `REPORT`）时，按顺序完成：
1. `state.py` — 添加新字段（如需）。
2. `nodes/` — 在合适的子模块（或新建子模块）实现节点函数，并在 `__init__.py` re-export。
3. `graph.py` — 添加节点、边、路由分支。
4. `tests/` — 为新节点编写 pytest 测试。

---

## 4. SQL 工具规范

- 所有生成 SQL 后必须用 `clean_sql()` 去除 Markdown 代码围栏（`` ```sql `` 等）。
- 权限级别判断统一使用 `detect_permission_level(sql)`，不要在节点函数里重复实现。
- 这两个函数定义在 `src/agent/nodes/_sql_utils.py`，其他模块直接导入。

```python
from src.agent.nodes._sql_utils import clean_sql, detect_permission_level
```

---

## 5. LLM 接口规范

- `LLMClient` 是 `Protocol`，不是 ABC；所有 LLM 实现 duck-typing 即可，**不要**继承它。
- 节点函数通过 `llm.chat(messages, system_prompt=..., temperature=0.0)` 调用，**禁止**在节点函数内直接实例化任何 LLM 类。
- 所有 LLM 调用默认 `temperature=0.0`（确定性输出），有特殊需求时显式传参并注释原因。

---

## 6. 数据库执行器规范

- 新数据库后端需继承 `BaseExecutor`（`src/database/executor.py`）并实现 `execute()` 和 `dialect` 属性。
- `executor.execute(sql, approved=bool)` 是**唯一**与数据库交互的入口，节点函数不直接操作连接。
- 写操作（`permission_level == "confirm"`）必须经过用户确认或显式 `approved=True`。

---

## 7. 测试规范

```python
# tests/test_nodes.py 风格
class FakeLLM:
    """按调用顺序返回预设响应的 Fake LLM（测试专用）。"""
    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self._call_index = 0

    def chat(self, messages: list[dict], system_prompt: str | None = None,
             temperature: float = 0.0) -> str:
        resp = self._responses[self._call_index]
        self._call_index += 1
        return resp

    def get_model_name(self) -> str:
        return "fake-model"
```

- 测试文件放 `tests/`，命名 `test_<module>.py`，镜像 `src/` 结构。
- 使用 `FakeLLM` / `FakeExecutor` 做单元测试，**不要**在单元测试中调用真实 LLM 或数据库。
- 运行测试：`source .venv/bin/activate && pytest tests/ -v`

---

## 8. 配置规范

- 配置文件：`config.yaml`（默认）/ `config.local.yaml`（本地覆盖，已 gitignore）。
- **禁止**将 API Key、密码等敏感信息提交到代码仓库；一律放 `config.local.yaml` 或环境变量。
- 新增配置项时同步更新 `config.yaml`（给出注释说明的默认值）。

---

## 9. Git 提交规范（Conventional Commits）

```
<type>(<scope>): <简短描述>

[可选正文]

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
```

| type | 用途 |
|------|------|
| `feat` | 新功能 |
| `fix` | 缺陷修复 |
| `refactor` | 重构（不改变行为） |
| `test` | 添加/修改测试 |
| `docs` | 文档变更 |
| `chore` | 构建、依赖、CI 等维护 |

scope 示例：`agent`、`nodes`、`llm`、`database`、`ontology`、`cli`、`web`

---

## 10. 依赖管理

- 依赖声明在 `pyproject.toml` 的 `[project.dependencies]` 中，开发依赖放 `[project.optional-dependencies].dev`。
- **禁止**使用 `requirements.txt` 管理依赖（项目使用 pyproject.toml）。
- 安装：`pip install -e ".[dev]"`

---

## 快速参考

```bash
# 激活环境
source .venv/bin/activate

# 运行全部测试
pytest tests/ -v

# 运行特定测试
pytest tests/test_nodes.py -v

# 安装依赖
pip install -e ".[dev]"
```
