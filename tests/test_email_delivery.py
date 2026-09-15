"""邮件投递，以及「一个租户可以有多个平台的投递目标」。

加邮件的动机不是「多个渠道更好」，是别的渠道走不通：
Discord 中文用户基本不用；微信个人主体只能注册订阅号，而订阅号在
接口层面就禁止发模板消息——要主动推送必须企业主体 + 认证服务号。

这里守的几条都是会安静出错的：
- 升级时已有的 report_channel_id 必须搬进新表，否则之前配过的租户
  会静默失去投递目标，报告不再发出且不报错。
- 一个目标失败不能影响其他目标：报告已经生成好了，因为一个邮箱写错
  就整份丢掉是最糟的结果。
- 没配 SMTP 时要留下痕迹，不能假装发过了。
"""

import asyncio
import os
import tempfile

import pytest

os.environ.setdefault("COROS_RUNTIME_SETTINGS_PATH", tempfile.mktemp())

from src.integrations import email_sender  # noqa: E402
from src.runtime import delivery  # noqa: E402
from src.runtime.control_store import ControlStore  # noqa: E402
from src.runtime.tenant import TenantContext, tenant_scope  # noqa: E402

OWNER_CHANNEL = "1537316749622386718"


@pytest.fixture
def store(tmp_path, monkeypatch):
    control = ControlStore(tmp_path / "control.db")
    monkeypatch.setattr(delivery, "get_control_store", lambda: control)
    monkeypatch.setenv("DISCORD_RUNNING_CHANNEL_ID", OWNER_CHANNEL)
    control.ensure_default_tenant("Owner")
    return control


# ── 目标表 ────────────────────────────────────────────────────────────

def test_default_tenant_without_targets_still_uses_env(store):
    """升级前的安装一条配置都不用改。"""
    with tenant_scope(TenantContext("default")):
        assert delivery.report_targets() == [
            {"provider": "discord", "target": OWNER_CHANNEL}
        ]


def test_email_only_tenant_has_no_discord_target(store):
    tenant = store.create_tenant(name="只用邮件")
    store.add_delivery_target(tenant["id"], "email", "runner@example.com")
    with tenant_scope(TenantContext(str(tenant["id"]))):
        targets = delivery.report_targets()
        assert targets == [{"provider": "email", "target": "runner@example.com"}]
        assert delivery.email_targets() == ["runner@example.com"]


def test_one_tenant_can_have_several_targets(store):
    tenant = store.create_tenant(name="两个都要")
    store.add_delivery_target(tenant["id"], "discord", "123456789012345678")
    store.add_delivery_target(tenant["id"], "email", "runner@example.com")
    with tenant_scope(TenantContext(str(tenant["id"]))):
        providers = {t["provider"] for t in delivery.report_targets()}
    assert providers == {"discord", "email"}


def test_bad_email_is_refused(store):
    tenant = store.create_tenant(name="手滑")
    with pytest.raises(ValueError):
        store.add_delivery_target(tenant["id"], "email", "不是邮箱")


def test_bad_discord_channel_is_refused(store):
    tenant = store.create_tenant(name="手滑")
    with pytest.raises(ValueError):
        store.add_delivery_target(tenant["id"], "discord", "#我的频道")


def test_unknown_provider_is_refused(store):
    tenant = store.create_tenant(name="手滑")
    with pytest.raises(ValueError):
        store.add_delivery_target(tenant["id"], "telegram", "123")


def test_existing_channel_column_is_migrated(tmp_path, monkeypatch):
    """**升级路径。** 不搬的话之前配过频道的租户会静默失去投递目标。"""
    path = tmp_path / "control.db"
    first = ControlStore(path)
    tenant = first.create_tenant(name="老用户")
    first.update_tenant(tenant["id"], report_channel_id="123456789012345678")
    # 清掉新表，模拟「这条配置是在加 delivery_targets 之前写的」
    with first._connect() as db:
        db.execute("DELETE FROM delivery_targets")

    migrated = ControlStore(path)  # 重新初始化会跑迁移
    rows = migrated.list_delivery_targets(tenant["id"])
    assert [(r["provider"], r["target"]) for r in rows] == [
        ("discord", "123456789012345678")
    ]


# ── 扇出 ──────────────────────────────────────────────────────────────

def test_one_bad_address_does_not_stop_the_others(store, monkeypatch):
    """报告已经生成好了。一个地址写错就整份丢掉是最糟的结果。"""
    tenant = store.create_tenant(name="两个邮箱")
    store.add_delivery_target(tenant["id"], "email", "broken@example.com")
    store.add_delivery_target(tenant["id"], "email", "good@example.com")

    monkeypatch.setattr(email_sender, "configured", lambda: True)
    sent: list[str] = []

    def fake_send(to, subject, body):
        if to.startswith("broken"):
            raise RuntimeError("550 mailbox unavailable")
        sent.append(to)

    monkeypatch.setattr(email_sender, "send_report", fake_send)

    with tenant_scope(TenantContext(str(tenant["id"]))):
        delivered = asyncio.run(delivery.deliver_non_discord("主题", "正文"))

    assert sent == ["good@example.com"]
    assert len(delivered) == 1


def test_unconfigured_smtp_delivers_nothing(store, monkeypatch):
    """没配 SMTP 就是没发。不能返回「成功」骗自己。"""
    tenant = store.create_tenant(name="没配 SMTP")
    store.add_delivery_target(tenant["id"], "email", "runner@example.com")
    monkeypatch.setattr(email_sender, "configured", lambda: False)

    with tenant_scope(TenantContext(str(tenant["id"]))):
        assert asyncio.run(delivery.deliver_non_discord("主题", "正文")) == []


def test_discord_targets_are_not_emailed(store, monkeypatch):
    """Discord 那条路单独走（有论坛帖和路线图），别在这里重复发一遍。"""
    tenant = store.create_tenant(name="只有 Discord")
    store.add_delivery_target(tenant["id"], "discord", "123456789012345678")
    monkeypatch.setattr(email_sender, "configured", lambda: True)
    monkeypatch.setattr(
        email_sender, "send_report", lambda *a: pytest.fail("不该给 Discord 目标发邮件")
    )
    with tenant_scope(TenantContext(str(tenant["id"]))):
        assert asyncio.run(delivery.deliver_non_discord("主题", "正文")) == []


def test_log_masks_the_address():
    assert email_sender._masked("runner@example.com") == "ru***@example.com"
