import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from src.api.admin import admin_payload, apply_admin_action
from src.api.web_server import WebHandler
from src.runtime.control_store import ControlStore


def test_admin_action_manages_tenant_subscription_and_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("TENANT_DATA_ROOT", str(tmp_path / "tenants"))
    store = ControlStore(tmp_path / "control.db")

    payload = apply_admin_action(
        {"action": "create_tenant", "name": "Runner One", "plan_code": "personal"},
        store,
    )
    tenant = next(item for item in payload["tenants"] if item["id"] != "default")

    payload = apply_admin_action(
        {
            "action": "update_tenant",
            "tenant_id": tenant["id"],
            "subscription_status": "active",
            "subscription_expires_at": "2027-01-31",
        },
        store,
    )
    updated = next(item for item in payload["tenants"] if item["id"] == tenant["id"])
    assert updated["subscription_status"] == "active"

    payload = apply_admin_action(
        {
            "action": "bind_identity",
            "tenant_id": tenant["id"],
            "provider": "discord",
            "external_user_id": "123456789",
            "workspace_id": "guild-1",
            "label": "Personal Discord",
        },
        store,
    )
    updated = next(item for item in payload["tenants"] if item["id"] == tenant["id"])
    assert updated["identities"][0]["external_user_id"] == "123456789"

    identity_id = updated["identities"][0]["id"]
    payload = apply_admin_action(
        {"action": "remove_identity", "identity_id": identity_id}, store
    )
    updated = next(item for item in payload["tenants"] if item["id"] == tenant["id"])
    assert updated["identities"] == []


def test_admin_payload_never_returns_secret_values(tmp_path, monkeypatch):
    secret = "top-secret-api-key-value"
    monkeypatch.setenv("LLM_API_KEY", secret)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", secret)
    store = ControlStore(tmp_path / "control.db")

    serialized = json.dumps(admin_payload(store))

    assert secret not in serialized
    assert next(item for item in admin_payload(store)["integrations"] if item["id"] == "llm")[
        "configured"
    ]


def test_admin_api_requires_token_and_supports_tenant_creation(tmp_path, monkeypatch):
    # 管理后台走自己的 WEB_ADMIN_TOKEN。设置页的令牌**不该**能开这扇门，
    # 下面那条断言就是守这个的。
    monkeypatch.setenv("WEB_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("WEB_SETTINGS_TOKEN", "settings-only-token")
    monkeypatch.setenv("CONTROL_DB_PATH", str(tmp_path / "control.db"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), WebHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    try:
        with pytest.raises(HTTPError) as exc_info:
            urlopen(f"{base_url}/api/admin", timeout=3)
        assert exc_info.value.code == 401

        # 设置页的令牌不能开管理后台。这条要走真的 HTTP——
        # 单元层面比对函数是一回事，端点上真的挡住是另一回事。
        with pytest.raises(HTTPError) as exc_info:
            urlopen(
                Request(
                    f"{base_url}/api/admin",
                    headers={"Authorization": "Bearer settings-only-token"},
                ),
                timeout=3,
            )
        assert exc_info.value.code == 401

        request = Request(
            f"{base_url}/api/admin",
            headers={"Authorization": "Bearer test-admin-token"},
        )
        with urlopen(request, timeout=3) as response:
            payload = json.load(response)
        assert payload["overview"]["tenants"] == 1

        create = Request(
            f"{base_url}/api/admin",
            data=json.dumps(
                {"action": "create_tenant", "name": "Runner Two", "plan_code": "trial"}
            ).encode(),
            method="POST",
            headers={
                "Authorization": "Bearer test-admin-token",
                "Content-Type": "application/json",
            },
        )
        with urlopen(create, timeout=3) as response:
            payload = json.load(response)
        assert payload["overview"]["tenants"] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
