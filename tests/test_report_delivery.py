"""自动报告发到哪里去。

多租户下最容易出、也最难发现的一类错：定时任务不认人，
**所有租户的运动和睡眠数据都发进同一个频道**——也就是站长自己那个。
这类错不会报异常，只会让别人的心率出现在你的频道里。

所以这里的规则是不对称的，测试也照这个不对称来写。
"""

import os
import tempfile

import pytest

os.environ.setdefault("COROS_RUNTIME_SETTINGS_PATH", tempfile.mktemp())

from src.runtime import delivery  # noqa: E402
from src.runtime.control_store import ControlStore  # noqa: E402
from src.runtime.tenant import TenantContext, tenant_scope  # noqa: E402

OWNER_CHANNEL = "1537316749622386718"
OWNER_FORUM = "1544914627283124236"
TENANT_CHANNEL = "2222222222222222222"


@pytest.fixture
def store(tmp_path, monkeypatch):
    control = ControlStore(tmp_path / "control.db")
    monkeypatch.setattr(delivery, "get_control_store", lambda: control)
    monkeypatch.setenv("DISCORD_RUNNING_CHANNEL_ID", OWNER_CHANNEL)
    monkeypatch.setenv("DISCORD_REPORT_FORUM_CHANNEL_ID", OWNER_FORUM)
    control.ensure_default_tenant("Owner")
    return control


def test_default_tenant_still_reads_env(store):
    """升级前那个你不用改任何配置。"""
    with tenant_scope(TenantContext("default")):
        assert delivery.report_channel_id() == int(OWNER_CHANNEL)
        assert delivery.report_forum_channel_id() == int(OWNER_FORUM)


def test_other_tenant_never_falls_back_to_the_owner_channel(store):
    """这条是整个文件的重点：没配就不发，**绝不借用全局频道**。"""
    tenant = store.create_tenant(name="Somebody Else")
    with tenant_scope(TenantContext(str(tenant["id"]))):
        assert delivery.report_channel_id() is None
        assert delivery.report_forum_channel_id() is None


def test_other_tenant_uses_its_own_channel(store):
    tenant = store.create_tenant(name="Somebody Else")
    store.update_tenant(tenant["id"], report_channel_id=TENANT_CHANNEL)
    with tenant_scope(TenantContext(str(tenant["id"]))):
        assert delivery.report_channel_id() == int(TENANT_CHANNEL)


def test_unknown_tenant_gets_nothing(store):
    """控制库里查不到这个租户时也不能回退。"""
    with tenant_scope(TenantContext("ghost-tenant")):
        assert delivery.report_channel_id() is None


def test_channel_id_must_be_numeric(store):
    """粘进来一个频道名而不是频道号，要当场报错，不要存进去等报告发不出去。"""
    tenant = store.create_tenant(name="Somebody Else")
    with pytest.raises(ValueError):
        store.update_tenant(tenant["id"], report_channel_id="#我的跑步频道")


def test_clearing_channel_is_allowed(store):
    tenant = store.create_tenant(name="Somebody Else")
    store.update_tenant(tenant["id"], report_channel_id=TENANT_CHANNEL)
    updated = store.update_tenant(tenant["id"], report_channel_id="")
    assert updated["report_channel_id"] is None


# ── 定时任务替谁跑 ────────────────────────────────────────────────────

def test_single_tenant_mode_behaves_exactly_as_before(store, monkeypatch):
    monkeypatch.setenv("MULTI_TENANT_ENABLED", "false")
    contexts = delivery.scheduled_tenants()
    assert [c.tenant_id for c in contexts] == ["default"]


def test_multi_tenant_mode_covers_available_tenants(store, monkeypatch):
    monkeypatch.setenv("MULTI_TENANT_ENABLED", "true")
    active = store.create_tenant(name="Active")
    contexts = delivery.scheduled_tenants()
    ids = {c.tenant_id for c in contexts}
    assert "default" in ids
    assert str(active["id"]) in ids


def test_disabled_tenant_stops_receiving_reports(store, monkeypatch):
    """被停用的人不该还在收自动报告——那是只有收报告的人才知道的矛盾状态。"""
    monkeypatch.setenv("MULTI_TENANT_ENABLED", "true")
    paused = store.create_tenant(name="Paused")
    store.update_tenant(paused["id"], status="disabled")
    ids = {c.tenant_id for c in delivery.scheduled_tenants()}
    assert str(paused["id"]) not in ids


def test_expired_subscription_stops_receiving_reports(store, monkeypatch):
    monkeypatch.setenv("MULTI_TENANT_ENABLED", "true")
    lapsed = store.create_tenant(name="Lapsed")
    store.update_tenant(lapsed["id"], subscription_status="expired")
    ids = {c.tenant_id for c in delivery.scheduled_tenants()}
    assert str(lapsed["id"]) not in ids


def test_control_store_failure_falls_back_to_default_only(monkeypatch):
    """控制库挂了也要让站长自己的报告继续发，而不是整个定时任务哑掉。"""
    monkeypatch.setenv("MULTI_TENANT_ENABLED", "true")

    def boom():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(delivery, "get_control_store", boom)
    assert [c.tenant_id for c in delivery.scheduled_tenants()] == ["default"]
