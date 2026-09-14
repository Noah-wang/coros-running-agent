from datetime import UTC, datetime, timedelta

import pytest

from src.runtime.control_store import ControlStore
from src.runtime.tenant import TenantContext, current_tenant, tenant_scope


def test_tenant_scope_restores_previous_context():
    assert current_tenant().tenant_id == "default"

    with tenant_scope(TenantContext("tenant-a", "user-a", "discord")):
        assert current_tenant().tenant_id == "tenant-a"
        assert current_tenant().user_id == "user-a"

        with tenant_scope(TenantContext("tenant-b", "user-b", "feishu")):
            assert current_tenant().tenant_id == "tenant-b"

        assert current_tenant().tenant_id == "tenant-a"

    assert current_tenant().tenant_id == "default"


def test_control_store_creates_and_lists_tenants(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    store.ensure_default_tenant("Noah")
    created = store.create_tenant("Runner A", plan_code="founder")

    assert created["id"].startswith("tn_")
    assert created["status"] == "active"
    assert created["subscription_status"] == "trial"
    assert [item["name"] for item in store.list_tenants()] == ["Noah", "Runner A"]


def test_identity_binding_resolves_one_tenant(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    first = store.create_tenant("Runner A")
    second = store.create_tenant("Runner B")

    store.bind_identity(first["id"], "discord", "111", workspace_id="guild-1")

    assert store.resolve_identity("discord", "111", "guild-1")["id"] == first["id"]
    assert store.resolve_identity("discord", "111", "guild-2") is None

    with pytest.raises(ValueError, match="already bound"):
        store.bind_identity(second["id"], "discord", "111", workspace_id="guild-1")


def test_subscription_and_status_updates_are_audited(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    tenant = store.create_tenant("Runner A")
    expires_at = (datetime.now(UTC) + timedelta(days=31)).isoformat()

    updated = store.update_tenant(
        tenant["id"],
        status="paused",
        plan_code="pro",
        subscription_status="active",
        subscription_expires_at=expires_at,
    )

    assert updated["status"] == "paused"
    assert updated["plan_code"] == "pro"
    assert updated["subscription_expires_at"] == expires_at
    assert store.list_audit_events(limit=10)[0]["action"] == "tenant.updated"


def test_invalid_tenant_values_are_rejected(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    tenant = store.create_tenant("Runner A")

    with pytest.raises(ValueError, match="status"):
        store.update_tenant(tenant["id"], status="deleted-ish")
    with pytest.raises(ValueError, match="provider"):
        store.bind_identity(tenant["id"], "email!", "a@example.com")
