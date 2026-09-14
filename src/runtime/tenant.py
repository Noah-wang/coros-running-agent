"""Request-scoped tenant identity.

The default tenant deliberately keeps the legacy single-user behavior. New
surfaces set an explicit context before entering the orchestrator, and the
context follows async calls through ``ContextVar``.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Iterator


_TENANT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: str
    user_id: str = ""
    surface: str = "system"

    def __post_init__(self) -> None:
        if not _TENANT_ID.fullmatch(self.tenant_id):
            raise ValueError("tenant_id must contain only letters, numbers, hyphens, or underscores")


def default_tenant_id() -> str:
    value = os.getenv("DEFAULT_TENANT_ID", "default").strip() or "default"
    if not _TENANT_ID.fullmatch(value):
        return "default"
    return value


_current: ContextVar[TenantContext | None] = ContextVar("tenant_context", default=None)


def current_tenant() -> TenantContext:
    return _current.get() or TenantContext(default_tenant_id())


def set_current_tenant(context: TenantContext) -> Token[TenantContext | None]:
    return _current.set(context)


def reset_current_tenant(token: Token[TenantContext | None]) -> None:
    _current.reset(token)


@contextmanager
def tenant_scope(context: TenantContext) -> Iterator[TenantContext]:
    token = set_current_tenant(context)
    try:
        yield context
    finally:
        reset_current_tenant(token)

