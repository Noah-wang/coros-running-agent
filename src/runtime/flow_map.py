"""工具到架构模块的映射，用于前端把一次请求"走"在架构图上。

前端那张架构图原来是静态 SVG，看得出有哪些模块，看不出**一次提问实际经过了哪几个**。
把工具调用映射成模块 id 之后，回答过程中命中哪个就高亮哪个。

映射刻意做得粗：一次提问会调三四个工具，如果每个工具都是一个节点，
图上会有二十几个框，反而看不清主线。所以按**数据来源**归并——
用户关心的是"它去查了运动数据还是知识库"，不是"调了哪个函数"。
"""

from typing import Any

# 模块 id → 展示名。id 同时是 SVG 里节点的 data-module 值。
MODULES: dict[str, str] = {
    "entry": "Input",
    "router": "Router",
    "loop": "Main agent loop",
    "capability": "Capability layer",
    "langgraph": "LangGraph",
    "races": "Race photos",
    "coros": "COROS data",
    "profile": "Long-term profile",
    "knowledge": "RAG knowledge",
    "search": "Web search",
    "observation": "Tool result",
    "llm": "LLM generation",
    "reflection": "Reflection check",
    "answer": "Answer",
}

# 工具名 → 模块 id。没列出的工具归到 loop，不额外画节点。
TOOL_MODULES: dict[str, str] = {
    "list_races": "races",
    "photo": "races",
    "list_recent_activities": "coros",
    "get_sleep_report": "coros",
    "coros": "coros",
    "coros-list": "coros",
    "coros-activity": "coros",
    "coros-fit-sync": "coros",
    "coros-pb": "coros",
    "get_personal_bests": "profile",
    "get_athlete_profile": "profile",
    "feel": "profile",
    "feelings": "profile",
    "search_running_knowledge": "knowledge",
    "running": "knowledge",
    "running-video": "knowledge",
    "knowledge-source": "knowledge",
    "search_web": "search",
    "reflection": "reflection",
}

COMMAND_MODULES: dict[str, tuple[str, ...]] = {
    "coros": (
        "entry",
        "router",
        "capability",
        "langgraph",
        "coros",
        "observation",
        "llm",
        "answer",
    ),
    "coros-activity": (
        "entry",
        "router",
        "capability",
        "coros",
        "observation",
        "llm",
        "answer",
    ),
    "coros-list": ("entry", "router", "capability", "coros", "answer"),
    "coros-pb": ("entry", "router", "capability", "profile", "answer"),
    "running": (
        "entry",
        "router",
        "capability",
        "profile",
        "knowledge",
        "observation",
        "llm",
        "answer",
    ),
    "feelings": ("entry", "router", "capability", "profile", "answer"),
}


def module_for(tool_name: str) -> str:
    return TOOL_MODULES.get(tool_name, "loop")


def step_payload(tool_name: str, why: str = "") -> dict[str, Any]:
    module = module_for(tool_name)
    return {
        "type": "trace_step",
        "module": module,
        "label": MODULES.get(module, module),
        "tool": tool_name,
        "why": why[:120],
    }


def command_modules(command_name: str) -> tuple[str, ...]:
    return COMMAND_MODULES.get(
        command_name,
        ("entry", "router", "capability", module_for(command_name), "answer"),
    )


def module_payload(module: str, why: str = "") -> dict[str, Any]:
    return {
        "type": "trace_step",
        "module": module,
        "label": MODULES.get(module, module),
        "why": why[:120],
    }
