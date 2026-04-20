# ontology-aip-agent — Claude Code Rules

本体（Ontology）驱动的自然语言数据查询 Agent。  
用户自然语言提问 → LLM 意图分类 → 生成 SQL / 决策 / 操作计划 → 执行并返回自然语言答案。

**运行时栈**：Python 3.12 · LangGraph · RDFLib · SQLite（可扩展至 StarRocks/Iceberg）

---

## 目录结构

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
│   ├── ollama.py / openai_compat.py / vertex.py
├── database/
│   ├── executor.py         # BaseExecutor ABC + SQLiteExecutor
│   └── connectors.py / schema.py / mock_data.py
└── ontology/
    ├── provider.py         # OntologyProvider ABC（统一入口）
    ├── rdf_provider.py     # RDFOntologyProvider 实现
    ├── context.py / parser.py
tests/                      # pytest，路径镜像 src/
config.yaml                 # 默认配置（config.local.yaml 本地覆盖，已 gitignore）
```

**结构规则**：
- 新业务逻辑节点 → `src/agent/nodes/` 对应文件 + `__init__.py` re-export
- 每个文件原则上不超过 300 行；超出时按职责拆分子模块
- 禁止在 `src/` 内创建 `utils.py` / `helpers.py` 等无意义命名的平级文件

---

## Python 编码规范

**类型注解**（Python 3.10+ 风格）：
- 所有公有函数参数和返回值必须有类型注解
- 使用 `X | Y` 代替 `Optional[X]`；使用内置 `list[...]` `dict[...]`，不引入 `typing.List`

**文档字符串**（Google Style）：所有公有类和非平凡函数必须有 docstring。

**日志与异常**：
- 禁止裸 `print()` 调试，一律用 `logger = logging.getLogger(__name__)`
- 禁止裸 `except: pass`；捕获具体异常（`PermissionDenied`、`json.JSONDecodeError`）优先于宽泛 `Exception`

**导入顺序**（isort 标准）：标准库 → 第三方 → `src.*`。使用绝对路径导入，禁止相对导入（`from .xxx`）。

---

## Agent / LangGraph 规范

**节点签名**：
```python
def my_node(state: AgentState, dep: SomeDependency) -> dict:
    return {"key": value}   # 只返回变更的键，不直接修改 state
```

**依赖注入**：`LLMClient`、`BaseExecutor`、`OntologyProvider` 通过 `graph.py` 的 lambda 闭包注入，节点函数本身不做依赖解析。

**AgentState**（`src/agent/state.py`）：全局状态合同；新增字段时同步更新 docstring，说明用途和生命周期。字段类型用 `total=False`，节点函数通过 `state.get("key", default)` 安全读取。

**新增意图路径**：① `state.py` 添加字段 → ② `nodes/` 实现节点 → ③ `graph.py` 添加节点/边/路由 → ④ `tests/` 编写 pytest 测试。

---

## SQL 工具

```python
from src.agent.nodes._sql_utils import clean_sql, detect_permission_level
```

- 生成 SQL 后必须用 `clean_sql()` 去除 Markdown 代码围栏
- 权限级别判断统一使用 `detect_permission_level(sql)`，不在节点函数里重复实现

---

## LLM 接口

- `LLMClient` 是 Protocol，不是 ABC；实现 duck-typing 即可，**不要**继承它
- 节点函数通过 `llm.chat(messages, system_prompt=..., temperature=0.0)` 调用
- 禁止在节点函数内直接实例化任何 LLM 类
- 默认 `temperature=0.0`；有特殊需求时显式传参并注释原因

---

## 数据库执行器

- 新后端继承 `BaseExecutor`（`src/database/executor.py`），实现 `execute()` 和 `dialect` 属性
- `executor.execute(sql, approved=bool)` 是**唯一**与数据库交互的入口
- 写操作（`permission_level == "confirm"`）必须经用户确认或显式 `approved=True`

---

## Ontology Provider

- `OntologyProvider`（`src/ontology/provider.py`）是加载接口，`build_graph` 接收它而非原始字符串
- `RDFOntologyProvider` 读取 `aip:physicalTable` / `aip:queryEngine` / `aip:partitionKeys` 注解，将物理表名渲染进 LLM schema context
- 新后端实现 `OntologyProvider.load() -> OntologyContext` 即可

---

## 测试规范

```python
class FakeLLM:
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

- 测试文件放 `tests/`，命名 `test_<module>.py`，镜像 `src/` 结构
- 单元测试使用 `FakeLLM` / `FakeExecutor` / `MockOntologyProvider`，不调用真实 LLM 或数据库
- 运行：`source .venv/bin/activate && pytest tests/ -v`

---

## 配置规范

- `config.yaml` 默认配置；`config.local.yaml` 本地覆盖（已 gitignore）
- 禁止将 API Key、密码等敏感信息提交到仓库
- 使用 `pyproject.toml` 管理依赖，禁止使用 `requirements.txt`
- 安装：`pip install -e ".[dev]"`

---

## Git 提交规范（Conventional Commits）

```
<type>(<scope>): <简短描述>

[可选正文]

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
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
