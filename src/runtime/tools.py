import asyncio
import inspect
import json
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from src.runtime.llm import complete_with_tools
from src.runtime.trace import log_event
from src.runtime.untrusted import wrap as wrap_untrusted


DEFAULT_MAX_ROUNDS = 4
MAX_RESULT_CHARS = 4000
# 单个工具的执行上限。整轮对话最坏是 max_rounds × 这个值，
# 所以它必须明显小于用户愿意等的时间。
DEFAULT_TOOL_TIMEOUT_SECONDS = 75


def _tool_timeout_seconds() -> float:
    value = os.getenv("TOOL_TIMEOUT_SECONDS", str(DEFAULT_TOOL_TIMEOUT_SECONDS))
    try:
        return max(float(value), 5.0)
    except ValueError:
        return float(DEFAULT_TOOL_TIMEOUT_SECONDS)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]

    # 这个工具会不会改变状态。
    writes: bool = False
    # 这个工具的返回值里含有第三方能控制的文本（书籍原文、视频字幕、外部接口）。
    # 一旦这种内容进了上下文，本轮就不再允许调用写工具——见 run_tool_loop。
    returns_untrusted: bool = False
    # 这个工具自己需要多久。None 表示用全局的 TOOL_TIMEOUT_SECONDS。
    #
    # 全局值（75 秒）是按「一次取数」定的。但有的工具内部要串一整条链——
    # 睡眠报告要并行拉五个 COROS 接口再让模型写一篇报告，正常就要一两分钟。
    # 用全局值的话它每次都超时，而超时**不报错**、只是变成一条「拿不到数据」
    # 喂给模型，于是表现成「这个功能时好时坏」。
    timeout_seconds: float | None = None

    def schema(self) -> dict[str, Any]:
        """工具的 JSON schema，额外注入一个 why 字段。

        这是把 ReAct 的 Thought 找回来的办法。原版 ReAct 让模型输出
        `Thought: ...` 文本，天然可见；原生 function calling 下推理在模型内部，
        唯一漏出来的是调工具那一轮的 content——**而第一轮用 tool_choice="required"
        强制调工具时，DeepSeek 根本不输出 content**（实测 auto 有、required 为空）。

        强制调用是防幻觉用的，不能为了看思考就去掉。所以换个位置：
        让理由跟着工具参数一起来。参数是必须输出的，压不掉。
        """
        parameters = {
            **self.parameters,
            "properties": {
                **self.parameters.get("properties", {}),
                "why": {
                    "type": "string",
                    "description": "一句话说明你为什么现在要调这个工具",
                },
            },
        }
        # why 设成必填。设成可选时模型时填时不填——尤其第一轮
        # tool_choice="required" 下它倾向于只给最小参数集，理由就丢了。
        parameters["required"] = [
            *self.parameters.get("required", []),
            "why",
        ]
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }


class ToolRegistry:
    def __init__(self, tools: Sequence[Tool]) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    async def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            return {"error": f"unknown tool: {name}"}

        # why 是注入给模型解释自己的，业务处理器没有这个参数，传进去会 TypeError
        arguments = {k: v for k, v in arguments.items() if k != "why"}

        try:
            result = tool.handler(**arguments)
            if inspect.isawaitable(result):
                result = await result
            return result
        except TypeError as exc:
            return {"error": f"bad arguments for {name}: {exc}"}
        except Exception as exc:
            return {"error": f"{name} failed: {exc}"}


def _parse_arguments(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _serialize_result(result: Any) -> str:
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return f"{text[:MAX_RESULT_CHARS]}...(truncated)"


async def run_tool_loop(
    system_prompt: str,
    user_prompt: str,
    registry: ToolRegistry,
    history: Sequence[dict[str, str]] = (),
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    log: Callable[[str], None] | None = None,
    force_first_tool: bool = False,
    on_tool: Callable[[str, str], Any] | None = None,
    used_tools: list[str] | None = None,
) -> str:
    """跑一轮完整的工具调用循环，返回模型的最终文本回答。

    工具往返只存在于这一次调用内部。调用方传进来的 history 只包含
    user / assistant 两种消息，返回值也只是最终文本，
    这样多轮会话历史不会被中间过程撑爆。
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    messages.extend(dict(message) for message in history)
    messages.append({"role": "user", "content": user_prompt})

    # 本轮上下文里是否已经混入了第三方能控制的文本。一旦为真就不再允许写操作。
    tainted = False

    for round_index in range(max_rounds):
        # 第一轮强制调工具，是为了挡住「拿历史里自己上一轮的回答当数据源」。
        # 那种情况下模型不查就答，还会把没查过的字段一起编出来，
        # 而且提示词里写「必须查」是拦不住的——试过，第三轮就破功了。
        choice = "required" if force_first_tool and round_index == 0 else "auto"
        message = await complete_with_tools(messages, registry.schemas(), choice)
        tool_calls = getattr(message, "tool_calls", None)

        if not tool_calls:
            return message.content or ""

        # 模型在决定调工具那一轮往往会顺带说一句为什么。这段话本来就会被存进
        # 消息历史（下一轮它自己看得见），但一直没进日志——
        # 于是 trace 里只有「调了某个工具」，没有「为什么调它」。
        #
        # 这是原生 function calling 和原版 ReAct 的差别：原版把 Thought 写成
        # 提示词里的一行文本，天然可见；原生模式下推理在模型内部，
        # 只有这段 content 是唯一漏出来的部分。它已经在手边，记下来不花任何代价。
        thought = (message.content or "").strip()
        if thought:
            log_event("tool_thought", round=round_index, text=thought[:300])

        messages.append(
            {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in tool_calls
                ],
            }
        )

        for call in tool_calls:
            arguments = _parse_arguments(call.function.arguments)
            if used_tools is not None:
                used_tools.append(call.function.name)
            # 日志打在执行**之前**。打在之后的话，挂住的工具在日志里完全看不见——
            # 只能看到进了循环，然后没有下文，排查时根本不知道卡在哪一个工具上。
            log_event(
                "tool_call",
                round=round_index,
                name=call.function.name,
                why=str(arguments.get("why", ""))[:160] or "-",
                arguments=json.dumps(
                    {k: v for k, v in arguments.items() if k != "why"},
                    ensure_ascii=False,
                )[:200],
            )
            if log is not None:
                log(
                    f"tool_call round={round_index} name={call.function.name} "
                    f"arguments={arguments}"
                )
            started = time.monotonic()

            if on_tool is not None:
                # 把「要调什么、为什么」推给愿意显示进度的入口。
                # 失败不能影响主流程——它只是个提示。
                try:
                    await on_tool(call.function.name, str(arguments.get("why", "")))
                except Exception:
                    pass

            tool = registry.get(call.function.name)
            if tainted and tool is not None and tool.writes:
                # 本轮上下文里已经有第三方能控制的文本了，从这里开始拒绝写操作。
                #
                # 这是结构性的，不是靠提示词。注入的典型形态就是「先让你读到一段
                # 被投毒的资料，再诱导你去写」——把这两步隔开，中间那条链就断了。
                # 代价是「查完资料顺手记一笔」要分两句说，值得。
                result = {
                    "error": (
                        f"本轮已经读取过外部资料，出于安全不能再执行 {call.function.name} "
                        "这类写操作。如实告诉用户：请单独发一条消息来做这件事。"
                    )
                }
                log_event("write_blocked_after_untrusted", name=call.function.name)
                if log is not None:
                    log(f"write_blocked_after_untrusted name={call.function.name}")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": _serialize_result(result),
                    }
                )
                continue

            try:
                tool = registry.get(call.function.name)
                timeout = (
                    tool.timeout_seconds
                    if tool is not None and tool.timeout_seconds
                    else _tool_timeout_seconds()
                )
                result = await asyncio.wait_for(
                    registry.execute(call.function.name, arguments),
                    timeout=timeout,
                )
            except TimeoutError:
                # 超时要变成给模型的一条结果，而不是抛出去。
                # 抛出去整轮就废了；返回错误的话模型还能换个工具，
                # 或者至少如实告诉用户这个数据源现在拿不到。
                result = {
                    "error": (
                        f"{call.function.name} 超时（{_tool_timeout_seconds():.0f} 秒）。"
                        "这个数据源现在拿不到，别等它，如实告诉用户。"
                    )
                }
                log_event("tool_timeout", round=round_index, name=call.function.name)
                if log is not None:
                    log(f"tool_timeout round={round_index} name={call.function.name}")
            content = _serialize_result(result)
            log_event(
                "tool_result",
                name=call.function.name,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                chars=len(content),
                untrusted=bool(tool is not None and tool.returns_untrusted),
            )
            if tool is not None and tool.returns_untrusted:
                # 外部内容必须带边界标签进上下文，否则模型分不出
                # 「这是资料」和「这是指令」。
                content = wrap_untrusted(content, source=tool.name)
                tainted = True

            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": content}
            )

    # 轮数用尽，收掉工具再要一次最终回答，避免无限循环。
    final = await complete_with_tools(messages, None)
    return final.content or ""
