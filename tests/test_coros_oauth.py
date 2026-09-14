"""自建 COROS OAuth：公网回调 + 把令牌写进 mcp-remote 的存储。

这里测的重点不是「流程能跑通」，而是几个**不会报错、只会安静地坏掉**的地方：

- 写出去的 client_info 必须带一个 localhost 回调，否则 mcp-remote 的
  findExistingClientPort 会在第二次启动时抛 "Cannot find localhost callback URI"。
  少了这个，授权当天一切正常，重启之后全线挂掉。
- state 必须一次性，否则回调链接可以被重放。
- 文件名的哈希必须是 md5(server_url)，算错了写进去没人读，
  表现是「授权成功但仍然提示未授权」。
"""

import json
import os
import tempfile
import time

import pytest

os.environ.setdefault("COROS_RUNTIME_SETTINGS_PATH", tempfile.mktemp())

from src.integrations import coros_oauth as oauth  # noqa: E402

_real_credentials_dir = oauth.credentials_dir

CLIENT = {
    "client_id": "test-client-id",
    "redirect_uris": ["https://agent.example.com/coros/callback"],
    "grant_types": ["authorization_code", "refresh_token"],
    "token_endpoint_auth_method": "none",
}
TOKENS = {"access_token": "at", "refresh_token": "rt", "token_type": "Bearer"}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(oauth, "PENDING_PATH", tmp_path / "pending.json")
    monkeypatch.setattr(oauth, "COMPLETED_PATH", tmp_path / "completed.json")
    monkeypatch.setattr(oauth, "credentials_dir", lambda tenant_id=None: tmp_path / "mcp-auth")
    monkeypatch.setenv("WEB_PUBLIC_DOMAIN", "agent.example.com")
    yield


def test_hash_matches_mcp_remote(monkeypatch):
    """线上实测：md5("https://mcpus.coros.com/mcp") 就是文件名前缀。"""
    monkeypatch.setattr(oauth, "COROS_MCP_URL", "https://mcpus.coros.com/mcp")
    assert oauth._server_url_hash() == "f5d358637f13fb7a24af0a0aa57302d1"


def test_redirect_uri_from_domain():
    assert oauth.public_redirect_uri() == "https://agent.example.com/coros/callback"


def test_localhost_domain_is_refused(monkeypatch):
    """没有公网域名就该明确报错，而不是生成一个没人能跳回来的地址。"""
    monkeypatch.setenv("WEB_PUBLIC_DOMAIN", "localhost")
    with pytest.raises(RuntimeError):
        oauth.public_redirect_uri()


def test_written_client_info_keeps_a_localhost_uri(tmp_path):
    """**最关键的一条。** 少了 localhost 回调，mcp-remote 重启即崩。"""
    oauth.write_mcp_credentials("default", CLIENT, TOKENS)
    written = json.loads(
        (tmp_path / "mcp-auth" / f"{oauth._server_url_hash()}_client_info.json").read_text()
    )
    from urllib.parse import urlparse

    hosts = [urlparse(uri).hostname for uri in written["redirect_uris"]]
    assert "localhost" in hosts, "没有 localhost 回调，findExistingClientPort 会抛错"
    assert "agent.example.com" in hosts, "公网回调不能被覆盖掉"


def test_tokens_written_where_mcp_remote_reads(tmp_path):
    oauth.write_mcp_credentials("default", CLIENT, TOKENS)
    path = tmp_path / "mcp-auth" / f"{oauth._server_url_hash()}_tokens.json"
    assert path.exists()
    assert json.loads(path.read_text())["access_token"] == "at"


def test_credentials_are_not_world_readable(tmp_path):
    oauth.write_mcp_credentials("default", CLIENT, TOKENS)
    path = tmp_path / "mcp-auth" / f"{oauth._server_url_hash()}_tokens.json"
    assert oct(path.stat().st_mode)[-3:] == "600"


# ── state 的一次性和过期 ─────────────────────────────────────────────

def _pend(state: str, **overrides):
    entry = {
        "tenant_id": "default",
        "code_verifier": "v",
        "client_info": CLIENT,
        "redirect_uri": "https://agent.example.com/coros/callback",
        "expires_at": time.time() + 600,
    }
    entry.update(overrides)
    oauth._save_pending({state: entry})


def test_unknown_state_is_rejected():
    with pytest.raises(RuntimeError):
        oauth.complete("code", "never-issued")


def test_expired_state_is_rejected():
    _pend("s1", expires_at=time.time() - 1)
    with pytest.raises(RuntimeError):
        oauth.complete("code", "s1")


def test_state_is_single_use(monkeypatch):
    """回调链接会留在浏览器历史里。不作废的话，重放一次就能再换一份令牌。"""
    _pend("s2")
    monkeypatch.setattr(oauth, "_post", lambda *a, **k: dict(TOKENS))
    monkeypatch.setattr(oauth, "metadata", lambda: {"token_endpoint": "https://x/token"})

    assert oauth.complete("code", "s2") == "default"
    with pytest.raises(RuntimeError):
        oauth.complete("code", "s2")


def test_token_error_is_surfaced(monkeypatch):
    """COROS 返回错误时要抛出来，不能写一个没有 access_token 的空壳进去。"""
    _pend("s3")
    monkeypatch.setattr(oauth, "_post", lambda *a, **k: {"error": "invalid_grant"})
    monkeypatch.setattr(oauth, "metadata", lambda: {"token_endpoint": "https://x/token"})
    with pytest.raises(RuntimeError, match="没有返回令牌"):
        oauth.complete("code", "s3")


def test_pending_survives_across_processes(tmp_path):
    """授权链接是 bot 进程生成的，回调落在 web 进程——内存里存会直接丢。"""
    _pend("s4")
    assert "s4" in oauth._load_pending()
    assert oauth.PENDING_PATH.exists()


def test_metadata_base_survives_mcp_in_hostname(monkeypatch):
    """主机名 mcpus 里本身含 "mcp"——按字符串切会得到 "https:/"，然后 no host given。"""
    seen = {}
    monkeypatch.setattr(oauth, "COROS_MCP_URL", "https://mcpus.coros.com/mcp")
    monkeypatch.setattr(oauth, "_get", lambda url: seen.setdefault("url", url) or {})
    oauth.metadata()
    assert seen["url"] == (
        "https://mcpus.coros.com/.well-known/oauth-authorization-server"
    )


# ── 令牌写到哪 ────────────────────────────────────────────────────────

def test_default_tenant_falls_back_to_home_not_cwd(monkeypatch, tmp_path):
    """线上踩过：Path("") 是 Path(".")，**真值**，兜底永远不触发，
    令牌被写进当时的工作目录，mcp-remote 去 ~/.mcp-auth 读读不到——
    授权「成功」了却还是连不上。"""
    monkeypatch.setattr(oauth, "credentials_dir", oauth.credentials_dir.__wrapped__
                        if hasattr(oauth.credentials_dir, "__wrapped__") else _real_credentials_dir)
    monkeypatch.setattr(oauth, "mcp_config_dir", lambda tenant_id=None: None)
    monkeypatch.delenv("MCP_REMOTE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(oauth.Path, "home", staticmethod(lambda: tmp_path / "home"))

    folder = oauth.credentials_dir("default")
    assert str(folder).startswith(str(tmp_path / "home")), f"落在了 {folder}"
    assert ".mcp-auth" in str(folder)


# ── 完成记号（跨进程回话）────────────────────────────────────────────

def test_completion_is_recorded_and_taken_once(monkeypatch):
    """web 写记号、bot 取记号。取走即删，否则会重复回话。"""
    _pend("s5")
    monkeypatch.setattr(oauth, "_post", lambda *a, **k: dict(TOKENS))
    monkeypatch.setattr(oauth, "metadata", lambda: {"token_endpoint": "https://x/token"})

    oauth.complete("code", "s5")
    assert oauth.take_completion("s5") == "default"
    assert oauth.take_completion("s5") is None


def test_take_completion_unknown_state_is_none():
    assert oauth.take_completion("never") is None
