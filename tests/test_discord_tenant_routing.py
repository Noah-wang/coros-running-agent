from src.runtime.control_store import ControlStore
from src.runtime.identity import resolve_external_tenant


def test_known_discord_user_resolves_to_bound_tenant(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("MULTI_TENANT_ENABLED", "true")
    store = ControlStore(tmp_path / "control.db")
    tenant = store.create_tenant("Runner A")
    store.bind_identity(tenant["id"], "discord", "111", workspace_id="guild-1")

    context = resolve_external_tenant(
        "discord", "111", workspace_id="guild-1", surface="discord", store=store
    )

    assert context is not None
    assert context.tenant_id == tenant["id"]
    assert context.user_id == "111"


def test_unknown_user_is_rejected_when_multi_tenant_is_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTI_TENANT_ENABLED", "true")
    store = ControlStore(tmp_path / "control.db")

    assert (
        resolve_external_tenant(
            "discord", "999", workspace_id="guild-1", surface="discord", store=store
        )
        is None
    )


def test_global_identity_binding_works_across_discord_guilds(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTI_TENANT_ENABLED", "true")
    store = ControlStore(tmp_path / "control.db")
    tenant = store.create_tenant("Runner A")
    store.bind_identity(tenant["id"], "discord", "111")

    context = resolve_external_tenant(
        "discord", "111", workspace_id="another-guild", surface="discord", store=store
    )

    assert context is not None
    assert context.tenant_id == tenant["id"]


def test_legacy_mode_uses_default_tenant(tmp_path, monkeypatch):
    monkeypatch.setenv("MULTI_TENANT_ENABLED", "false")
    store = ControlStore(tmp_path / "control.db")

    context = resolve_external_tenant(
        "discord", "999", workspace_id="guild-1", surface="discord", store=store
    )

    assert context is not None
    assert context.tenant_id == "default"
