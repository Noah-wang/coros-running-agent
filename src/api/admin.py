"""Administrator-facing control-plane helpers.

The browser receives operational state only. Secret values remain in server
environment variables and are never serialized into the response.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from src.integrations.coros_mcp import mcp_config_dir
from src.runtime.control_store import ControlStore, get_control_store
from src.runtime.identity import multi_tenant_enabled
from src.runtime.runtime_settings import automation_payload
from src.runtime.usage_store import summary as global_usage_summary


ALLOWED_IDENTITY_PROVIDERS = {"discord", "feishu"}


def _configured(*names: str) -> bool:
    return any(bool(os.getenv(name, "").strip()) for name in names)


def _public_endpoint(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            parsed = urlparse(value)
            return parsed.hostname or "custom endpoint"
    return "default endpoint"


def _coros_profile_root(tenant_id: str) -> Path:
    profile = mcp_config_dir(tenant_id)
    if profile is not None:
        return profile
    return Path.home() / ".mcp-auth"


def _coros_authorized(tenant_id: str) -> bool:
    root = _coros_profile_root(tenant_id)
    if not root.exists():
        return False
    try:
        return any(path.is_file() for path in root.rglob("*"))
    except OSError:
        return False


def _integration_payload() -> list[dict[str, Any]]:
    llm_configured = _configured("LLM_API_KEY", "DEEPSEEK_API_KEY")
    embedding_configured = _configured("EMBEDDING_API_KEY") or llm_configured
    automations = automation_payload()
    search_providers = [
        name
        for name, present in (
            ("Tavily", _configured("TAVILY_API_KEY")),
            ("Brave", _configured("BRAVE_SEARCH_API_KEY")),
        )
        if present
    ]
    return [
        {
            "id": "llm",
            "name": "LLM",
            "configured": llm_configured,
            "detail": os.getenv("LLM_MODEL")
            or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            "endpoint": _public_endpoint("LLM_BASE_URL", "DEEPSEEK_BASE_URL"),
        },
        {
            "id": "embedding",
            "name": "Embedding",
            "configured": embedding_configured,
            "detail": os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
            "endpoint": _public_endpoint("EMBEDDING_BASE_URL", "LLM_BASE_URL"),
        },
        {
            "id": "discord",
            "name": "Discord",
            "configured": _configured("DISCORD_BOT_TOKEN"),
            "detail": "Bot gateway",
            "endpoint": "discord.com",
        },
        {
            "id": "coros",
            "name": "COROS MCP",
            "configured": _coros_authorized("default"),
            "detail": "Owner OAuth profile",
            "endpoint": _public_endpoint("COROS_MCP_URL") or "mcpus.coros.com",
        },
        {
            "id": "feishu",
            "name": "Feishu",
            "configured": _configured("FEISHU_APP_ID") and _configured("FEISHU_APP_SECRET"),
            "detail": "Event callback",
            "endpoint": "open.feishu.cn",
        },
        {
            "id": "search",
            "name": "Web search",
            "configured": bool(search_providers),
            "detail": ", ".join(search_providers) if search_providers else "No provider",
            "endpoint": "server managed",
        },
        {
            "id": "automations",
            "name": "Automations",
            "configured": any(automations.values()),
            "detail": (
                f"Workout {'on' if automations.get('auto_report') else 'off'} · "
                f"Sleep {'on' if automations.get('sleep_report') else 'off'}"
            ),
            "endpoint": "scheduler",
        },
    ]


def _audit_payload(store: ControlStore) -> list[dict[str, Any]]:
    events = store.list_audit_events(limit=30)
    for event in events:
        try:
            event["details"] = json.loads(str(event.pop("details_json", "{}")))
        except json.JSONDecodeError:
            event["details"] = {}
    return events


def admin_payload(store: ControlStore | None = None) -> dict[str, Any]:
    registry = store or get_control_store()
    registry.ensure_default_tenant(os.getenv("AGENT_OWNER_NAME", "Owner"))
    identities = registry.list_identities()
    identities_by_tenant: dict[str, list[dict[str, Any]]] = {}
    for identity in identities:
        identities_by_tenant.setdefault(str(identity["tenant_id"]), []).append(identity)

    tenants: list[dict[str, Any]] = []
    for tenant in registry.list_tenants():
        tenant_id = str(tenant["id"])
        tenants.append(
            {
                **tenant,
                "identities": identities_by_tenant.get(tenant_id, []),
                "usage_30d": registry.usage_summary(30, tenant_id),
                "coros_authorized": _coros_authorized(tenant_id),
            }
        )

    usage = registry.usage_summary(30)
    global_usage = global_usage_summary(30)
    global_calls = sum(int(item.get("calls", 0)) for item in global_usage["by_model"].values())
    global_tokens = sum(
        int(item.get("total_tokens", 0)) for item in global_usage["by_model"].values()
    )
    return {
        "mode": "multi-tenant" if multi_tenant_enabled() else "compatibility",
        "multi_tenant_enabled": multi_tenant_enabled(),
        "overview": {
            "tenants": len(tenants),
            "active_tenants": sum(tenant["status"] == "active" for tenant in tenants),
            "active_subscriptions": sum(
                tenant["subscription_status"] == "active" for tenant in tenants
            ),
            "trial_subscriptions": sum(
                tenant["subscription_status"] == "trial" for tenant in tenants
            ),
            **usage,
            "calls": global_calls,
            "total_tokens": global_tokens,
        },
        "tenants": tenants,
        "integrations": _integration_payload(),
        "automations": automation_payload(),
        "global_usage_30d": global_usage,
        "audit": _audit_payload(registry),
    }


def apply_admin_action(data: dict[str, Any], store: ControlStore | None = None) -> dict[str, Any]:
    registry = store or get_control_store()
    registry.ensure_default_tenant(os.getenv("AGENT_OWNER_NAME", "Owner"))
    action = str(data.get("action", "")).strip()

    if action == "create_tenant":
        registry.create_tenant(
            str(data.get("name", "")),
            plan_code=str(data.get("plan_code", "trial")),
        )
    elif action == "update_tenant":
        tenant_id = str(data.get("tenant_id", "")).strip()
        changes = {
            key: data[key]
            for key in (
                "name",
                "status",
                "plan_code",
                "subscription_status",
                "subscription_expires_at",
                "report_channel_id",
                "report_forum_channel_id",
            )
            if key in data
        }
        if tenant_id == "default":
            # 默认租户的投递目标来自 .env（保持旧安装零改动），
            # 在后台改会造成「界面一个值、实际发到另一个频道」的错位。
            changes = {key: value for key, value in changes.items() if key == "name"}
        registry.update_tenant(tenant_id, **changes)
    elif action == "bind_identity":
        provider = str(data.get("provider", "")).strip().lower()
        if provider not in ALLOWED_IDENTITY_PROVIDERS:
            raise ValueError("provider must be discord or feishu")
        registry.bind_identity(
            str(data.get("tenant_id", "")).strip(),
            provider,
            str(data.get("external_user_id", "")),
            workspace_id=str(data.get("workspace_id", "")),
            label=str(data.get("label", "")),
        )
    elif action == "remove_identity":
        registry.remove_identity(str(data.get("identity_id", "")).strip())
    else:
        raise ValueError("unsupported admin action")

    return admin_payload(registry)
