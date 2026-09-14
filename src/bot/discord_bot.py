import os

import discord
from discord import app_commands

from agents.coros_report.auth_flow import (
    complete_coros_auth_flow,
    is_coros_callback_url,
    start_coros_auth_flow,
)
from src.orchestrator import get_orchestrator
from src.runtime.capability import RuntimeAttachment
from src.runtime.identity import multi_tenant_enabled, resolve_external_tenant
from src.runtime.tenant import TenantContext, tenant_scope


# 等用户去浏览器点授权的时间。给足，但不能无限等——
# 这个协程挂在 on_message 上，一直不返回会占着这条消息的处理。
AUTH_WAIT_SECONDS = 300
AUTH_POLL_SECONDS = 3


def _log_connect(detail: str) -> None:
    print(f"[coros-connect] {detail}", flush=True)


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


def _discord_workspace_id(value: object) -> str:
    guild_id = getattr(value, "guild_id", None)
    if guild_id is None:
        guild = getattr(value, "guild", None)
        guild_id = getattr(guild, "id", None)
    return str(guild_id or "")


def _message_tenant_context(message: discord.Message) -> TenantContext | None:
    return resolve_external_tenant(
        "discord",
        str(message.author.id),
        workspace_id=_discord_workspace_id(message),
        surface="discord",
    )


def _interaction_tenant_context(interaction: discord.Interaction) -> TenantContext | None:
    return resolve_external_tenant(
        "discord",
        str(interaction.user.id),
        workspace_id=_discord_workspace_id(interaction),
        surface="discord",
    )


def _looks_like_coros_connect_request(text: str) -> bool:
    normalized = text.strip().casefold()
    return normalized in {
        "!coros-connect",
        "!connect-coros",
        "连接coros",
        "连接 coros",
        "连接高驰",
        "重新连接coros",
        "重新连接 coros",
        "重新连接高驰",
        "coros授权",
        "coros 授权",
        "高驰授权",
    }


async def _handle_coros_connect_message(message: discord.Message) -> bool:
    orchestrator = get_orchestrator()
    if not orchestrator.is_discord_channel_allowed(
        message.channel.id, getattr(message.channel, "parent_id", None)
    ):
        return False

    text = message.content.strip()
    if is_coros_callback_url(text):
        await message.channel.send("收到 COROS 回调链接，正在完成服务器授权...")
        try:
            await complete_coros_auth_flow(text)
        except Exception as exc:
            await message.channel.send(f"COROS 授权失败：{str(exc).strip() or exc.__class__.__name__}")
            return True
        await message.channel.send(
            "COROS 授权完成。现在可以发送 `!coros-auto-report` 测试自动运动报告。"
        )
        return True

    if not _looks_like_coros_connect_request(text):
        return False

    await message.channel.send("正在生成 COROS 授权链接...")

    # 首选公网回调那条路：用户点一下就完事，不用面对一个打不开的
    # localhost 页面再复制粘贴。需要配了公网域名才能用。
    try:
        import asyncio as _asyncio

        from src.integrations.coros_oauth import start as start_public_oauth
        from src.integrations.coros_oauth import take_completion

        url, state = await _asyncio.to_thread(start_public_oauth)
    except Exception as exc:
        _log_connect(f"public_oauth_unavailable reason={exc}")
    else:
        await message.channel.send(
            "请点击下面的链接授权 COROS：\n"
            f"{url}\n\n"
            "授权完成后浏览器会显示「COROS 已连接」，我在这里等着回你。"
        )
        # 授权是在 **web 进程**里完成的，bot 这边收不到任何事件。
        # 不轮询的话用户点完回到频道只看到一片安静，会以为失败了。
        deadline = _asyncio.get_running_loop().time() + AUTH_WAIT_SECONDS
        while _asyncio.get_running_loop().time() < deadline:
            await _asyncio.sleep(AUTH_POLL_SECONDS)
            if await _asyncio.to_thread(take_completion, state) is not None:
                await message.channel.send(
                    "COROS 授权完成，已经连上了。可以发 `!coros-auto-report` 试一下。"
                )
                _log_connect("public_oauth_completed")
                return True
        await message.channel.send(
            "等了一会儿没等到授权完成。如果你已经点过并看到「COROS 已连接」，"
            "那其实已经成功了，直接用就行；否则发一次 `连接coros` 重新开始。"
        )
        return True

    # 兜底：公网回调不可用（没配域名，或 mcp-remote 存储格式变了）时，
    # 退回原来那套粘贴流程。体验差，但至少能用。
    try:
        result = await start_coros_auth_flow()
    except Exception as exc:
        await message.channel.send(f"生成 COROS 授权链接失败：{str(exc).strip() or exc.__class__.__name__}")
        return True

    if result.already_connected:
        await message.channel.send("COROS 已经处于连接状态，可以直接使用 `!coros-auto-report` 测试。")
        return True

    await message.channel.send(
        "请点击下面的链接授权 COROS：\n"
        f"{result.authorization_url}\n\n"
        f"授权后如果浏览器跳到 `localhost:{result.callback_port}` 并显示打不开，"
        "请把地址栏里的完整链接复制回来发到这里，我会自动完成服务器授权。"
    )
    return True


async def _dispatch_interaction_command(
    interaction: discord.Interaction,
    client: discord.Client,
    command_name: str,
    argument: str,
    start_message: str,
) -> None:
    orchestrator = get_orchestrator()
    if (
        interaction.channel_id is None
        or not orchestrator.is_discord_channel_allowed(
            interaction.channel_id, getattr(interaction.channel, "parent_id", None)
        )
        or not orchestrator.is_allowed_for_command(
            orchestrator.permission_channel_id_for(interaction.channel)
            if interaction.channel is not None
            else interaction.channel_id,
            command_name,
        )
    ):
        await interaction.response.send_message(
            "这个命令只能在指定频道使用。", ephemeral=True
        )
        return

    tenant_context = _interaction_tenant_context(interaction)
    if tenant_context is None:
        await interaction.response.send_message(
            "你的账号还没有绑定到 COROS Agent，或订阅当前不可用。请联系管理员完成开通。",
            ephemeral=True,
        )
        return

    with tenant_scope(tenant_context):
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

    @tree.command(name="coros-sleep-report", description="生成 COROS 睡眠与恢复晨报")
    async def coros_sleep_report_command(interaction: discord.Interaction) -> None:
        await _dispatch_interaction_command(
            interaction,
            client,
            "coros-sleep-report",
            "",
            "正在生成 COROS 睡眠与恢复晨报。",
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

        if _interaction_tenant_context(interaction) is None:
            await interaction.response.send_message(
                "你的账号还没有绑定到 COROS Agent。", ephemeral=True
            )
            return

        await interaction.response.send_message(orchestrator.describe_capabilities())

    # 监听消息
    @client.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return

        orchestrator = get_orchestrator()
        if not orchestrator.is_discord_channel_allowed(
            message.channel.id, getattr(message.channel, "parent_id", None)
        ):
            return

        tenant_context = _message_tenant_context(message)
        if tenant_context is None:
            if multi_tenant_enabled():
                await message.channel.send(
                    "你的账号还没有绑定到 COROS Agent，或订阅当前不可用。请联系管理员完成开通。"
                )
            return

        with tenant_scope(tenant_context):
            if await _handle_coros_connect_message(message):
                return

            try:
                # 只在能力频道亮「正在输入」。论坛帖里 dispatch_text 也会真的干活，
                # 但那条路径不一定有 typing 权限，所以不强求。
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
