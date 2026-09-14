import asyncio
import os
import re
from dataclasses import dataclass
from typing import TextIO
from urllib.error import URLError
from urllib.request import urlopen

from src.integrations.coros_mcp import (
    DEFAULT_MCP_CLIENT,
    mcp_callback_port,
    mcp_config_dir,
)
from src.runtime.tenant import current_tenant


COROS_MCP_URL = os.getenv("COROS_MCP_URL", "https://mcpus.coros.com/mcp")
AUTH_TIMEOUT_SECONDS = int(os.getenv("COROS_MCP_AUTH_TIMEOUT_SECONDS", "300"))

_auth_processes: dict[str, asyncio.subprocess.Process] = {}


@dataclass(frozen=True)
class CorosAuthStartResult:
    authorization_url: str | None
    callback_port: int
    already_connected: bool = False


def is_coros_callback_url(text: str) -> bool:
    return bool(re.search(r"https?://(?:localhost|127\.0\.0\.1):\d+/oauth/callback\?", text))


def _extract_authorization_url(text: str) -> str | None:
    match = re.search(r"https://mcpus\.coros\.com/oauth2/authorize\?\S+", text)
    if match:
        return match.group(0).strip()
    return None


def _extract_callback_path(text: str) -> str | None:
    port = mcp_callback_port()
    match = re.search(
        rf"https?://(?:localhost|127\.0\.0\.1):{port}(/oauth/callback\?\S+)",
        text,
    )
    if match:
        return match.group(1).strip()
    return None


async def _terminate_existing_process(tenant_id: str | None = None) -> None:
    identifier = tenant_id or current_tenant().tenant_id
    process = _auth_processes.get(identifier)
    if process is None:
        return
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()
    _auth_processes.pop(identifier, None)


async def _read_until_auth_state(stream: asyncio.StreamReader, sink: list[str]) -> str | None:
    while True:
        line = await stream.readline()
        if not line:
            return None
        text = line.decode("utf-8", errors="replace")
        sink.append(text)
        url = _extract_authorization_url(text)
        if url:
            return url
        if "Proxy established successfully" in text or "Connected to remote server" in text:
            return "__CONNECTED__"


async def start_coros_auth_flow() -> CorosAuthStartResult:
    """Start mcp-remote OAuth and return the user-facing COROS authorization URL."""
    tenant_id = current_tenant().tenant_id
    callback_port = mcp_callback_port(tenant_id)
    await _terminate_existing_process(tenant_id)

    environment = dict(os.environ)
    profile = mcp_config_dir(tenant_id)
    if profile is not None:
        profile.mkdir(parents=True, exist_ok=True)
        environment["MCP_REMOTE_CONFIG_DIR"] = str(profile)

    process = await asyncio.create_subprocess_exec(
        "npx",
        DEFAULT_MCP_CLIENT,
        COROS_MCP_URL,
        str(callback_port),
        "--auth-timeout",
        str(AUTH_TIMEOUT_SECONDS),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )
    _auth_processes[tenant_id] = process
    stderr_lines: list[str] = []
    stdout_lines: list[str] = []
    assert process.stderr is not None
    assert process.stdout is not None

    done, pending = await asyncio.wait(
        {
            asyncio.create_task(_read_until_auth_state(process.stderr, stderr_lines)),
            asyncio.create_task(_read_until_auth_state(process.stdout, stdout_lines)),
        },
        return_when=asyncio.FIRST_COMPLETED,
        timeout=30,
    )
    for task in pending:
        task.cancel()

    authorization_url = None
    already_connected = False
    for task in done:
        result = task.result()
        if result == "__CONNECTED__":
            already_connected = True
            break
        authorization_url = result
        if authorization_url:
            break

    if already_connected:
        await _terminate_existing_process(tenant_id)
        return CorosAuthStartResult(
            authorization_url=None,
            callback_port=callback_port,
            already_connected=True,
        )

    if not authorization_url:
        await _terminate_existing_process(tenant_id)
        log_text = "".join(stderr_lines + stdout_lines).strip()
        raise RuntimeError(f"没有拿到 COROS 授权链接。{log_text[:500]}")

    return CorosAuthStartResult(
        authorization_url=authorization_url,
        callback_port=callback_port,
        already_connected=False,
    )


def _open_local_callback(callback_path: str) -> str:
    url = f"http://127.0.0.1:{mcp_callback_port()}{callback_path}"
    with urlopen(url, timeout=15) as response:
        body = response.read(2000).decode("utf-8", errors="replace")
    return body


async def complete_coros_auth_flow(callback_url: str) -> str:
    """Forward a pasted localhost callback URL into the server-side mcp-remote callback server."""
    path = _extract_callback_path(callback_url)
    if path is None:
        raise RuntimeError("这不是有效的 COROS 回调链接。请粘贴浏览器地址栏里的 localhost 回调地址。")

    try:
        body = await asyncio.to_thread(_open_local_callback, path)
    except URLError as exc:
        raise RuntimeError("没有正在等待授权的 COROS 连接流程。请先发送 `连接 COROS` 重新生成授权链接。") from exc

    tenant_id = current_tenant().tenant_id
    process = _auth_processes.get(tenant_id)
    if process is not None:
        try:
            await asyncio.wait_for(process.wait(), timeout=20)
        except TimeoutError:
            await _terminate_existing_process(tenant_id)

    return body.strip() or "COROS 授权已提交。"
