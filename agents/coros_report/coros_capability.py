import discord

from agents.coros_report.agent import list_available_coros_tools
from agents.coros_report.graph import generate_coros_graph_report
from agents.coros_report.auto_report import check_and_send_coros_auto_report, latest_coros_activity, register_coros_auto_report
from agents.coros_report.activity_browser import (
    generate_selected_activity_report_for_conversation,
    list_activity_records,
    query_activity_records,
)
from agents.coros_report.feelings import list_recent_feelings, record_feeling
from agents.coros_report.fit_archive import archive_fit_for_activities, render_route_map_for_activity
from agents.coros_report.knowledge import answer_running_question
from agents.coros_report.coros_read_tools import COROS_READ_TOOLS
from agents.coros_report.personal_bests import format_personal_bests
from src.runtime.capability import Capability, CommandContext, TextCommand
from src.runtime.knowledge_sources import add_source, format_sources
from agents.coros_report.video_knowledge import import_running_video_knowledge


DEFAULT_REPORT_REQUEST = "分析我最近一次运动，重点看配速、心率、恢复和下一次训练建议。"


async def _coros_report(context: CommandContext, argument: str) -> None:
    request = argument.strip() or DEFAULT_REPORT_REQUEST
    await context.progress("正在读取 COROS 数据并生成报告...")
    try:
        report = await generate_coros_graph_report(request)
        await context.send_chunks(report)
        activity = await latest_coros_activity()
        if activity is not None:
            await _send_route_map(context, activity)
    except Exception as exc:
        await context.send(f"生成 COROS 报告失败：{exc}")


async def _coros_tools(context: CommandContext, _: str) -> None:
    await context.progress("正在读取 COROS MCP 工具列表...")
    try:
        tools = await list_available_coros_tools()
        await context.send_chunks(tools)
    except Exception as exc:
        await context.send(f"读取 COROS 工具失败：{exc}")


async def _coros_list(context: CommandContext, argument: str) -> None:
    await context.progress("正在读取 COROS 运动记录列表...")
    try:
        records = await list_activity_records(argument)
        await context.send_chunks(records)
    except Exception as exc:
        await context.send(f"读取 COROS 运动记录失败：{exc}")


async def _coros_activity(context: CommandContext, argument: str) -> None:
    await context.progress("正在读取所选 COROS 运动详情并生成报告...")
    try:
        result = await generate_selected_activity_report_for_conversation(
            argument,
            context.conversation_id,
        )
        await context.send_chunks(result.report)
        if result.activity is not None:
            await _send_route_map(context, result.activity)
    except Exception as exc:
        await context.send(f"生成所选 COROS 运动报告失败：{exc}")


async def _coros_pb(context: CommandContext, _: str) -> None:
    await context.send(format_personal_bests())


async def _coros_fit_sync(context: CommandContext, argument: str) -> None:
    if context.read_only:
        await context.send("网页入口不开放 FIT 文件归档。")
        return

    query = argument.strip() or "最近 30 天"
    await context.progress(f"正在同步 COROS FIT 文件：{query}")
    try:
        records, _, label = await query_activity_records(query)
        if not records:
            await context.send(f"没有查到 {label} 的 COROS 运动记录。")
            return
        results = await archive_fit_for_activities(records)
        archived = sum(1 for result in results if result.paths)
        downloaded = sum(1 for result in results if result.downloaded)
        await context.send(
            f"FIT 同步完成：{label}，检查 {len(results)} 条，"
            f"已有或已归档 {archived} 条，本次新下载 {downloaded} 条。"
        )
    except Exception as exc:
        await context.send(f"同步 FIT 文件失败：{exc}")


async def _send_route_map(context: CommandContext, activity: dict) -> None:
    if context.read_only:
        return
    try:
        result = await render_route_map_for_activity(activity)
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        if "MAPBOX_ACCESS_TOKEN" not in message:
            await context.send(f"路线图生成失败：{message}")
        return

    if result.path is None:
        return

    await context.channel.send("本次室外运动路线图：", file=discord.File(result.path))


async def _running_ask(context: CommandContext, argument: str) -> None:
    question = argument.strip()
    if not question:
        await context.send("请写你的跑步训练问题。")
        return

    await context.progress("正在检索跑步书籍并生成回答...")
    try:
        answer = await answer_running_question(
            question, context.conversation_id, read_only=context.read_only
        )
        await context.send_chunks(answer)
    except Exception as exc:
        await context.send(f"回答跑步问题失败：{exc}")


async def _running_video(context: CommandContext, argument: str) -> None:
    if context.read_only:
        await context.send("这个入口是只读的，导入知识库请在 Discord 里操作。")
        return

    video_input = argument.strip()
    if not video_input:
        await context.send("请提供 B站 BV号或视频链接。")
        return

    await context.progress("正在抓取 B站字幕，并导入跑步知识库...")
    try:
        result = await import_running_video_knowledge(video_input)
        await context.send_chunks(result)
    except Exception as exc:
        await context.send(f"导入跑步视频失败：{exc}")


async def _knowledge_source(context: CommandContext, argument: str) -> None:
    """订阅一个 UP 主，之后定时任务会把他的视频字幕导入知识库。"""
    if context.read_only:
        await context.send("这个入口是只读的，添加知识来源请在 Discord 里操作。")
        return

    text = argument.strip()
    if not text or text in {"list", "列表"}:
        await context.send(format_sources())
        return

    # 分类可以写在链接后面：「<链接> shoes」。没写就按跑鞋处理——
    # 目前订阅的绝大多数是跑鞋测评，让默认值贴近实际用法。
    parts = text.split()
    category = "shoes"
    for part in parts:
        if part in {"shoes", "training", "跑鞋", "训练"}:
            category = {"跑鞋": "shoes", "训练": "training"}.get(part, part)
    await context.send(add_source(text, category))


async def _record_feeling(context: CommandContext, argument: str) -> None:
    if context.read_only:
        await context.send("这个入口是只读的，记录感受请在 Discord 里操作。")
        return

    result = await record_feeling(argument)
    await context.send(result)


async def _list_feelings(context: CommandContext, _: str) -> None:
    await context.send(list_recent_feelings())


async def _auto_check(context: CommandContext, _: str) -> None:
    await context.progress("正在检查是否有新的 COROS 运动...")
    result = await check_and_send_coros_auto_report(
        context.client,
        notify_no_change=True,
        send_on_first_run=True,
    )
    if (
        result.startswith("COROS auto report")
        and "no new activity" not in result
        and not result.endswith("sent.")
    ):
        await context.send(result)


async def _auto_report(context: CommandContext, _: str) -> None:
    await context.progress("正在对最新一条 COROS 运动生成自动报告...")
    result = await check_and_send_coros_auto_report(
        context.client,
        send_on_first_run=True,
        force_send=True,
    )
    if result.startswith("COROS auto report") and not result.endswith("sent."):
        await context.send(result)


def build_coros_capability() -> Capability:
    return Capability(
        name="coros-report",
        description="读取 COROS 运动数据，生成训练报告，回答跑步问题，并记录主观感受。",
        channel_env_name="DISCORD_RUNNING_CHANNEL_ID",
        read_tools=COROS_READ_TOOLS,
        text_commands=(
            TextCommand(
                "coros",
                "生成 COROS 运动报告",
                _coros_report,
                argument_hint=(
                    "用户的原话。默认分析最新一次运动，"
                    "想分析指定的某一条要改用 coros-activity。"
                ),
            ),
            TextCommand(
                "coros-tools",
                "列出 COROS MCP 工具",
                _coros_tools,
                # 运维用的自检命令，模型没有理由主动调它
                expose_as_tool=False,
            ),
            TextCommand(
                "coros-list",
                "列出 COROS 运动记录摘要，输出带编号，供用户挑选某一条",
                _coros_list,
                aliases=("coros-activities",),
                argument_hint="时间范围或条数，例如「最近 30 天」，可留空",
            ),
            TextCommand(
                "coros-activity",
                "分析用户从列表里选中的某一条运动并生成报告",
                _coros_activity,
                aliases=("coros-select",),
                argument_hint="用户的原话，例如「分析第 1 条」「第 3 条重点看心率」",
            ),
            TextCommand(
                "coros-pb",
                "查看 COROS 自动记录的个人 PB",
                _coros_pb,
                aliases=("pb", "personal-best", "personal-bests"),
            ),
            TextCommand(
                "coros-fit-sync",
                "把 COROS 原始 FIT 文件下载归档到服务器",
                _coros_fit_sync,
                aliases=("fit-sync",),
                # 会往服务器磁盘写文件
                writes=True,
                argument_hint="时间范围和条数，可留空",
            ),
            TextCommand(
                "running",
                "基于跑步书籍和视频知识库回答训练理论问题，也接收用户补充的长期档案",
                _running_ask,
                # 会把用户提到的年龄、成绩、目标写进长期档案，
                # 但 knowledge 内部会按 read_only 裁掉那个写工具
                writes=True,
                read_only_safe=True,
                # 回答里会带上检索到的书籍原文和视频字幕
                returns_untrusted=True,
                argument_hint="用户的原话",
            ),
            TextCommand(
                "running-video",
                "把 B站跑步长视频字幕导入跑步知识库",
                _running_video,
                aliases=("running-import-video",),
                writes=True,
                argument_hint="B站链接或 BV 号，必须是用户真的给出来的",
            ),
            TextCommand(
                "knowledge-source",
                "订阅一个 B 站 UP 主，定时把他的视频字幕导入知识库",
                _knowledge_source,
                aliases=("subscribe", "订阅"),
                writes=True,
                argument_hint=(
                    "UP 主空间链接或数字 ID，后面可以跟分类 shoes 或 training；"
                    "留空或写 list 表示查看当前订阅"
                ),
            ),
            TextCommand(
                "feel",
                "记录运动后的主观感受",
                _record_feeling,
                aliases=("feeling",),
                writes=True,
                argument_hint="用户描述的感受原话",
            ),
            TextCommand(
                "feelings",
                "查看最近记录的运动感受",
                _list_feelings,
                aliases=("feeling-list",),
            ),
            TextCommand(
                "coros-auto-check",
                "手动检查是否有新的 COROS 运动",
                _auto_check,
                writes=True,
                expose_as_tool=False,
            ),
            TextCommand(
                "coros-auto-report",
                "强制对最新一条 COROS 运动生成报告",
                _auto_report,
                writes=True,
                expose_as_tool=False,
            ),
        ),
        startup_handlers=(register_coros_auto_report,),
    )
