"""报告发到哪里去——按租户解析投递目标。

单租户时代这两个目标是环境变量里的两个频道号，全局共用一份。
多租户下这就成了一个隐患：定时任务跑起来不认人，
**所有租户的运动数据都会发进同一个频道**，也就是你自己的那个。

所以这里的规则不对称，而且是故意的：

- **默认租户**（也就是升级前那个你）继续读环境变量，旧安装不用改任何配置。
- **其他租户**只认控制库里给自己配的频道；没配就返回 None，**不回退到环境变量**。

回退看起来更「健壮」，实际是把别人的睡眠和心率发到你的频道里。
宁可不发——不发是能被发现的，发错地方不会。
"""

from __future__ import annotations

import os

from src.runtime.control_store import get_control_store
from src.runtime.identity import multi_tenant_enabled, tenant_is_available
from src.runtime.tenant import TenantContext, current_tenant, default_tenant_id


def _as_channel_id(value: object) -> int | None:
    text = str(value or "").strip()
    if not text.isdigit():
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _tenant_field(field: str) -> int | None:
    try:
        tenant = get_control_store().get_tenant(current_tenant().tenant_id)
    except Exception:
        # 控制库读不出来时不要猜。猜的结果就是发错地方。
        return None
    if not tenant:
        return None
    return _as_channel_id(tenant.get(field))


def report_channel_id() -> int | None:
    """自动运动/睡眠报告发到哪个频道。"""
    if current_tenant().tenant_id == default_tenant_id():
        return _as_channel_id(os.getenv("DISCORD_RUNNING_CHANNEL_ID"))
    return _tenant_field("report_channel_id")


def report_forum_channel_id() -> int | None:
    """报告发帖的论坛。没配就退化成在普通频道里发消息，不跨租户借用。"""
    if current_tenant().tenant_id == default_tenant_id():
        return _as_channel_id(os.getenv("DISCORD_REPORT_FORUM_CHANNEL_ID"))
    return _tenant_field("report_forum_channel_id")


def scheduled_tenants() -> list[TenantContext]:
    """定时任务这一轮要替谁跑。

    多租户没开时只有默认租户，行为和单租户时代**完全一样**——
    这是这个函数存在的主要理由：开关关着的时候不引入任何新行为。

    开了之后按 `tenant_is_available` 过滤，和 Discord 消息进来时用的是
    同一把尺子。不共用的话会出现「他被停用了却还在收自动报告」这种
    自相矛盾的状态，而且只有收到报告的人知道。
    """
    if not multi_tenant_enabled():
        return [TenantContext(default_tenant_id())]

    try:
        tenants = get_control_store().list_tenants()
    except Exception:
        # 控制库读不出来时退回默认租户，至少你自己的报告不受影响。
        return [TenantContext(default_tenant_id())]

    return [
        TenantContext(str(tenant["id"]))
        for tenant in tenants
        if tenant_is_available(tenant)
    ]


# ── 多平台投递 ────────────────────────────────────────────────────────

def report_targets(tenant_id: str | None = None) -> list[dict[str, str]]:
    """这个租户配了哪些投递目标。

    默认租户没在控制库里配过的话，退回 .env 里那个 Discord 频道——
    升级前的安装什么都不用改。**其他租户不退回**，理由见 report_channel_id。
    """
    tenant = tenant_id or current_tenant().tenant_id
    try:
        rows = get_control_store().list_delivery_targets(tenant)
    except Exception:
        rows = []

    targets = [
        {"provider": str(r["provider"]), "target": str(r["target"])}
        for r in rows
        if r.get("provider") and r.get("target")
    ]
    if targets:
        return targets

    channel_id = report_channel_id()
    return [{"provider": "discord", "target": str(channel_id)}] if channel_id else []


def email_targets(tenant_id: str | None = None) -> list[str]:
    return [t["target"] for t in report_targets(tenant_id) if t["provider"] == "email"]


async def deliver_non_discord(subject: str, body: str, tenant_id: str | None = None) -> list[str]:
    """把报告发到 Discord 之外的目标。返回成功送达的目标描述。

    Discord 那条路单独走，因为它有论坛发帖和路线图附件这些邮件没有的东西。
    这里只管扇出文本。

    **一个目标失败不能影响其他目标**，也不能让整个报告任务崩掉——
    报告已经生成好了，因为一个邮箱地址写错就全丢掉是最糟的结果。
    """
    import asyncio

    from src.integrations.email_sender import configured as email_configured
    from src.integrations.email_sender import send_report
    from src.runtime.trace import log_event

    delivered: list[str] = []
    for address in email_targets(tenant_id):
        if not email_configured():
            log_event("email_skipped", reason="smtp_not_configured")
            break
        try:
            await asyncio.to_thread(send_report, address, subject, body)
            delivered.append(f"email:{address.split('@')[-1]}")
        except Exception as exc:
            log_event("email_failed", error=str(exc)[:200])
    return delivered
