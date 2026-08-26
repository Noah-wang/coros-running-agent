import os

import discord
from discord import app_commands

from src.orchestrator import get_orchestrator
from src.runtime.capability import RuntimeAttachment


# 拿本地变量
def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is missing. Add it to .env.")
    return value


def _runtime_attachments(message: discord.Message) -> tuple[RuntimeAttachment, ...]:
    attachments: list[RuntimeAttachment] = []
    for item in message.attachments:
        async def save(target, attachment=item) -> None:
            await attachment.save(target)

        attachments.append(
            RuntimeAttachment(
                filename=item.filename,
                content_type=item.content_type,
                url=item.url,
                size=item.size,
                save=save,
            )
        )
    return tuple(attachments)


async def _dispatch_interaction_command(
    interaction: discord.Interaction,
    client: discord.Client,
    command_name: str,
    argument: str,
    start_message: str,
) -> None:
    orchestrator = get_orchestrator()
    if interaction.channel_id is None or not orchestrator.is_allowed_for_command(
        interaction.channel_id,
        command_name,
    ):
        await interaction.response.send_message(
            "这个命令只能在指定频道使用。", ephemeral=True
        )
        return

    try:
        await interaction.response.send_message(start_message)
        if interaction.channel is not None:
            # 命令要走 LLM、MCP 和知识库检索，耗时通常十几秒，
            # 期间亮出 Discord 原生的「正在输入」，避免看起来像没反应。
            async with interaction.channel.typing():
                await orchestrator.dispatch_command(
                    client,
                    interaction.channel,
                    command_name,
                    argument,
                )
    except Exception as exc:
        error_text = str(exc).strip() or exc.__class__.__name__
        if len(error_text) > 500:
            error_text = f"{error_text[:500].rstrip()}..."
        message = f"执行 `{command_name}` 失败。\n```text\n{error_text}\n```"
        if interaction.response.is_done():
            await interaction.followup.send(message)
        else:
            await interaction.response.send_message(message, ephemeral=True)


# 创建discord客户端
def create_discord_client() -> discord.Client:
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    # 机器人上线
    @client.event
    async def on_ready() -> None:
        await tree.sync()
        get_orchestrator().run_startup_handlers(client)
        print(f"Logged in as {client.user}")

    # coros命令
    @tree.command(name="coros", description="生成 COROS 运动报告")
    @app_commands.describe(
        request="你想分析什么，例如：最近一次跑步、明天是否适合高强度"
    )
    async def coros_command(
        interaction: discord.Interaction, request: str = ""
    ) -> None:
        if not request:
            request = "分析我最近一次运动，重点看配速、心率、恢复和下一次训练建议。"

        await _dispatch_interaction_command(
            interaction,
            client,
            "coros",
            request,
            "收到，开始生成 COROS 运动报告。",
        )

    # coros工具命令
    @tree.command(name="coros-tools", description="列出 COROS MCP 当前提供的工具")
    async def coros_tools_command(interaction: discord.Interaction) -> None:
        await _dispatch_interaction_command(
            interaction,
            client,
            "coros-tools",
            "",
            "正在读取 COROS MCP 工具列表...",
        )

    @tree.command(name="coros-list", description="列出 COROS 运动记录摘要")
    @app_commands.describe(
        days="最近多少天，默认 90；想查全部可在文字频道发送 !coros-list all",
        limit="最多显示多少条，默认 20",
    )
    async def coros_list_command(
        interaction: discord.Interaction,
        days: int = 90,
        limit: int = 20,
    ) -> None:
        await _dispatch_interaction_command(
            interaction,
            client,
            "coros-list",
            f"days={days} limit={limit}",
            "正在读取 COROS 运动记录列表...",
        )

    @tree.command(name="coros-activity", description="选择一条 COROS 运动记录生成报告")
    @app_commands.describe(
        selection="列表序号或 labelId，例如：1",
        question="可选：你想重点分析什么",
    )
    async def coros_activity_command(
        interaction: discord.Interaction,
        selection: str,
        question: str = "",
    ) -> None:
        await _dispatch_interaction_command(
            interaction,
            client,
            "coros-activity",
            f"{selection} {question}".strip(),
            "正在读取所选 COROS 运动并生成报告...",
        )

    @tree.command(name="coros-pb", description="查看 COROS 自动记录的个人 PB")
    async def coros_pb_command(interaction: discord.Interaction) -> None:
        await _dispatch_interaction_command(
            interaction,
            client,
            "coros-pb",
            "",
            "正在读取 COROS 自动 PB。",
        )

    # 跑步书籍回答命令
    @tree.command(name="running-ask", description="基于已导入跑步书籍回答训练问题")
    @app_commands.describe(question="你的跑步训练问题")
    async def running_ask_command(
        interaction: discord.Interaction, question: str
    ) -> None:
        await _dispatch_interaction_command(
            interaction,
            client,
            "running",
            question,
            "收到，开始检索跑步书籍。",
        )

    @tree.command(name="running-video", description="把 B站跑步长视频导入知识库")
    @app_commands.describe(video="B站 BV号或视频链接")
    async def running_video_command(
        interaction: discord.Interaction, video: str
    ) -> None:
        await _dispatch_interaction_command(
            interaction,
            client,
            "running-video",
            video,
            "收到，开始导入跑步视频知识。",
        )

    @tree.command(name="feel", description="记录一次运动后的主观感受")
    @app_commands.describe(note="例如：今天腿很沉，RPE 7，左膝有点紧")
    async def feel_command(interaction: discord.Interaction, note: str) -> None:
        await _dispatch_interaction_command(
            interaction,
            client,
            "feel",
            note,
            "正在记录你的运动感受。",
        )

    @tree.command(name="feelings", description="查看最近记录的运动感受")
    async def feelings_command(interaction: discord.Interaction) -> None:
        await _dispatch_interaction_command(
            interaction,
            client,
            "feelings",
            "",
            "正在读取最近记录的运动感受。",
        )

    @tree.command(name="capabilities", description="查看当前已加载的能力")
    async def capabilities_command(interaction: discord.Interaction) -> None:
        orchestrator = get_orchestrator()
        if interaction.channel_id is None or not orchestrator.is_capabilities_channel(
            interaction.channel_id
        ):
            await interaction.response.send_message(
                "这个命令只能在指定频道使用。", ephemeral=True
            )
            return

        await interaction.response.send_message(orchestrator.describe_capabilities())

    # 监听消息
    @client.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return

        orchestrator = get_orchestrator()
        try:
            # 只在能力频道亮「正在输入」。其他频道 dispatch_text 会立刻返回，
            # 亮指示器既没意义，还会让 bot 看起来在到处打字。
            if orchestrator.is_capabilities_channel(message.channel.id):
                async with message.channel.typing():
                    await orchestrator.dispatch_text(
                        client,
                        message.channel,
                        message.content,
                        _runtime_attachments(message),
                        message,
                    )
            else:
                await orchestrator.dispatch_text(
                    client,
                    message.channel,
                    message.content,
                    _runtime_attachments(message),
                    message,
                )
        except Exception as exc:
            error_text = str(exc).strip() or exc.__class__.__name__
            if len(error_text) > 500:
                error_text = f"{error_text[:500].rstrip()}..."
            await message.channel.send(f"处理消息失败。\n```text\n{error_text}\n```")

    return client


def run_discord_bot() -> None:
    token = _required_env("DISCORD_BOT_TOKEN")
    client = create_discord_client()
    client.run(token)
