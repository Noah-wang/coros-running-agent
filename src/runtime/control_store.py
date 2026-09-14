"""Small control-plane database for tenants, identities, and subscriptions.

This database contains operational metadata only. COROS tokens, prompts,
workouts, and conversations do not belong here.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.runtime.paths import DATA_DIR


_SAFE_PROVIDER = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_TENANT_STATUSES = {"active", "paused", "disabled"}
_SUBSCRIPTION_STATUSES = {"trial", "active", "past_due", "expired", "cancelled"}
_UPDATABLE_TENANT_FIELDS = {
    "name",
    "status",
    "plan_code",
    "subscription_status",
    "subscription_expires_at",
    # 每个租户自己的报告投递目标。不配的话自动报告无处可去——
    # **绝不能回退到全局那个频道**，那等于把别人的运动数据发进你的频道。
    "report_channel_id",
    "report_forum_channel_id",
}

# 只允许纯数字的 Discord 频道号，或空（表示未配置）。
_CHANNEL_FIELDS = {"report_channel_id", "report_forum_channel_id"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _public_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


class ControlStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tenants (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    plan_code TEXT NOT NULL DEFAULT 'trial',
                    subscription_status TEXT NOT NULL DEFAULT 'trial',
                    subscription_expires_at TEXT,
                    report_channel_id TEXT,
                    report_forum_channel_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS identities (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    external_user_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL DEFAULT '',
                    label TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(provider, workspace_id, external_user_id)
                );

                CREATE TABLE IF NOT EXISTS usage_events (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                    trace_id TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS usage_events_tenant_created
                    ON usage_events(tenant_id, created_at);

                CREATE TABLE IF NOT EXISTS audit_events (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT,
                    action TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                """
            )
            self._migrate(db)

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        """给已经存在的库补列。

        CREATE TABLE IF NOT EXISTS 对**已存在**的表不做任何事——
        新字段只会出现在全新安装上，线上那份老库悄悄地少两列，
        直到某次 UPDATE 报 "no such column" 才发现。
        """
        existing = {row["name"] for row in db.execute("PRAGMA table_info(tenants)")}
        for column in ("report_channel_id", "report_forum_channel_id"):
            if column not in existing:
                db.execute(f"ALTER TABLE tenants ADD COLUMN {column} TEXT")

    @staticmethod
    def _tenant(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def _audit(
        self,
        db: sqlite3.Connection,
        action: str,
        tenant_id: str | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        db.execute(
            "INSERT INTO audit_events (id, tenant_id, action, details_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (_public_id("audit"), tenant_id, action, json.dumps(details or {}, ensure_ascii=False), _now()),
        )

    def ensure_default_tenant(self, name: str = "Owner") -> dict[str, Any]:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM tenants WHERE id = 'default'").fetchone()
            if row is None:
                now = _now()
                db.execute(
                    """INSERT INTO tenants
                    (id, name, status, plan_code, subscription_status, created_at, updated_at)
                    VALUES ('default', ?, 'active', 'owner', 'active', ?, ?)""",
                    (name.strip() or "Owner", now, now),
                )
                self._audit(db, "tenant.created", "default", {"source": "bootstrap"})
                row = db.execute("SELECT * FROM tenants WHERE id = 'default'").fetchone()
            return dict(row)

    def create_tenant(
        self,
        name: str,
        *,
        plan_code: str = "trial",
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        display_name = name.strip()
        if not display_name or len(display_name) > 120:
            raise ValueError("name must contain 1-120 characters")
        identifier = tenant_id or _public_id("tn")
        now = _now()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO tenants
                (id, name, status, plan_code, subscription_status, created_at, updated_at)
                VALUES (?, ?, 'active', ?, 'trial', ?, ?)""",
                (identifier, display_name, plan_code.strip() or "trial", now, now),
            )
            self._audit(db, "tenant.created", identifier, {"name": display_name})
            row = db.execute("SELECT * FROM tenants WHERE id = ?", (identifier,)).fetchone()
            return dict(row)

    def get_tenant(self, tenant_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            return self._tenant(db.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone())

    def list_tenants(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM tenants ORDER BY created_at, id").fetchall()
            return [dict(row) for row in rows]

    def update_tenant(self, tenant_id: str, **changes: Any) -> dict[str, Any]:
        values = {key: value for key, value in changes.items() if key in _UPDATABLE_TENANT_FIELDS}
        if not values:
            raise ValueError("no supported tenant fields were supplied")
        if "status" in values and values["status"] not in _TENANT_STATUSES:
            raise ValueError("invalid tenant status")
        if (
            "subscription_status" in values
            and values["subscription_status"] not in _SUBSCRIPTION_STATUSES
        ):
            raise ValueError("invalid subscription status")
        for field in _CHANNEL_FIELDS & values.keys():
            raw = values[field]
            if raw in {None, ""}:
                values[field] = None
                continue
            text = str(raw).strip()
            # Discord 频道号就是一串数字。不校验的话，粘进来一个带空格的
            # 频道名会被原样存下，直到某天报告发不出去才发现。
            if not text.isdigit() or len(text) > 32:
                raise ValueError(f"{field} must be a numeric Discord channel id")
            values[field] = text

        if "name" in values:
            values["name"] = str(values["name"]).strip()
            if not values["name"] or len(values["name"]) > 120:
                raise ValueError("name must contain 1-120 characters")
        if "plan_code" in values:
            values["plan_code"] = str(values["plan_code"]).strip()
            if not values["plan_code"] or len(values["plan_code"]) > 64:
                raise ValueError("plan_code must contain 1-64 characters")
        if "subscription_expires_at" in values:
            raw_expiry = values["subscription_expires_at"]
            if raw_expiry in {None, ""}:
                values["subscription_expires_at"] = None
            else:
                expiry = str(raw_expiry).strip()
                try:
                    datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("subscription_expires_at must be an ISO date or datetime") from exc
                values["subscription_expires_at"] = expiry

        values["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        params = [*values.values(), tenant_id]
        with self._lock, self._connect() as db:
            cursor = db.execute(f"UPDATE tenants SET {assignments} WHERE id = ?", params)
            if cursor.rowcount != 1:
                raise ValueError("tenant was not found")
            self._audit(db, "tenant.updated", tenant_id, values)
            row = db.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
            return dict(row)

    def bind_identity(
        self,
        tenant_id: str,
        provider: str,
        external_user_id: str,
        *,
        workspace_id: str = "",
        label: str = "",
    ) -> dict[str, Any]:
        provider = provider.strip().lower()
        external_user_id = external_user_id.strip()
        workspace_id = workspace_id.strip()
        if not _SAFE_PROVIDER.fullmatch(provider):
            raise ValueError("invalid identity provider")
        if not external_user_id or len(external_user_id) > 200:
            raise ValueError("external_user_id must contain 1-200 characters")
        if self.get_tenant(tenant_id) is None:
            raise ValueError("tenant was not found")

        identity_id = _public_id("idn")
        with self._lock, self._connect() as db:
            try:
                db.execute(
                    """INSERT INTO identities
                    (id, tenant_id, provider, external_user_id, workspace_id, label, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        identity_id,
                        tenant_id,
                        provider,
                        external_user_id,
                        workspace_id,
                        label.strip()[:120],
                        _now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("identity is already bound") from exc
            self._audit(db, "identity.bound", tenant_id, {"provider": provider})
            row = db.execute("SELECT * FROM identities WHERE id = ?", (identity_id,)).fetchone()
            return dict(row)

    def list_identities(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            if tenant_id:
                rows = db.execute(
                    "SELECT * FROM identities WHERE tenant_id = ? ORDER BY created_at", (tenant_id,)
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM identities ORDER BY created_at").fetchall()
            return [dict(row) for row in rows]

    def remove_identity(self, identity_id: str) -> None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT tenant_id, provider FROM identities WHERE id = ?", (identity_id,)
            ).fetchone()
            if row is None:
                raise ValueError("identity was not found")
            db.execute("DELETE FROM identities WHERE id = ?", (identity_id,))
            self._audit(
                db,
                "identity.removed",
                str(row["tenant_id"]),
                {"provider": str(row["provider"])},
            )

    def resolve_identity(
        self,
        provider: str,
        external_user_id: str,
        workspace_id: str = "",
    ) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT tenants.* FROM identities
                JOIN tenants ON tenants.id = identities.tenant_id
                WHERE identities.provider = ? AND identities.external_user_id = ?
                  AND identities.workspace_id = ?""",
                (provider.strip().lower(), external_user_id.strip(), workspace_id.strip()),
            ).fetchone()
            return self._tenant(row)

    def list_audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM audit_events ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
            return [dict(row) for row in rows]

    def record_usage(
        self,
        tenant_id: str,
        trace_id: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        if tenant_id == "default":
            self.ensure_default_tenant()
        with self._lock, self._connect() as db:
            if db.execute("SELECT 1 FROM tenants WHERE id = ?", (tenant_id,)).fetchone() is None:
                raise ValueError("tenant was not found")
            db.execute(
                """INSERT INTO usage_events
                (id, tenant_id, trace_id, model, prompt_tokens, completion_tokens, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    _public_id("use"),
                    tenant_id,
                    trace_id,
                    model,
                    max(int(prompt_tokens or 0), 0),
                    max(int(completion_tokens or 0), 0),
                    _now(),
                ),
            )

    def usage_summary(self, days: int = 30, tenant_id: str | None = None) -> dict[str, int]:
        modifier = f"-{max(int(days), 1)} days"
        query = """SELECT COUNT(*) AS calls,
                    COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens
                   FROM usage_events WHERE created_at >= datetime('now', ?)"""
        params: list[Any] = [modifier]
        if tenant_id:
            query += " AND tenant_id = ?"
            params.append(tenant_id)
        with self._connect() as db:
            row = db.execute(query, params).fetchone()
        prompt = int(row["prompt_tokens"])
        completion = int(row["completion_tokens"])
        return {
            "calls": int(row["calls"]),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }


_store: ControlStore | None = None
_store_lock = threading.Lock()


def control_db_path() -> Path:
    configured = os.getenv("CONTROL_DB_PATH", "").strip()
    return Path(configured).expanduser() if configured else DATA_DIR / "control" / "control.db"


def get_control_store() -> ControlStore:
    global _store
    with _store_lock:
        if _store is None or _store.path != control_db_path():
            _store = ControlStore(control_db_path())
        return _store
