import os
from dataclasses import dataclass

import discord


@dataclass(frozen=True)
class ForumPost:
    thread: discord.Thread
    message: discord.Message


def _configured_id(name: str) -> int | None:
    value = os.getenv(name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


async def _forum_channel(client: discord.Client) -> discord.ForumChannel | None:
    channel_id = _configured_id("DISCORD_REPORT_FORUM_CHANNEL_ID")
    if channel_id is None:
        return None

    channel = client.get_channel(channel_id)
    if channel is None:
        channel = await client.fetch_channel(channel_id)
    return channel if isinstance(channel, discord.ForumChannel) else None


def _forum_tag(
    channel: discord.ForumChannel,
    tag_id_env: str,
) -> discord.ForumTag | None:
    tag_id = _configured_id(tag_id_env)
    if tag_id is not None:
        return next((tag for tag in channel.available_tags if tag.id == tag_id), None)
    return None


async def create_report_post(
    client: discord.Client,
    title: str,
    report: str,
    tag_id_env: str,
) -> ForumPost | None:
    """Create a tagged forum post, or return None when forum output is disabled."""
    channel = await _forum_channel(client)
    if channel is None:
        return None

    tag = _forum_tag(channel, tag_id_env)
    if tag is None:
        raise RuntimeError("Configured COROS forum tag was not found.")

    chunks = [report[start : start + 1800] for start in range(0, len(report), 1800)]
    if not chunks:
        chunks = ["报告生成完成。"]

    created = await channel.create_thread(
        name=title[:100],
        content=chunks[0],
        applied_tags=[tag],
    )
    for chunk in chunks[1:]:
        await created.thread.send(chunk)
    return ForumPost(thread=created.thread, message=created.message)
