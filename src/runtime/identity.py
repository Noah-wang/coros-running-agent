"""Resolve Discord, Feishu, or future channel identities to one tenant."""

from __future__ import annotations

import os
from datetime import UTC, datetime

from src.runtime.control_store import ControlStore, get_control_store
from src.runtime.tenant import TenantContext


def multi_tenant_enabled() -> bool:
    return os.getenv("MULTI_TENANT_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def tenant_is_available(tenant: dict[str, object]) -> bool:
    if tenant.get("id") == "default":
        return True
    if tenant.get("status") != "active":
        return False
    if tenant.get("subscription_status") not in {"trial", "active"}:
        return False
    expires = tenant.get("subscription_expires_at")
    if isinstance(expires, str) and expires:
        try:
            parsed = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            if parsed.astimezone(UTC) <= datetime.now(UTC):
                return False
        except ValueError:
            return False
    return True


def resolve_external_tenant(
    provider: str,
    external_user_id: str,
    *,
    workspace_id: str = "",
    surface: str = "",
    store: ControlStore | None = None,
) -> TenantContext | None:
    registry = store or get_control_store()
    if not multi_tenant_enabled():
        registry.ensure_default_tenant(os.getenv("AGENT_OWNER_NAME", "Owner"))
        return TenantContext("default", external_user_id, surface or provider)

    tenant = registry.resolve_identity(provider, external_user_id, workspace_id)
    if tenant is None and workspace_id:
        tenant = registry.resolve_identity(provider, external_user_id, "")
    if tenant is None or not tenant_is_available(tenant):
        return None
    return TenantContext(str(tenant["id"]), external_user_id, surface or provider)
