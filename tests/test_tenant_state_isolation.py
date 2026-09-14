import json

from src.runtime import conversation, memory
from src.runtime.control_store import ControlStore
from src.runtime.tenant import TenantContext, tenant_scope


def test_memory_is_isolated_by_tenant(tmp_path, monkeypatch):
    monkeypatch.setenv("TENANT_DATA_ROOT", str(tmp_path / "tenants"))

    with tenant_scope(TenantContext("tenant-a")):
        memory.update_agent_memory("coros-report", {"goal": "sub-4"})

    with tenant_scope(TenantContext("tenant-b")):
        assert memory.get_agent_memory("coros-report") == {}
        memory.update_agent_memory("coros-report", {"goal": "sub-5"})

    with tenant_scope(TenantContext("tenant-a")):
        assert memory.get_agent_memory("coros-report")["goal"] == "sub-4"


def test_conversation_memory_and_files_are_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TENANT_DATA_ROOT", str(tmp_path / "tenants"))
    monkeypatch.setenv("CONVERSATION_PERSIST_ENABLED", "true")
    conversation._sessions.clear()

    with tenant_scope(TenantContext("tenant-a")):
        conversation.set_context_value("same-channel", "running-coach", "race", "LA")

    with tenant_scope(TenantContext("tenant-b")):
        assert conversation.get_context_value("same-channel", "running-coach", "race") is None
        conversation.set_context_value("same-channel", "running-coach", "race", "Boston")

    with tenant_scope(TenantContext("tenant-a")):
        assert conversation.get_context_value("same-channel", "running-coach", "race") == "LA"

    journals = list(tmp_path.glob("tenants/*/conversations/**/*.jsonl"))
    assert len(journals) == 2


def test_usage_events_are_grouped_by_tenant(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    first = store.create_tenant("Runner A")
    second = store.create_tenant("Runner B")

    store.record_usage(first["id"], "trace-a", "model-x", 100, 20)
    store.record_usage(second["id"], "trace-b", "model-x", 300, 50)

    overall = store.usage_summary(days=30)
    first_only = store.usage_summary(days=30, tenant_id=first["id"])

    assert overall["calls"] == 2
    assert overall["total_tokens"] == 470
    assert first_only == {
        "calls": 1,
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
    }


def test_default_tenant_keeps_legacy_memory_path(tmp_path, monkeypatch):
    legacy = tmp_path / "memory.json"
    legacy.write_text(json.dumps({"global": {"name": "Noah"}, "agents": {}, "caches": {}}))
    monkeypatch.setattr(memory, "MEMORY_PATH", legacy)

    with tenant_scope(TenantContext("default")):
        assert memory.get_global_memory()["name"] == "Noah"
