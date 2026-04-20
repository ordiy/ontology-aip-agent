import pytest
from src.agent.state import AgentState
from src.agent.nodes import (
    classify_intent,
    extract_user_overrides,
    apply_decision,
    plan_operation,
)

class FakeLLM:
    def __init__(self, responses: list[str]):
        self._responses = responses
        self._call_index = 0

    def chat(self, messages, system_prompt=None, temperature=0.0):
        resp = self._responses[self._call_index]
        self._call_index += 1
        return resp

def test_classify_intent_decide():
    llm = FakeLLM(["DECIDE"])
    state = {"user_query": "哪些订单该取消？", "ontology_context": "..."}
    result = classify_intent(state, llm)
    assert result["intent"] == "DECIDE"

def test_classify_intent_operate():
    llm = FakeLLM(["OPERATE"])
    state = {"user_query": "处理所有逾期订单", "ontology_context": "..."}
    result = classify_intent(state, llm)
    assert result["intent"] == "OPERATE"

def test_extract_user_overrides():
    llm = FakeLLM(['{"skip_approval": true, "skip_steps": ["notify"], "override_rules": [], "reason": "urgent"}'])
    state = {"user_query": "这批紧急，跳过通知直接执行", "rdf_rules": {}}
    result = extract_user_overrides(state, llm)
    assert result["user_overrides"]["skip_approval"] is True
    assert "notify" in result["user_overrides"]["skip_steps"]
    assert result["user_overrides"]["reason"] == "urgent"

def test_apply_decision():
    llm = FakeLLM(['{"decision": "cancel", "affected_entities": [101, 102], "excluded_entities": [], "reasoning": "overdue > 30d", "requires_approval": true, "confidence": 1.0}'])
    state = {
        "user_query": "...",
        "rdf_rules": {},
        "user_overrides": {"skip_approval": False},
        "query_result": [{"id": 101, "days": 40}, {"id": 102, "days": 35}]
    }
    result = apply_decision(state, llm)
    assert result["decision"]["decision"] == "cancel"
    assert result["decision"]["affected_entities"] == [101, 102]
    assert result["decision"]["requires_approval"] is True

def test_plan_operation():
    llm = FakeLLM(['[{"step_name": "update", "description": "desc", "sql": "UPDATE...", "skipped": false, "skip_reason": "", "rollback_sql": "ROLLBACK..."}]'])
    state = {
        "user_query": "cancel orders",
        "rdf_rules": {"Order": type('Rule', (), {'entity': 'Order', 'operation_steps': ['verify', 'update']})},
        "user_overrides": {},
        "decision": {"affected_entities": [101]},
        "ontology_context": "..."
    }
    result = plan_operation(state, llm)
    assert len(result["operation_plan"]) == 1
    assert result["operation_plan"][0]["step_name"] == "update"
    assert "rollback_sql" in result["operation_plan"][0]

def test_rollback_operations():
    from src.agent.nodes import rollback_operations
    from unittest.mock import MagicMock
    
    mock_executor = MagicMock()
    state = {
        "error": "Database lock",
        "rollback_stack": [
            {"step": "step1", "rollback_sql": "DELETE FROM t1 WHERE id=1"},
            {"step": "step2", "rollback_sql": "UPDATE t2 SET s=0 WHERE id=2"}
        ],
        "current_op_index": 2
    }
    
    result = rollback_operations(state, mock_executor)
    
    # Check that executor was called for each rollback in reverse
    assert mock_executor.execute.call_count == 2
    # Verify the order of calls: step2 then step1
    args_list = [call[0][0] for call in mock_executor.execute.call_args_list]
    assert args_list[0] == "UPDATE t2 SET s=0 WHERE id=2"
    assert args_list[1] == "DELETE FROM t1 WHERE id=1"
    
    assert "✅ Rolled back step1" in result["response"]
    assert "✅ Rolled back step2" in result["response"]
    assert "Database lock" in result["response"]
