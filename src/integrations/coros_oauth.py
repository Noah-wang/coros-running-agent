"""自己跑 COROS 的 OAuth，再把令牌写进 mcp-remote 的存储。

**为什么不用 mcp-remote 自带的授权流程**

它的回调地址写死成 `http://{host}:{port}/oauth/callback`。浏览器跳过去必然
打不开——那是**用户自己的机器**，不是服务器。于是用户得从地址栏把链接复制出来
再粘回聊天窗口。这是整条开通链路上唯一一个需要用户「理解原理」的步骤，
也是最容易流失的一步。

改造它的两条路都试过，都不通（mcp-remote 0.1.38）：

1. `--host` 换主机名：`redirectUrl` 是 ``http://${host}:${port}${path}``，
   scheme 硬编码 http、端口必带。要用就得公网明文暴露两万个端口。
2. `--static-oauth-client-metadata` 覆盖 `redirect_uris`：授权请求里的
   `redirect_uri` 取的是 `provider.redirectUrl`，不是注册用的元数据，
   两者不一致会被拒；而且 `findExistingClientPort` 在参数分支**之前**
   无条件先跑，读到非 localhost 的回调就抛
   "Cannot find localhost callback URI"，第二次启动直接崩。

设备码流程也试过。COROS 的 metadata 里声明 `device_code` 可用，
但动态注册的客户端拿不到这个 grant——注册时明确请求了，服务端静默忽略，
返回的 `grant_types` 只有 `authorization_code` 和 `refresh_token`，
随后调 device_authorization 一律 401。**文档说支持，实际不给。**

**所以走这条**：我们自己注册一个客户端（公网 HTTPS 回调，实测 COROS 接受），
自己跑 authorization_code + PKCE，拿到令牌后写进 mcp-remote 的存储目录。
mcp-remote 启动时发现已有令牌就不会再发起授权，用户只需要点一下。

**代价：这耦合了 mcp-remote 的内部存储格式。** 所以版本是钉死的
（`DEFAULT_MCP_CLIENT`）。写进去的 `client_info.json` 里**必须**带一个
localhost 回调地址，否则上面那个 `findExistingClientPort` 会抛错。
格式万一变了，原来那条粘贴回调的路（`auth_flow.complete_coros_auth_flow`）
仍然保留着，可以兜底。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from src.integrations.coros_mcp import DEFAULT_MCP_CLIENT, mcp_callback_port, mcp_config_dir
from src.runtime.paths import DATA_DIR
from src.runtime.tenant import current_tenant

COROS_MCP_URL = os.getenv("COROS_MCP_URL", "https://mcpus.coros.com/mcp")
SCOPE = "openid mcp.tools offline_access"
HTTP_TIMEOUT = 20

# 授权链接由 bot 进程生成，回调由 web 进程接收——**两个不同的进程**。
# 所以待处理的授权必须落盘，放内存里 web 那边根本看不到。
PENDING_PATH = DATA_DIR / "coros_oauth_pending.json"
PENDING_TTL_SECONDS = 900


def _post(url: str, data: dict[str, str] | None = None, json_body: dict | None = None) -> dict:
    if json_body is not None:
        payload = json.dumps(json_body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    else:
        payload = urllib.parse.urlencode(data or {}).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def metadata() -> dict:
    # 用 urlparse 取源，别用字符串切。
    # "https://mcpus.coros.com/mcp".split("/mcp") 会在 `//mcpus` 那里就切开
    # （主机名里本身含 "mcp"），拿到 "https:/"，然后报 "no host given"。
    parts = urllib.parse.urlparse(COROS_MCP_URL)
    base = f"{parts.scheme}://{parts.netloc}"
    return _get(f"{base}/.well-known/oauth-authorization-server")


def public_redirect_uri() -> str:
    explicit = os.getenv("COROS_OAUTH_REDIRECT_URI", "").strip()
    if explicit:
        return explicit
    domain = os.getenv("WEB_PUBLIC_DOMAIN", "").strip()
    if not domain or domain in {"localhost", "127.0.0.1"}:
        raise RuntimeError(
            "没有公网回调地址。请配置 WEB_PUBLIC_DOMAIN 或 COROS_OAUTH_REDIRECT_URI。"
        )
    return f"https://{domain}/coros/callback"


# ── mcp-remote 的存储 ────────────────────────────────────────────────

def _mcp_version() -> str:
    # "mcp-remote@0.1.38" → "0.1.38"。目录名带版本号，取错了写进去也没人读。
    _, _, version = DEFAULT_MCP_CLIENT.partition("@")
    return version or "0.1.38"


def _server_url_hash() -> str:
    return hashlib.md5(COROS_MCP_URL.encode("utf-8")).hexdigest()


def credentials_dir(tenant_id: str | None = None) -> Path:
    base = mcp_config_dir(tenant_id)
    if base is None:
        # 默认租户沿用 ~/.mcp-auth，保持升级前的安装不动。
        base = Path(os.getenv("MCP_REMOTE_CONFIG_DIR", "")).expanduser() or (
            Path.home() / ".mcp-auth"
        )
    return Path(base) / f"mcp-remote-{_mcp_version()}"


def _write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def write_mcp_credentials(tenant_id: str, client_info: dict, tokens: dict) -> None:
    """把客户端信息和令牌写成 mcp-remote 认识的样子。"""
    folder = credentials_dir(tenant_id)
    prefix = _server_url_hash()

    # **必须补一个 localhost 回调**：findExistingClientPort 找不到就抛错，
    # mcp-remote 会在第二次启动时直接崩掉。它只用这个地址反推端口号，
    # 不会真的往上面跳转。
    stored = dict(client_info)
    localhost_uri = f"http://localhost:{mcp_callback_port(tenant_id)}/oauth/callback"
    uris = [str(u) for u in stored.get("redirect_uris", [])]
    if not any(urllib.parse.urlparse(u).hostname in {"localhost", "127.0.0.1"} for u in uris):
        uris.append(localhost_uri)
    stored["redirect_uris"] = uris

    _write_private_json(folder / f"{prefix}_client_info.json", stored)
    _write_private_json(folder / f"{prefix}_tokens.json", tokens)


def has_credentials(tenant_id: str | None = None) -> bool:
    folder = credentials_dir(tenant_id)
    return (folder / f"{_server_url_hash()}_tokens.json").exists()


# ── 待处理授权（跨进程）────────────────────────────────────────────

def _load_pending() -> dict[str, Any]:
    if not PENDING_PATH.exists():
        return {}
    try:
        data = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_pending(data: dict[str, Any]) -> None:
    now = time.time()
    fresh = {
        state: entry
        for state, entry in data.items()
        if isinstance(entry, dict) and float(entry.get("expires_at", 0)) > now
    }
    _write_private_json(PENDING_PATH, fresh)


# ── 流程 ──────────────────────────────────────────────────────────────

def register_client(redirect_uri: str) -> dict:
    """动态注册一个客户端。

    COROS 会**静默改写**你请求的 grant_types（申请 device_code 直接被丢掉），
    所以这里只申请它真的会给的那两个，免得以为拿到了实际没有。
    """
    return _post(
        metadata()["registration_endpoint"],
        json_body={
            "client_name": os.getenv("COROS_OAUTH_CLIENT_NAME", "COROS Running Agent"),
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": SCOPE,
        },
    )


def start(tenant_id: str | None = None) -> str:
    """返回给用户点的授权链接。"""
    tenant = tenant_id or current_tenant().tenant_id
    redirect_uri = public_redirect_uri()
    client = register_client(redirect_uri)

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    state = secrets.token_urlsafe(24)

    pending = _load_pending()
    pending[state] = {
        "tenant_id": tenant,
        "code_verifier": verifier,
        "client_info": client,
        "redirect_uri": redirect_uri,
        "expires_at": time.time() + PENDING_TTL_SECONDS,
    }
    _save_pending(pending)

    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{metadata()['authorization_endpoint']}?{query}"


def complete(code: str, state: str) -> str:
    """用回调带回来的 code 换令牌并落盘。返回完成授权的租户 id。"""
    pending = _load_pending()
    entry = pending.pop(state, None)
    # state 一次性：用过就删。不删的话回调链接可以被重放。
    _save_pending(pending)

    if not isinstance(entry, dict):
        raise RuntimeError("授权请求已过期或不存在，请重新发起。")
    if float(entry.get("expires_at", 0)) <= time.time():
        raise RuntimeError("授权请求已过期，请重新发起。")

    client = entry["client_info"]
    tokens = _post(
        metadata()["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": entry["redirect_uri"],
            "client_id": client["client_id"],
            "code_verifier": entry["code_verifier"],
        },
    )
    if "access_token" not in tokens:
        raise RuntimeError(f"COROS 没有返回令牌：{str(tokens)[:120]}")

    tenant_id = str(entry["tenant_id"])
    write_mcp_credentials(tenant_id, client, tokens)
    return tenant_id
