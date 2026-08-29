import asyncio
import os
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Protocol

from src.ask import answer_open_question
from src.registry import CapabilityRegistry, get_registry
from src.runtime.capability import CommandContext, RuntimeAttachment
from src.runtime.conversation import (
    PHOTO_MEMORY_TOPIC,
    RUNNING_COACH_TOPIC,
    get_pending_questions,
)
from src.runtime.flow_map import command_modules, module_payload, step_payload
from src.runtime.llm import complete_json
from src.runtime.output_guard import sanitize as sanitize_output
from src.runtime.tools import Tool
from src.runtime.trace import Span, log_event, new_trace


ROUTER_SYSTEM_PROMPT = """
你是 COROS Running Agent 的自然语言路由器。

你的任务是把用户的一句话转换成当前入口允许的内部命令。只返回 JSON，不要解释。

返回格式：
{
  "command": "ask | coros | coros-tools | coros-list | coros-activity | coros-pb | coros-fit-sync | running | running-video | feel | feelings | photo | none",
  "argument": "传给命令的参数",
  "confidence": 0.0,
  "reason": "一句很短的原因"
}

规则：
- 只能选择用户提示中列出的 allowed_commands。
- command = "ask" 是开放式提问的出口，argument 保留用户原话。
  当用户要跨工具查询或计算一个答案时选 ask，例如「我今年跑了多少公里」
  「我最好的一次 10K 是哪天」「我离目标还差多少」。
- 有具体命令能精确覆盖时优先用具体命令。
- 如果用户意图不明确、只是闲聊、感谢、测试或打招呼，返回 command = "none"。
- command = "coros" 时，argument 保留用户原话，用于生成最近一次或指定运动报告。
- 当用户问“今天这次训练怎么样”“最近一次运动/跑步怎么样”“帮我复盘今天/最近训练”
  “生成运动报告”“下一次应该怎么练”时，只要 coros 在 allowed_commands 中，
  就优先选择 command = "coros"。
- command = "coros-tools" 时，argument 为空字符串，用于列出 COROS MCP 工具。
- command = "coros-list" 时，用于列出 COROS 运动记录摘要。用户说“列出运动记录”
  “查看历史运动”“看最近运动列表”“查所有运动记录”时选择它，argument 保留时间范围或条数。
- command = "coros-activity" 时，用于分析用户通过 coros-list 选择的某一条运动。
  用户说“分析第 1 条”“看第 3 条运动记录”“第 2 条重点看心率”时选择它。
- command = "coros-pb" 时，用于查看 COROS 自动记录的个人 PB。
- command = "coros-fit-sync" 时，用于把 COROS 原始 FIT 文件下载归档到服务器。
- command = "running" 时，argument 保留用户原话，用于跑步知识问答、训练计划、
  成绩瓶颈分析，或补充长期跑步档案。
- 用户补充年龄、身高、体重、半马/全马成绩、目标成绩、目标日期、周跑量、最长跑、
  比赛崩盘原因时，如果 running 在 allowed_commands 中，优先选择 command = "running"。
- command = "running-video" 时，argument 必须是用户提供的 B站链接或 BV 号，
  用于导入跑步视频知识库。
- command = "feel" 时，argument 保留用户原话，用于记录主观感受。
- command = "feelings" 时，argument 为空字符串。
- command = "photo" 时，argument 保留用户原话。照片能力内部会自己做意图识别，
  判断是新建一组、追加到已有分组、补充元数据还是检索，这里不要替它决定。
  Web 入口只允许照片检索，不允许保存或修改。
""".strip()


class MessageChannel(Protocol):
    id: int

    def send(self, content: str, /) -> Awaitable[object]:
        ...


@dataclass(frozen=True)
class NaturalLanguageRoute:
    command_name: str
    argument: str
    confidence: float = 1.0
    reason: str = ""


class MainAgentOrchestrator:
    def __init__(self, registry: CapabilityRegistry | None = None) -> None:
        self._registry = registry or get_registry()

    def describe_capabilities(self) -> str:
        return self._registry.describe()

    def run_startup_handlers(self, client: object) -> None:
        self._registry.run_startup_handlers(client)

    def is_allowed_for_command(self, channel_id: int, command_name: str) -> bool:
        channel_env_name = self._registry.channel_env_for_command(command_name)
        if channel_env_name is None:
            return True
        return self._is_allowed_channel(channel_id, channel_env_name)

    def is_capabilities_channel(self, channel_id: int) -> bool:
        return any(
            self._is_allowed_channel(channel_id, env_name)
            for env_name in self._registry.channel_env_names()
        )

    async def dispatch_command(
        self,
        client: object,
        channel: MessageChannel,
        command_name: str,
        argument: str = "",
        attachments: tuple[RuntimeAttachment, ...] = (),
        message: object | None = None,
    ) -> bool:
        if command_name == "ask":
            await self._handle_ask(
                self._command_context(
                    client,
                    channel,
                    attachments=attachments,
                    message=message,
                ),
                argument,
                client=client,
            )
            return True

        if not self.is_allowed_for_command(channel.id, command_name):
            return True

        try:
            return await self._registry.dispatch_command(
                self._command_context(
                    client,
                    channel,
                    attachments=attachments,
                    message=message,
                ),
                command_name,
                argument,
            )
        except Exception as exc:
            await self._send_error(channel, f"执行 `{command_name}` 失败", exc)
            self._log(f"command_failed command={command_name} error={exc}")
            return True

    async def dispatch_text(
        self,
        client: object,
        channel: MessageChannel,
        content: str,
        attachments: tuple[RuntimeAttachment, ...] = (),
        message: object | None = None,
    ) -> bool:
        new_trace("dc")
        with Span("request", surface="discord", channel=channel.id, chars=len(content.strip())):
            return await self._dispatch_text_inner(
                client, channel, content, attachments, message
            )

    async def _dispatch_text_inner(
        self,
        client: object,
        channel: MessageChannel,
        content: str,
        attachments: tuple[RuntimeAttachment, ...] = (),
        message: object | None = None,
    ) -> bool:
        stripped = content.strip()
        if not stripped:
            return False

        if stripped == "!capabilities":
            if self.is_capabilities_channel(channel.id):
                await channel.send(self.describe_capabilities())
            return True

        if stripped.startswith("!"):
            command_name, _, _ = stripped[1:].partition(" ")
            if not self.is_allowed_for_command(channel.id, command_name):
                return True
            try:
                return await self._registry.dispatch_text(
                    self._command_context(
                        client,
                        channel,
                        attachments=attachments,
                        message=message,
                    ),
                    stripped,
                )
            except Exception as exc:
                await self._send_error(channel, f"执行 `{command_name}` 失败", exc)
                self._log(f"text_command_failed command={command_name} error={exc}")
                return True

        # 只拦图片。原来是「有任何附件就当存照片」，
        # 结果在跑步频道贴张截图或传个 PDF 都会被照片能力接走。
        if self.is_allowed_for_command(channel.id, "photo") and any(
            attachment.is_image for attachment in attachments
        ):
            self._log(f"attachment_dispatch channel_id={channel.id} command=photo")
            # 原文交给照片能力，由它做意图识别。这里不写死 store——
            # 那等于把「再加上这张」和「这是新的一场比赛」当成同一件事。
            return await self.dispatch_command(
                client, channel, "photo", stripped, attachments, message
            )

        if self.is_allowed_for_command(
            channel.id, "photo"
        ) and self._has_pending_photo_questions(channel, stripped):
            self._log(f"pending_photo_dispatch channel_id={channel.id} command=photo")
            return await self.dispatch_command(
                client, channel, "photo", stripped, attachments, message
            )

        direct_route = self._route_from_direct_intent(
            stripped,
            self._allowed_natural_language_commands(channel.id),
        )
        if direct_route is not None:
            return await self.dispatch_command(
                client,
                channel,
                direct_route.command_name,
                direct_route.argument,
                attachments,
                message,
            )

        if self.is_allowed_for_command(
            channel.id, "running"
        ) and self._has_pending_running_questions(channel, stripped):
            return await self.dispatch_command(
                client, channel, "running", stripped, message=message
            )

        if self._main_agent_loop_enabled():
            await self._handle_ask(
                self._command_context(
                    client, channel, attachments=attachments, message=message
                ),
                stripped,
                client=client,
            )
            return True

        route = await self._route_natural_language(channel.id, stripped)
        if route is not None:
            return await self.dispatch_command(
                client,
                channel,
                route.command_name,
                route.argument,
                attachments,
                message,
            )

        if self._read_tools_for_channel(channel.id):
            return await self.dispatch_command(
                client, channel, "ask", stripped, attachments, message
            )

        if self.is_capabilities_channel(channel.id):
            await channel.send(
                "我没判断出要调用哪个能力。可以试试：\n"
                "!coros <问题>：生成运动报告\n"
                "!coros-list 最近 90 天：列出运动记录\n"
                "!coros-activity 1：分析列表中的第 1 条运动\n"
                "!running <问题>：基于跑步知识库回答\n"
                "!running-video <B站BV号或链接>：导入跑步视频知识\n"
                "!feel <感受>：记录运动感受\n"
                "!feelings：查看最近感受记录"
            )
            return True

        return False

    async def dispatch_web_text(
        self,
        client: object,
        channel: MessageChannel,
        content: str,
        allowed_commands: tuple[str, ...] = (
            "coros",
            "coros-tools",
            "coros-list",
            "coros-activity",
            "coros-pb",
            "running",
            "feelings",
            # 照片在网页也放行：能力层自己按 read_only 只开检索，
            # 保存和改标注在这条路上根本不会执行。
            "photo",
        ),
        # 放在 allowed_commands **之后**：调用方用位置传参传命令表，
        # 插在前面会把命令表当成语言，而且不会报错——只是模型收到一个
        # 看不懂的语言指令，然后一切照旧，最难查的那种。
        lang: str = "",
    ) -> NaturalLanguageRoute | None:
        stripped = content.strip()
        if not stripped:
            return None

        if stripped.startswith("!"):
            command_name, _, argument = stripped[1:].partition(" ")
            command_name = command_name.strip()
            argument = argument.strip()
            if command_name not in allowed_commands:
                await channel.send("这个网页入口不支持这个命令。")
                return None
            if not self._is_read_only_command(command_name):
                await channel.send("网页入口是只读的，写操作请在 Discord 里进行。")
                return None
            try:
                self._emit_command_trace(channel, command_name)
                handled = await self._registry.dispatch_command(
                    self._command_context(client, channel, read_only=True),
                    command_name,
                    argument,
                )
            except Exception as exc:
                await self._send_error(channel, f"执行 `{command_name}` 失败", exc)
                return NaturalLanguageRoute(command_name, argument, 1.0, "explicit")
            if handled:
                return NaturalLanguageRoute(command_name, argument, 1.0, "explicit")
            await channel.send("我没有找到这个命令。")
            return None

        direct_route = self._route_from_direct_intent(stripped, allowed_commands)
        if direct_route is not None:
            self._emit_command_trace(channel, direct_route.command_name)
            await self._registry.dispatch_command(
                self._command_context(client, channel, read_only=True),
                direct_route.command_name,
                direct_route.argument,
            )
            return direct_route

        if "photo" in allowed_commands and self._has_pending_photo_questions(
            channel, stripped
        ):
            self._log("web_pending_photo_rejected")
            await channel.send("网页入口不开放照片库的写入。请在 Discord 里补充照片信息。")
            return None

        if self._main_agent_loop_enabled():
            await self._handle_ask(
                self._command_context(client, channel, read_only=True),
                stripped,
                allowed_commands=allowed_commands,
                lang=lang,
            )
            return NaturalLanguageRoute("ask", stripped, 1.0, "main agent loop")

        if "running" in allowed_commands and self._has_pending_running_questions(
            channel, stripped
        ):
            await self._registry.dispatch_command(
                self._command_context(client, channel, read_only=True),
                "running",
                stripped,
            )
            return NaturalLanguageRoute("running", stripped, 1.0, "pending answer")

        try:
            route = await self._route_natural_language_from_allowed(
                -1,
                stripped,
                allowed_commands,
            )
        except Exception as exc:
            await self._send_error(channel, "自然语言路由失败", exc)
            return None

        if route is None:
            await channel.send(
                "我没判断出要调用哪个能力。可以直接试：\n"
                "!coros 分析我最近一次运动\n"
                "!coros-list 最近 90 天\n"
                "!running 我现在半马 1:40，全马 4:30，应该怎么练"
            )
            return None

        if not self._is_read_only_command(route.command_name):
            await channel.send("网页入口是只读的，写操作请在 Discord 里进行。")
            return None

        self._emit_command_trace(channel, route.command_name)
        try:
            await self._registry.dispatch_command(
                self._command_context(client, channel, read_only=True),
                route.command_name,
                route.argument,
            )
        except Exception as exc:
            await self._send_error(channel, f"执行 `{route.command_name}` 失败", exc)
        return route

    def _emit_trace_step(self, channel: MessageChannel, module: str, why: str = "") -> None:
        emit_step = getattr(channel, "trace_step", None)
        if not callable(emit_step):
            return
        try:
            emit_step(module_payload(module, why))
        except Exception:
            pass

    def _emit_command_trace(self, channel: MessageChannel, command_name: str) -> None:
        for module in command_modules(command_name):
            self._emit_trace_step(channel, module, f"{command_name} · {module}")

    def _has_pending_running_questions(self, channel: MessageChannel, content: str) -> bool:
        if not content:
            return False
        return bool(
            get_pending_questions(self._conversation_id(channel), RUNNING_COACH_TOPIC)
        )

    def _has_pending_photo_questions(self, channel: MessageChannel, content: str) -> bool:
        if not content:
            return False
        return bool(
            get_pending_questions(self._conversation_id(channel), PHOTO_MEMORY_TOPIC)
        )

    def _route_from_direct_intent(
        self,
        content: str,
        allowed_commands: tuple[str, ...],
    ) -> NaturalLanguageRoute | None:
        if "coros" in allowed_commands and self._looks_like_daily_coros_report(content):
            return NaturalLanguageRoute(
                "coros",
                content.strip(),
                1.0,
                "direct daily coros report",
            )
        return None

    def _looks_like_daily_coros_report(self, content: str) -> bool:
        text = content.strip().lower()
        if not text:
            return False
        if any(
            term in text
            for term in (
                "半马",
                "全马",
                "pb",
                "个人最好",
                "最好成绩",
                "训练计划",
                "计划",
                "知识库",
                "书",
                "视频",
            )
        ):
            return False
        time_terms = (
            "今天",
            "今日",
            "这次",
            "刚才",
            "刚刚",
            "刚跑完",
            "跑完",
            "最近一次",
            "最新",
            "最近的",
        )
        activity_terms = ("训练", "运动", "跑步", "跑", "run", "workout")
        report_terms = (
            "怎么样",
            "如何",
            "分析",
            "复盘",
            "报告",
            "总结",
            "评价",
            "下一次",
            "下次",
            "怎么练",
        )
        return (
            any(term in text for term in time_terms)
            and any(term in text for term in activity_terms)
            and any(term in text for term in report_terms)
        )

    async def _route_natural_language(
        self,
        channel_id: int,
        content: str,
    ) -> NaturalLanguageRoute | None:
        if not self._natural_language_routing_enabled() or not content:
            return None
        allowed_commands = self._allowed_natural_language_commands(channel_id)
        if not allowed_commands:
            return None
        return await self._route_natural_language_from_allowed(
            channel_id,
            content,
            allowed_commands,
        )

    async def _route_natural_language_from_allowed(
        self,
        channel_id: int,
        content: str,
        allowed_commands: tuple[str, ...],
    ) -> NaturalLanguageRoute | None:
        try:
            route = await asyncio.wait_for(
                complete_json(
                    ROUTER_SYSTEM_PROMPT,
                    self._build_router_prompt(content, allowed_commands),
                ),
                timeout=self._natural_language_timeout_seconds(),
            )
            self._log(
                "natural_language_route_raw "
                f"channel_id={channel_id} allowed={allowed_commands} route={route}"
            )
        except Exception as exc:
            raise RuntimeError(str(exc) or exc.__class__.__name__) from exc

        parsed_route = self._route_from_llm_response(route, content, allowed_commands)
        if parsed_route is None:
            self._log(
                "natural_language_route_rejected "
                f"channel_id={channel_id} allowed={allowed_commands} route={route}"
            )
        return parsed_route

    def _build_router_prompt(
        self,
        content: str,
        allowed_commands: tuple[str, ...],
    ) -> str:
        command_descriptions = {
            "coros": "生成 COROS 单次运动报告或训练复盘。",
            "coros-tools": "列出 COROS MCP 当前提供的工具。",
            "coros-list": "列出 COROS 运动记录摘要，供用户选择某一条。",
            "coros-activity": "根据 coros-list 的序号或 ID，分析用户选择的单条 COROS 运动。",
            "coros-pb": "查看 COROS 自动记录的个人 PB。",
            "coros-fit-sync": "把 COROS 原始 FIT 文件下载归档到服务器。",
            "running": (
                "基于跑步知识库回答训练方法、计划、成绩瓶颈问题，"
                "也接收年龄、身高、体重、成绩、目标、跑量、比赛问题等长期档案补充。"
            ),
            "running-video": "把 B站跑步长视频字幕导入跑步知识库。",
            "feel": "记录运动后的主观感受，例如 RPE、腿沉、酸痛、疲劳。",
            "feelings": "查看最近记录的运动感受。",
            "ask": "开放式提问：主 Agent 自己查数据和知识库，然后直接回答。",
        }
        allowed_text = "\n".join(
            f"- {name}: {command_descriptions[name]}" for name in allowed_commands
        )
        return f"""
Allowed commands in this channel:
{allowed_text}

User message:
{content}
""".strip()

    def _route_from_llm_response(
        self,
        route: dict[str, Any],
        original_content: str,
        allowed_commands: tuple[str, ...],
    ) -> NaturalLanguageRoute | None:
        command_name = route.get("command")
        if not isinstance(command_name, str):
            return None
        command_name = command_name.strip()
        if command_name == "none" or command_name not in allowed_commands:
            return None

        confidence = self._parse_confidence(route.get("confidence"))
        if confidence < self._natural_language_confidence_threshold():
            return None

        argument = route.get("argument")
        if not isinstance(argument, str):
            argument = ""
        argument = argument.strip()

        if command_name in {
            "ask",
            "coros",
            "coros-activity",
            "coros-fit-sync",
            "running",
            "feel",
            "photo",
        } and not argument:
            argument = original_content
        if command_name in {"coros-tools", "coros-pb", "feelings"}:
            argument = ""
        if command_name == "running-video" and not self._valid_running_video_argument(
            argument
        ):
            return None

        if command_name == "photo" and not self._valid_photo_argument(argument):
            return None

        reason = route.get("reason")
        return NaturalLanguageRoute(
            command_name,
            argument,
            confidence,
            reason if isinstance(reason, str) else "",
        )

    def _command_tool(
        self,
        command: Any,
        client: object,
        channel: MessageChannel,
        read_only: bool,
        attachments: tuple[RuntimeAttachment, ...],
        message: object | None,
    ) -> Tool:
        async def handler(argument: str = "") -> str:
            argument = argument.strip()
            if read_only and not self._is_read_only_command(command.name):
                return f"{command.name} 在只读入口不可用，这个操作要在 Discord 里做。"

            buffer: list[str] = []

            async def capture(text: str) -> None:
                if text and text.strip():
                    buffer.append(text.strip())

            async def notify(text: str) -> None:
                if text and text.strip():
                    await channel.send(sanitize_output(text.strip()))

            context = CommandContext(
                client=client,
                channel=channel,
                send=capture,
                send_chunks=capture,
                notify=notify,
                # 图片绕过工具缓冲区直发给用户。入口不支持就是 None，
                # 能力层会退回把链接写进文本。
                show_images=getattr(channel, "show_images", None),
                message=message,
                conversation_id=self._conversation_id(channel),
                read_only=read_only,
                attachments=attachments,
            )
            await command.handler(context, argument)
            return "\n\n".join(buffer) or f"{command.name} 执行完成，没有输出。"

        description = command.description
        if read_only and command.read_only_description:
            description = command.read_only_description
        if command.argument_hint:
            description = f"{description}。参数：{command.argument_hint}"

        return Tool(
            name=command.name,
            description=description,
            parameters={
                "type": "object",
                "properties": {
                    "argument": {
                        "type": "string",
                        "description": command.argument_hint or "传给这个动作的参数，可留空",
                    }
                },
                "required": [],
            },
            handler=handler,
            writes=command.writes,
            returns_untrusted=command.returns_untrusted,
        )

    def _loop_tools(
        self,
        client: object,
        channel: MessageChannel,
        read_only: bool = False,
        attachments: tuple[RuntimeAttachment, ...] = (),
        message: object | None = None,
        allowed_commands: tuple[str, ...] | None = None,
    ) -> tuple[Tool, ...]:
        by_allowlist = allowed_commands is not None
        tools: list[Tool] = (
            list(self._all_read_tools())
            if by_allowlist
            else list(self._read_tools_for_channel(channel.id))
        )

        for channel_env_name, command in self._registry.tool_commands():
            if by_allowlist:
                if command.name not in allowed_commands:
                    continue
            elif channel_env_name is not None and not self._is_allowed_channel(
                channel.id, channel_env_name
            ):
                continue
            if read_only and command.writes and not command.read_only_safe:
                continue
            tools.append(
                self._command_tool(
                    command, client, channel, read_only, attachments, message
                )
            )
        return tuple(tools)

    def _all_read_tools(self) -> tuple[Any, ...]:
        return tuple(tool for _, tool in self._registry.read_tools())

    def _read_tools_for_channel(self, channel_id: int) -> tuple[Any, ...]:
        tools = []
        for channel_env_name, tool in self._registry.read_tools():
            if channel_env_name is None or self._is_allowed_channel(
                channel_id, channel_env_name
            ):
                tools.append(tool)
        return tuple(tools)

    async def _handle_ask(
        self,
        context: CommandContext,
        question: str,
        client: object | None = None,
        allowed_commands: tuple[str, ...] | None = None,
        lang: str = "",
    ) -> None:
        question = question.strip()
        if not question:
            await context.send("你想问什么？")
            return

        emit_step = getattr(context.channel, "trace_step", None)

        def emit(payload: dict[str, Any]) -> None:
            if not callable(emit_step):
                return
            try:
                emit_step(payload)
            except Exception:
                pass

        emit({"type": "trace_step", "module": "entry", "label": "入口"})
        emit({"type": "trace_step", "module": "loop", "label": "主 Agent 循环"})

        tools = self._loop_tools(
            client if client is not None else context.client,
            context.channel,
            read_only=context.read_only,
            attachments=context.attachments,
            message=context.message,
            allowed_commands=allowed_commands,
        )
        if not tools:
            await context.send("这个入口没有可用的能力。")
            return

        async def on_tool(name: str, why: str) -> None:
            if context.verbose_progress:
                await context.progress(why.strip() or f"正在调用 {name}")
            emit(step_payload(name, why))

        try:
            answer = await answer_open_question(
                question,
                tools,
                conversation_id=context.conversation_id,
                log=self._log,
                on_tool=on_tool,
                lang=lang,
            )
        except Exception as exc:
            await self._send_error(context.channel, "回答失败", exc)
            self._log(f"ask_failed error={exc}")
            return

        emit({"type": "trace_step", "module": "answer", "label": "生成回答"})
        await context.send_chunks(answer)

    def _main_agent_loop_enabled(self) -> bool:
        value = os.getenv("MAIN_AGENT_LOOP_ENABLED", "true")
        return value.lower() not in {"0", "false", "no", "off"}

    def _natural_language_routing_enabled(self) -> bool:
        value = os.getenv("NATURAL_LANGUAGE_ROUTING_ENABLED", "true")
        return value.lower() not in {"0", "false", "no", "off"}

    def _natural_language_confidence_threshold(self) -> float:
        value = os.getenv("NATURAL_LANGUAGE_ROUTING_CONFIDENCE", "0.7")
        try:
            return float(value)
        except ValueError:
            return 0.7

    def _natural_language_timeout_seconds(self) -> int:
        value = os.getenv("NATURAL_LANGUAGE_ROUTING_TIMEOUT_SECONDS", "20")
        try:
            return max(int(value), 1)
        except ValueError:
            return 20

    def _parse_confidence(self, value: object) -> float:
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return 0.0
        return 0.0

    def _valid_photo_argument(self, argument: str) -> bool:
        """照片命令收原话即可，意图由能力层判断。

        原来要求必须是 store/search/update 前缀，等于让路由器替照片能力
        决定动作，而它既看不到现有分组也看不到附件，判断不了追加还是新建。
        """
        return bool(argument.strip())

    def _valid_running_video_argument(self, argument: str) -> bool:
        return bool(argument and ("BV" in argument or "bilibili.com" in argument))

    def _is_read_only_command(self, command_name: str) -> bool:
        # photo 在这里放行，因为**能力层自己按 read_only 只开检索**——
        # 保存和改标注在只读上下文里根本不会执行。
        # 在这一层按命令名一刀切会连「查照片」也一起挡掉。
        return command_name not in {"feel", "running-video", "coros-fit-sync"}

    def _allowed_natural_language_commands(self, channel_id: int) -> tuple[str, ...]:
        commands: list[str] = []
        for command_name in (
            "coros",
            "running",
            "coros-list",
            "coros-activity",
            "coros-pb",
            "coros-fit-sync",
            "running-video",
            "feel",
            "feelings",
            "photo",
        ):
            if self.is_allowed_for_command(channel_id, command_name):
                commands.append(command_name)
        if self._read_tools_for_channel(channel_id):
            commands.append("ask")
        return tuple(commands)

    def _is_allowed_channel(self, channel_id: int, env_name: str) -> bool:
        configured_id = os.getenv(env_name)
        return configured_id is not None and str(channel_id) == configured_id

    def _log(self, message: str) -> None:
        log_event("orchestrator", detail=message)

    def _command_context(
        self,
        client: object,
        channel: MessageChannel,
        read_only: bool = False,
        attachments: tuple[RuntimeAttachment, ...] = (),
        message: object | None = None,
    ) -> CommandContext:
        async def send_text(text: str) -> None:
            await channel.send(sanitize_output(text))

        async def send_chunks(text: str) -> None:
            await self._send_chunks(channel, sanitize_output(text))

        return CommandContext(
            client=client,
            channel=channel,
            send=send_text,
            send_chunks=send_chunks,
            notify=getattr(channel, "notify", None) or send_text,
            show_images=getattr(channel, "show_images", None),
            verbose_progress=bool(getattr(channel, "verbose_progress", False)),
            message=message,
            conversation_id=self._conversation_id(channel),
            read_only=read_only,
            attachments=attachments,
        )

    def _conversation_id(self, channel: MessageChannel) -> str:
        conversation_id = getattr(channel, "conversation_id", None)
        if isinstance(conversation_id, str) and conversation_id.strip():
            return conversation_id.strip()
        return f"channel:{channel.id}"

    async def _send_chunks(self, channel: MessageChannel, text: str) -> None:
        chunk_size = 1800
        for start in range(0, len(text), chunk_size):
            await channel.send(text[start : start + chunk_size])

    async def _send_error(
        self,
        channel: MessageChannel,
        title: str,
        exc: Exception,
    ) -> None:
        error_text = str(exc).strip() or exc.__class__.__name__
        if len(error_text) > 500:
            error_text = f"{error_text[:500].rstrip()}..."
        await channel.send(f"{title}。\n```text\n{error_text}\n```")


_orchestrator: MainAgentOrchestrator | None = None


def get_orchestrator() -> MainAgentOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = MainAgentOrchestrator()
    return _orchestrator
