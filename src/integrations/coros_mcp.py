import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


DEFAULT_TIMEOUT_SECONDS = 60


def _timeout_seconds() -> float:
	value = os.getenv("COROS_MCP_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
	try:
		return max(float(value), 5.0)
	except ValueError:
		return float(DEFAULT_TIMEOUT_SECONDS)


def _serialize(value: Any) -> Any:
	if hasattr(value, "model_dump"):
		return value.model_dump(mode="json")
	if isinstance(value, list):
		return [_serialize(item) for item in value]
	if isinstance(value, dict):
		return {key: _serialize(item) for key, item in value.items()}
	return value


# mcp-remote 必须固定版本。
#
# 它把 OAuth 令牌按版本存在 ~/.mcp-auth/mcp-remote-<版本>/ 下面。写成不带版本的
# `mcp-remote` 时 npx 会拉最新版，而新版本的目录里没有令牌——于是它停在等待授权
# 那一步永远不返回，看起来就是 Discord 那边「正在输入」一直亮着。
#
# 2026-08-22 就是这么坏的：npx 自己升到 0.1.40，而令牌在 0.1.38 里。
# 升级要主动做，并且要有意识地重新授权一次，不能让它半夜自己升。
DEFAULT_MCP_CLIENT = "mcp-remote@0.1.38"


@asynccontextmanager
async def coros_session() -> AsyncIterator[ClientSession]:
	url = os.getenv("COROS_MCP_URL", "https://mcpus.coros.com/mcp")
	client = os.getenv("COROS_MCP_CLIENT", DEFAULT_MCP_CLIENT)
	server = StdioServerParameters(
		command="npx",
		args=[client, url],
	)

	async with stdio_client(server) as streams:
		async with ClientSession(*streams) as session:
			await session.initialize()
			yield session


async def list_coros_tools() -> list[dict[str, Any]]:
	async def _run() -> list[dict[str, Any]]:
		async with coros_session() as session:
			result = await session.list_tools()
			return _serialize(result.tools)

	return await asyncio.wait_for(_run(), timeout=_timeout_seconds())


async def call_coros_tool(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
	"""调一个 COROS MCP 工具。

	必须有超时。COROS 的 OAuth 令牌过期时，mcp-remote 会停在等待授权那一步
	**永远不返回**——不是报错，是挂着。上层的自动报告自己包了 wait_for 所以
	能看到超时，而工具循环这条路是裸调的，一条用户消息就能把整轮卡死，
	Discord 那边「正在输入」一直亮着。
	"""
	async def _run() -> dict[str, Any]:
		async with coros_session() as session:
			result = await session.call_tool(name, arguments or {})
			return _serialize(result)

	try:
		return await asyncio.wait_for(_run(), timeout=_timeout_seconds())
	except TimeoutError as exc:
		raise RuntimeError(
			f"COROS MCP 调用 {name} 超时（{_timeout_seconds():.0f} 秒）。"
			"常见原因是 COROS 授权过期，需要重新授权 mcp-remote。"
		) from exc


def compact_json(value: Any) -> str:
	return json.dumps(value, ensure_ascii=False, indent=2)
