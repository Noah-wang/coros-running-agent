import json
from typing import Any

from src.integrations.coros_mcp import call_coros_tool, compact_json, list_coros_tools
from src.runtime.llm import complete_json, complete_text
from src.runtime.memory import format_memory_for_prompt
from agents.coros_report.shadowrunner_prompt import REPORT_SYSTEM_PROMPT
from agents.coros_report.tool_planner_prompt import TOOL_PLANNER_PROMPT


def _tool_summary(tools: list[dict[str, Any]]) -> str:
	slim_tools = []
	for tool in tools:
		slim_tools.append(
			{
				"name": tool.get("name"),
				"description": tool.get("description"),
				"inputSchema": tool.get("inputSchema"),
			}
		)
	return compact_json(slim_tools)


async def _plan_tool_calls(user_request: str, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
	plan = await complete_json(
		TOOL_PLANNER_PROMPT,
		f"""
User request:
{user_request}

Available COROS MCP tools:
{_tool_summary(tools)}
""".strip(),
	)

	tool_calls = plan.get("tool_calls", [])
	if not isinstance(tool_calls, list):
		return []

	clean_calls: list[dict[str, Any]] = []
	available = {tool.get("name") for tool in tools}
	for item in tool_calls[:4]:
		if not isinstance(item, dict):
			continue
		name = item.get("name")
		if not isinstance(name, str) or name not in available:
			continue
		arguments = item.get("arguments")
		if not isinstance(arguments, dict):
			arguments = {}
		clean_calls.append({"name": name, "arguments": arguments})
	return clean_calls


async def plan_coros_tools(user_request: str) -> list[dict[str, Any]]:
	tools = await list_coros_tools()
	return await _plan_tool_calls(user_request, tools)


async def fetch_coros_results(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
	results: list[dict[str, Any]] = []
	for call in tool_calls:
		try:
			result = await call_coros_tool(call["name"], call["arguments"])
			results.append({"tool": call, "ok": True, "result": result})
		except Exception as exc:
			results.append({"tool": call, "ok": False, "error": str(exc)})
	return results


async def render_coros_report(
	user_request: str,
	results: list[dict[str, Any]],
) -> str:
	if not results:
		return (
			"我已经连上 COROS MCP，但还没有找到可以无参数读取运动数据的工具。"
			"你可以先发送 `!coros-tools` 查看工具列表，然后我们再调整工具选择逻辑。"
		)

	memory = format_memory_for_prompt("coros-report")
	return await complete_text(
		REPORT_SYSTEM_PROMPT,
		f"""
User request:
{user_request}

User memory:
{memory}

COROS tool calls and results:
{json.dumps(results, ensure_ascii=False, indent=2)}

Generate the workout report from the available COROS data.
""".strip(),
	)


async def generate_coros_report(user_request: str) -> str:
	tool_calls = await plan_coros_tools(user_request)
	results = await fetch_coros_results(tool_calls)
	return await render_coros_report(user_request, results)


async def list_available_coros_tools() -> str:
	tools = await list_coros_tools()
	return compact_json(tools)
