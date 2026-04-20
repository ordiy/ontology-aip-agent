# Langfuse 可观测性集成 Spec

**日期:** 2026-04-20
**状态:** Draft
**依赖:** `2026-04-20-ontology-provider-physical-mapping.md`（build_graph 接口）
**参考:** https://github.com/langfuse/langfuse

---

## 1. 背景与动机

当前 Agent 运行时完全黑盒：
- 每次 LLM 调用的 prompt / 输出 / token 消耗不可见
- LangGraph 节点执行耗时无法监控
- 无法回放失败请求或做 A/B 评估

Langfuse 提供 LLM 可观测性平台（traces、generations、scores），通过两个接入点覆盖整条链路：

1. **`langfuse.callback.CallbackHandler`** — LangGraph 原生支持，自动捕获每个节点的输入/输出/耗时
2. **`LangfuseTrackedLLMClient` 包装器** — 在每次 `llm.chat()` 调用上创建 Generation span，记录 model / input / output / latency

---

## 2. 集成架构

```
CLI / Web 请求
  │
  ├─ session_id = uuid4()  (每个 CLI 会话唯一)
  │
  ▼
ObservabilityClient.get_handler(session_id, trace_name)
  └─ 返回 langfuse.callback.CallbackHandler
       │
       ▼
agent.invoke(state, config={"callbacks": [handler]})
  │
  ├─ LangGraph Node: load_context    ──┐
  ├─ LangGraph Node: classify_intent ──┤ 自动追踪（CallbackHandler）
  ├─ LangGraph Node: generate_sql   ──┤ 每个节点 = 一个 Span
  └─ ...                             ──┘
        │
        └─ LLMClient.chat()  ── LangfuseTrackedLLMClient
                                  └─ generation span（model/input/output）
```

---

## 3. 新增文件与改动

| 文件 | 类型 | 内容 |
|------|------|------|
| `src/observability/__init__.py` | 新建 | re-export |
| `src/observability/langfuse_client.py` | 新建 | `ObservabilityClient` + `LangfuseTrackedLLMClient` |
| `config.yaml` | 修改 | 新增 `langfuse:` 配置节 |
| `pyproject.toml` | 修改 | 新增 `langfuse>=2.0.0` 依赖 |
| `src/cli/app.py` | 修改 | 初始化 `ObservabilityClient`，inject handler 到 `agent.invoke` |
| `tests/test_observability.py` | 新建 | 单元测试（mock Langfuse） |

---

## 4. `config.yaml` 新增配置

```yaml
langfuse:
  enabled: false              # 改为 true 开启追踪
  public_key: ""              # LANGFUSE_PUBLIC_KEY 或 config.local.yaml
  secret_key: ""              # LANGFUSE_SECRET_KEY 或 config.local.yaml
  host: https://cloud.langfuse.com   # 自托管时改为内网地址
  project: ontology-aip-agent
```

- `enabled: false` 为默认值，不影响现有行为
- Key 放 `config.local.yaml` 或环境变量 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`

---

## 5. `ObservabilityClient`（核心接口）

```python
# src/observability/langfuse_client.py

class ObservabilityClient:
    """管理 Langfuse 连接，提供 handler 和 LLM 包装器。"""

    def __init__(self, config: dict):
        """
        Args:
            config: 来自 config.yaml 的 langfuse 节，含 enabled/public_key/secret_key/host
        """

    def get_handler(
        self,
        session_id: str,
        trace_name: str = "agent-query",
        user_id: str | None = None,
        metadata: dict | None = None,
    ) -> Any:
        """返回 langfuse.callback.CallbackHandler；disabled 时返回 None。"""

    def wrap_llm(self, llm: LLMClient) -> LLMClient:
        """返回带 generation 追踪的包装 LLM；disabled 时原样返回。"""

    @property
    def enabled(self) -> bool: ...
```

---

## 6. `LangfuseTrackedLLMClient`（LLM 包装器）

```python
class LangfuseTrackedLLMClient:
    """包装任意 LLMClient，在每次 chat() 调用时向 Langfuse 发送 generation span。"""

    def chat(self, messages, system_prompt=None, temperature=0.0) -> str:
        generation = self._langfuse.generation(
            name="llm-chat",
            model=self._inner.get_model_name(),
            input={"messages": messages, "system_prompt": system_prompt},
        )
        start = time.time()
        try:
            output = self._inner.chat(messages, system_prompt, temperature)
            generation.end(
                output=output,
                usage={"input": _count_tokens(messages), "output": _count_tokens(output)},
            )
            return output
        except Exception as exc:
            generation.end(level="ERROR", status_message=str(exc))
            raise

    def get_model_name(self) -> str:
        return self._inner.get_model_name()
```

**token 计数**：用 `len(str(x).split()) * 1.3` 近似估算（无需 tiktoken），key 是有值可以在 Langfuse dashboard 展示。

---

## 7. `cli/app.py` 修改

```python
# 初始化（在 setup_agent 或 main 里，每个 CLI 会话一次）
obs = ObservabilityClient(config.get("langfuse", {}))
llm = obs.wrap_llm(llm)   # 包装 LLM（disabled 时 no-op）

# 每次查询时
session_id = str(uuid4())   # CLI 启动时生成一次
handler = obs.get_handler(
    session_id=session_id,
    trace_name=f"query:{intent_hint}",
    metadata={"domain": domain_name, "query": user_query},
)

config_dict = {"callbacks": [handler]} if handler else {}
result = agent.invoke(initial_state, config=config_dict)
```

---

## 8. Langfuse Dashboard 展示内容

| 层级 | 名称 | 内容 |
|------|------|------|
| Trace | `agent-query` | 一次完整查询，含 session_id / domain / user_query |
| Span | `classify_intent` | 节点输入 state / 输出 intent / 耗时 |
| Span | `generate_sql` | 节点输入 / 输出 SQL |
| Generation | `llm-chat` | model / prompt / response / token 估算 |
| Span | `execute_sql` | SQL 文本 / affected_rows / error |

---

## 9. 禁用时的行为（enabled: false）

- `get_handler()` 返回 `None`
- `wrap_llm()` 原样返回传入的 LLM
- `agent.invoke(state, config={})` ← 无 callbacks，行为与现在完全一致
- 无任何性能损耗，无需修改节点代码

---

## 10. 测试要求

```python
# tests/test_observability.py

def test_disabled_returns_none_handler():
    obs = ObservabilityClient({"enabled": False})
    assert obs.get_handler("session-1") is None

def test_disabled_wrap_llm_returns_original():
    obs = ObservabilityClient({"enabled": False})
    fake_llm = FakeLLM(["hello"])
    assert obs.wrap_llm(fake_llm) is fake_llm

def test_tracked_llm_forwards_chat():
    mock_langfuse = MagicMock()
    mock_gen = MagicMock()
    mock_langfuse.generation.return_value = mock_gen
    client = LangfuseTrackedLLMClient(FakeLLM(["response"]), mock_langfuse)
    result = client.chat([{"role": "user", "content": "hi"}])
    assert result == "response"
    mock_langfuse.generation.assert_called_once()
    mock_gen.end.assert_called_once()

def test_tracked_llm_records_error():
    # LLM 抛异常时，generation.end(level="ERROR") 被调用，异常继续传播
```

---

## 11. 实现顺序

```
阶段 1：依赖和配置
  → pyproject.toml 添加 langfuse>=2.0.0
  → config.yaml 添加 langfuse: 配置节

阶段 2：ObservabilityClient
  → src/observability/__init__.py
  → src/observability/langfuse_client.py
    (ObservabilityClient + LangfuseTrackedLLMClient)

阶段 3：CLI 接入
  → src/cli/app.py：init + wrap_llm + inject handler

阶段 4：测试
  → tests/test_observability.py
```
