from pathlib import Path

import discord

from agents.photo_memory.photo_intent import classify_photo_intent
from agents.photo_memory.photo_read_tools import PHOTO_READ_TOOLS
from agents.photo_memory.photo_store import (
    append_photos,
    find_group,
    format_photo_summary,
    has_pending_update,
    list_recent_groups,
    merge_groups,
    pending_photo_id,
    photo_paths,
    photo_urls,
    restore_pending_questions,
    save_photo_batch,
    search_photos,
    update_photo_meta,
)
from src.runtime.capability import Capability, CommandContext, TextCommand
from src.runtime.conversation import (
    PHOTO_MEMORY_TOPIC,
    RUNNING_COACH_TOPIC,
    set_context_value,
    set_pending_questions,
)


PHOTO_HELP = """
直接发图片并说明就行，例如：这是我柏林马拉松的比赛照片。
想往同一场比赛补图，说「再加上这张」我会加进同一组。
想看照片就说：给我看柏林马拉松的照片。
""".strip()

# 显式命令是手动兜底，走确定性分支；自然语言一律交给意图识别。
EXPLICIT_ACTIONS = {
    "store": "create",
    "save": "create",
    "add": "create",
    "append": "append",
    "update": "update",
    "补充": "update",
    "search": "search",
    "show": "search",
    "find": "search",
    "list": "search",
}


async def _send_files(context: CommandContext, paths: list[Path]) -> None:
    if not paths:
        return
    for start in range(0, len(paths), 10):
        batch = paths[start : start + 10]
        files = [discord.File(path) for path in batch]
        await context.channel.send(files=files)


def _strip_explicit_action(text: str) -> tuple[str, str]:
    action, _, rest = text.strip().partition(" ")
    mapped = EXPLICIT_ACTIONS.get(action.lower())
    if mapped:
        return mapped, rest.strip()
    return "", text.strip()


async def _photo(context: CommandContext, argument: str) -> None:
    if context.read_only:
        await _photo_read_only(context, argument)
        return

    forced_action, text = _strip_explicit_action(argument)
    has_attachments = any(item.is_image for item in context.attachments)
    pending_id = pending_photo_id(context.conversation_id)

    if not forced_action and not has_attachments and not text:
        await context.send(PHOTO_HELP)
        return

    intent = await classify_photo_intent(
        text,
        has_attachments,
        list_recent_groups(),
        pending_id,
    )
    if forced_action:
        intent["action"] = forced_action
        if forced_action in {"append", "update"} and not intent["target_id"]:
            intent["target_id"] = pending_id

    print(
        f"photo intent action={intent['action']} target={intent['target_id'] or '-'} "
        f"source={intent['source_id'] or '-'} "
        f"event={intent['event'] or '-'} reason={intent['reason']}",
        flush=True,
    )

    await _execute(context, intent, text)
    _sync_pending_questions(context)


async def _photo_read_only(context: CommandContext, argument: str) -> None:
    forced_action, text = _strip_explicit_action(argument)
    if forced_action and forced_action != "search":
        await context.send("网页入口只能查看照片，保存和修改请在 Discord 里操作。")
        return

    query = text.strip()
    if not query:
        groups = list_recent_groups()
        if not groups:
            await context.send("照片库还没有保存照片。")
            return
        names = "\n".join(
            f"- {group['event']}：{group['photo_count']} 张"
            for group in groups[:10]
        )
        await context.send(f"照片库最近保存的分组：\n{names}\n\n你可以问：查某某马拉松的照片。")
        return

    intent = await classify_photo_intent(
        query,
        False,
        list_recent_groups(),
        "",
    )
    if intent["used_fallback"]:
        records = search_photos(intent["search_query"] or query)
    else:
        records = [
            group for group in (find_group(pid) for pid in intent["match_ids"]) if group
        ]
        if not records:
            records = search_photos(intent["search_query"] or query)

    if not records:
        await context.send(f"没有找到和「{query}」相关的照片。")
        return

    _remember_photo_context(context, records)
    summary = format_photo_summary(records)

    all_urls: list[str] = []
    for record in records:
        all_urls.extend(photo_urls(record))

    # 图片走直发通道。走 send 的话它们会变成工具返回值交给模型，
    # 模型再复述一遍——实测的结果是它把 17 张图缩成一句
    # 「已经全部加载出来了」，用户一张都看不到。
    caption = "、".join(str(r.get("event", "照片")) for r in records)
    if context.images(all_urls, caption):
        # **必须告诉模型图已经发出去了。** 只返回摘要的话，
        # 模型不知道用户已经看到照片，会接着问「你确实参加过这场比赛吗、
        # 能不能提供更多信息以便我帮你查找」——和屏幕上的 17 张图直接打架。
        await context.send(
            f"{summary}\n\n"
            f"（这 {len(all_urls)} 张照片已经直接显示在用户屏幕上了。"
            "不要再说要去查找或需要更多信息，直接就着照片说话。）"
        )
        return

    # 入口不支持直发（例如纯文本通道）时，退回把链接写进文本
    lines = [summary, ""]
    for record in records:
        title = str(record.get("event", "照片"))
        urls = photo_urls(record)
        if not urls:
            continue
        lines.append(f"### {title}")
        for index, url in enumerate(urls, start=1):
            lines.append(f"![{title} {index}]({url})")
    await context.send("\n".join(lines).strip())


async def _execute(context: CommandContext, intent: dict, text: str) -> None:
    action = intent["action"]

    if action == "append":
        await context.send(await append_photos(intent["target_id"], context.attachments))
        # 追加时用户可能顺带补了元数据，例如「再加一张，成绩是 4:30:48」
        if any(intent[key] for key in ("event", "race_date", "result")):
            await context.send(
                update_photo_meta(
                    intent["target_id"],
                    event=intent["event"],
                    race_date=intent["race_date"],
                    result=intent["result"],
                    conversation_id=context.conversation_id,
                )
            )
        return

    if action == "create":
        await context.send(
            await save_photo_batch(
                text,
                context.attachments,
                context.conversation_id,
                event=intent["event"],
                race_date=intent["race_date"],
                result=intent["result"],
            )
        )
        return

    if action == "update":
        target = intent["target_id"]
        if not target or find_group(target) is None:
            await context.send("现在没有正在补充信息的照片。你可以先发一批照片。")
            return
        await context.send(
            update_photo_meta(
                target,
                event=intent["event"],
                race_date=intent["race_date"],
                result=intent["result"],
                note=text,
                conversation_id=context.conversation_id,
            )
        )
        return

    if action == "merge":
        await context.send(
            merge_groups(
                intent["source_id"],
                intent["target_id"],
                conversation_id=context.conversation_id,
            )
        )
        return

    if action == "search":
        await _search_and_send(context, intent)
        return

    await context.send(PHOTO_HELP)


def _sync_pending_questions(context: CommandContext) -> None:
    if has_pending_update(context.conversation_id):
        set_pending_questions(
            context.conversation_id,
            PHOTO_MEMORY_TOPIC,
            ["这批照片的比赛年月日是什么？", "这批照片的比赛成绩是多少？"],
        )
    else:
        set_pending_questions(context.conversation_id, PHOTO_MEMORY_TOPIC, [])


async def _search_and_send(context: CommandContext, intent: dict) -> None:
    """照片检索由模型直接挑分组，不做关键词匹配。

    关键词匹配在中文上很脆：查询按空格切词，而中文整句没有空格，
    「给我看看我2026年洛杉矶马拉松的照片」会变成一个词去 AND 匹配，
    必然落空。而且「2026年」要对上 race_date、「LA」要对上洛杉矶，
    每一条都得手写别名表，写多少都补不完。

    模型本来就拿到了全部分组（id、赛事名、日期、成绩），让它挑 id 就行。
    关键词搜索只在模型不可用时兜底——那时结果差，总好过没有。
    """
    query = intent["search_query"]

    if intent["used_fallback"]:
        records = search_photos(query)
    else:
        records = [
            group for group in (find_group(pid) for pid in intent["match_ids"]) if group
        ]

    if not records:
        hint = f"没有找到和「{query}」相关的照片。" if query else "你想看哪组照片？"
        names = ""
        groups = list_recent_groups()
        if groups:
            names = "、".join(str(g["event"]) for g in groups[:5])
            hint += f"\n现在存了这些：{names}"
        await context.send(hint)
        return

    await context.send(format_photo_summary(records))
    _remember_photo_context(context, records)
    for record in records:
        await _send_files(context, photo_paths(record))


def _remember_photo_context(
    context: CommandContext,
    records: list[dict[str, object]],
) -> None:
    if len(records) != 1:
        return

    record = records[0]
    race_date = str(record.get("race_date") or "").strip()
    if not race_date:
        return

    value = {
        "photo_id": record.get("id"),
        "event": record.get("event"),
        "race_date": race_date,
        "result": record.get("result"),
    }
    set_context_value(
        context.conversation_id,
        RUNNING_COACH_TOPIC,
        "recent_photo_context",
        value,
    )
    set_context_value(
        context.conversation_id,
        PHOTO_MEMORY_TOPIC,
        "recent_photo_context",
        value,
    )


def _restore_pending(_client: object) -> None:
    """启动时把磁盘上的待补充照片回填进内存 pending。

    内存 pending 随进程消失，磁盘上那条不会。不回填的话，重启之后
    用户回一句「成绩是 4:30:48」不会被识别成补充信息。
    """

    def _set(conversation_id: str, questions: list[str]) -> None:
        set_pending_questions(conversation_id, PHOTO_MEMORY_TOPIC, questions)

    try:
        restored = restore_pending_questions(_set)
    except Exception as exc:
        print(f"photo pending restore failed: {exc}", flush=True)
        return
    if restored:
        print(f"photo-memory restored {restored} pending conversations", flush=True)


def build_photo_capability() -> Capability:
    return Capability(
        name="photo-memory",
        description="保存和检索 Discord 上传的比赛照片，并追问比赛日期、成绩等元数据。",
        channel_env_name="DISCORD_RUNNING_CHANNEL_ID",
        text_commands=(
            TextCommand(
                "photo",
                # 描述必须以**检索**开头。原来写成「保存、追加、补充信息、检索…」，
                # 只读入口下模型看到一串写操作动词就绕开了它——
                # 于是「给我看照片」只查到张数，图一张都没显示。
                "按赛事检索比赛照片并把原图显示出来。"
                "用户说「给我看某某比赛的照片」「翻一下那场半马的图」时用它——"
                "只有它能返回图片本身，list_races 只有张数。"
                "也用于保存新照片、补充赛事名/日期/成绩、合并分组（这些只在 Discord 生效）。"
                "参数：用户的原话，照片能力内部自己做意图识别",
                _photo,
                aliases=("photos",),
                # 保存和合并都会改数据，但能力内部按 read_only 走只读分支
                writes=True,
                read_only_safe=True,
                # 只读入口下一个字都不提写操作：提了模型会以为整个工具不可用
                read_only_description=(
                    "按赛事检索比赛照片，并把原图直接显示给用户。"
                    "用户说「给我看某某比赛的照片」「翻一下那场半马的图」时调它。"
                    "**只有这个工具能返回图片本身**，list_races 只有张数。"
                    "现在就可以调用，不需要任何额外权限"
                ),
                argument_hint="用户的原话，照片能力内部自己做意图识别",
            ),
        ),
        startup_handlers=(_restore_pending,),
        read_tools=PHOTO_READ_TOOLS,
    )
