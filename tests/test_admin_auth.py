"""管理后台的门：单独的密码 + 猜错要被限速。

两个问题都不会自己暴露：

1. 管理后台原来和设置页共用 WEB_SETTINGS_TOKEN。设置页只是开关自动化，
   管理后台能建用户、改订阅、看所有租户用量——共用等于把小钥匙当大钥匙用。
2. 鉴权端点完全不限速。ratelimit 只挡 /api/chat，所以令牌可以被慢速爆破：
   一分钟试几次、试上一整天，在聊天限流看来完全正常。
"""

import os
import tempfile

import pytest

os.environ.setdefault("COROS_RUNTIME_SETTINGS_PATH", tempfile.mktemp())

from src.api import web_server as ws  # noqa: E402


class _FakeHandler:
    """只借 WebHandler 的鉴权方法，不起真的 HTTP 服务。"""

    def __init__(self, token: str | None, ip: str = "1.2.3.4") -> None:
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._ip = ip
        self.sent: list = []

    _bearer_token = ws.WebHandler._bearer_token
    _token_matches = ws.WebHandler._token_matches
    _settings_authorized = ws.WebHandler._settings_authorized
    _admin_authorized = ws.WebHandler._admin_authorized

    def _client_ip(self) -> str:
        return self._ip


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    ws._AUTH_FAILURES.clear()
    monkeypatch.setenv("WEB_SETTINGS_TOKEN", "settings-secret")
    monkeypatch.setenv("WEB_ADMIN_TOKEN", "admin-secret")
    yield
    ws._AUTH_FAILURES.clear()


def test_settings_token_does_not_open_the_admin_panel():
    """这是拆开两把钥匙的全部意义。"""
    handler = _FakeHandler("settings-secret")
    assert handler._settings_authorized() is True
    assert handler._admin_authorized() is False


def test_admin_token_opens_the_admin_panel():
    handler = _FakeHandler("admin-secret")
    assert handler._admin_authorized() is True


def test_missing_token_env_denies_instead_of_allowing(monkeypatch):
    """没配就一律拒绝。写成「没配就放行」的话，少配一个变量就全世界敞开。"""
    monkeypatch.delenv("WEB_ADMIN_TOKEN", raising=False)
    assert _FakeHandler("admin-secret")._admin_authorized() is False
    assert _FakeHandler(None)._admin_authorized() is False


def test_empty_token_env_denies(monkeypatch):
    monkeypatch.setenv("WEB_ADMIN_TOKEN", "   ")
    assert _FakeHandler("")._admin_authorized() is False


def test_wrong_token_denied():
    assert _FakeHandler("nope")._admin_authorized() is False


# ── 爆破限速 ──────────────────────────────────────────────────────────

def test_failures_below_limit_are_not_blocked(monkeypatch):
    monkeypatch.setenv("WEB_AUTH_FAIL_LIMIT", "5")
    for _ in range(4):
        ws._record_auth_failure("9.9.9.9")
    blocked, _ = ws._auth_failure_blocked("9.9.9.9")
    assert blocked is False


def test_blocked_after_limit(monkeypatch):
    monkeypatch.setenv("WEB_AUTH_FAIL_LIMIT", "5")
    for _ in range(5):
        ws._record_auth_failure("9.9.9.9")
    blocked, retry_after = ws._auth_failure_blocked("9.9.9.9")
    assert blocked is True
    assert retry_after > 0


def test_block_is_per_source(monkeypatch):
    """按 IP 分。不分的话一个人猜错就把所有人锁在门外。"""
    monkeypatch.setenv("WEB_AUTH_FAIL_LIMIT", "5")
    for _ in range(5):
        ws._record_auth_failure("9.9.9.9")
    assert ws._auth_failure_blocked("9.9.9.9")[0] is True
    assert ws._auth_failure_blocked("8.8.8.8")[0] is False


def test_success_clears_failures(monkeypatch):
    """自己手滑输错几次，输对之后要能立刻进去。"""
    monkeypatch.setenv("WEB_AUTH_FAIL_LIMIT", "5")
    for _ in range(4):
        ws._record_auth_failure("9.9.9.9")
    ws._clear_auth_failures("9.9.9.9")
    assert ws._auth_failure_blocked("9.9.9.9")[0] is False


def test_window_expiry_releases_the_block(monkeypatch):
    """过了窗口要自动解封，否则等于永久拉黑一个 IP。"""
    monkeypatch.setenv("WEB_AUTH_FAIL_LIMIT", "2")
    monkeypatch.setenv("WEB_AUTH_FAIL_WINDOW_SECONDS", "900")
    for _ in range(2):
        ws._record_auth_failure("9.9.9.9")
    assert ws._auth_failure_blocked("9.9.9.9")[0] is True

    # 把记录挪到窗口之外，等价于时间流逝
    ws._AUTH_FAILURES["9.9.9.9"] = type(ws._AUTH_FAILURES["9.9.9.9"])(
        [t - 1000 for t in ws._AUTH_FAILURES["9.9.9.9"]]
    )
    assert ws._auth_failure_blocked("9.9.9.9")[0] is False
