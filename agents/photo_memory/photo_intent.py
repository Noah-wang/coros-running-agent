"""照片操作的意图识别。

原来靠正则：有附件就当新建一组，赛事名用「XX马拉松」这类后缀去匹配。
结果「再加上这张奖牌的照片」既看不出是追加、也抽不出赛事名，
只能新建一组「未命名照片」；「这个是洛杉矶马拉松的照片」更是没有对应操作。

正则做不了意图识别——它只能匹配字面模式，认不出「再加上」意味着追加到上一组。
所以这里换成模型判断，把现有照片组作为上下文一并给它，让它决定
新建还是追加、追加到哪一组、顺便把元数据一起抽出来。

抽取仍然保留正则版本作为兜底：模型调用失败时不能让整个功能不可用。
"""

import json
from typing import Any

from agents.photo_memory.photo_store import extract_event, extract_race_date, extract_result
from src.runtime.llm import complete_json


INTENT_PROMPT = """
你是照片记忆助手的意图识别器。用户在聊天里管理自己的比赛照片。只返回 JSON。

返回格式：
{
  "action": "create | append | update | search | merge | help",
  "target_id": "要操作的照片组 id；merge 时填【保留】的那一组，create 和 search 时留空",
  "source_id": "只有 merge 用：要被并进去、随后删除的那一组 id",
  "event": "赛事名，例如 洛杉矶马拉松。没提到就留空",
  "race_date": "YYYY-MM-DD 或 YYYY-MM，没提到就留空",
  "result": "H:MM:SS，没提到就留空",
  "match_ids": ["search 时：从下面的照片组里直接挑出用户想看的那些 id，可以多个"],
  "search_query": "search 时用户想找的东西，一两个词，用于没找到时的提示",
  "reason": "一句很短的原因"
}

判断规则：
- 有图片附件时，只可能是 create 或 append。
- 用户说「再加上」「还有这张」「补一张」「这些也是」，或者这批图明显属于
  上下文里刚讨论的那组比赛，就是 append，target_id 填那一组的 id。
- 有图片但看不出属于哪一组，或者明确提到了一个新赛事，就是 create。
- 没有图片附件时，不可能是 create 或 append。
- 没有图片，但用户在补充某组照片的赛事名、日期或成绩，是 update，
  target_id 填被补充的那一组。用户没指明是哪一组时，用 pending 那一组。
- 没有图片，用户想看照片（「给我看看…」「…的照片呢」），是 search。
  **search 时不要做关键词匹配，直接从上面的照片组里挑 id 填进 match_ids。**
  年份对 race_date、「LA」对洛杉矶、「半马」对上海半马，这些都由你判断。
  用户说「所有照片」「全部」就把所有 id 都填上；一个都对不上就填空数组。
- 没有图片，用户想把某一组照片并进另一组（「把 A 也归到 B 那组」
  「这两组是同一场比赛」「A 和 B 合并」），是 merge。
  target_id 填要保留的那一组，source_id 填被并掉的那一组。
- 完全看不出意图就是 help。

抽取规则：
- 赛事名只填用户真的说了的，例如「这个是洛杉矶马拉松的照片」→ event 填 洛杉矶马拉松。
- 不要从图片内容或组名推断用户没说过的信息。
- 成绩统一成 H:MM:SS。「四小时三十分48」→ 4:30:48。
- 日期统一成 YYYY-MM-DD。「2026年3月8日」→ 2026-03-08。
- 用户只说了年月就填 YYYY-MM。
""".strip()

VALID_ACTIONS = {"create", "append", "update", "search", "merge", "help"}


def _fallback(text: str, has_attachments: bool, pending_id: str) -> dict[str, Any]:
    """模型不可用时的兜底：退回原来的正则行为。"""
    if has_attachments:
        action = "create"
        target_id = ""
    elif pending_id:
        action = "update"
        target_id = pending_id
    elif text.strip():
        action = "search"
        target_id = ""
    else:
        action = "help"
        target_id = ""

    return {
        "action": action,
        "target_id": target_id,
        # 兜底路径不做合并：合并会删掉一整组照片，不能在模型不可用时靠正则去猜
        "source_id": "",
        # 兜底时没有模型来挑 id，交给关键词搜索——见 used_fallback
        "match_ids": [],
        "used_fallback": True,
        "event": extract_event(text) if has_attachments else "",
        "race_date": extract_race_date(text),
        "result": extract_result(text),
        "search_query": text.strip() if action == "search" else "",
        "reason": "fallback",
    }


def _sanitize(
    raw: dict[str, Any],
    text: str,
    has_attachments: bool,
    known_ids: set[str],
    pending_id: str,
) -> dict[str, Any]:
    """模型输出不能直接信，越权的动作要拉回来。"""
    action = str(raw.get("action", "")).strip()
    if action not in VALID_ACTIONS:
        action = "create" if has_attachments else "help"

    # 没有附件不可能新建或追加；有附件不可能是搜索或合并
    if not has_attachments and action in {"create", "append"}:
        action = "update" if pending_id else "search"
    if has_attachments and action in {"search", "update", "merge", "help"}:
        action = "create"

    target_id = str(raw.get("target_id", "")).strip()
    if target_id not in known_ids:
        target_id = ""
    source_id = str(raw.get("source_id", "")).strip()
    if source_id not in known_ids or source_id == target_id:
        source_id = ""
    # 合并必须两组都指名道姓，缺一个就不敢动——删错组是不可逆的
    if action == "merge" and not (target_id and source_id):
        action = "help"
    if action == "append" and not target_id:
        action = "create"
    if action == "update" and not target_id:
        target_id = pending_id
        if not target_id:
            action = "search"

    def _text(key: str) -> str:
        value = raw.get(key)
        return value.strip() if isinstance(value, str) else ""

    model_race_date = _text("race_date")
    fallback_race_date = extract_race_date(text)
    race_date = model_race_date or fallback_race_date
    if model_race_date and fallback_race_date:
        # 模型有时会把「2024 五月26」抽成 2024-05。正则如果能读到日，
        # 用更完整的日期覆盖模型结果。
        if len(fallback_race_date) > len(model_race_date):
            race_date = fallback_race_date

    # 模型挑的 id 同样不能直接信：不在已知分组里的一律丢掉，并保持它给的顺序
    raw_ids = raw.get("match_ids")
    match_ids: list[str] = []
    if action == "search" and isinstance(raw_ids, list):
        for item in raw_ids:
            candidate = str(item).strip()
            if candidate in known_ids and candidate not in match_ids:
                match_ids.append(candidate)

    return {
        "action": action,
        "target_id": target_id,
        "source_id": source_id if action == "merge" else "",
        "match_ids": match_ids,
        "used_fallback": False,
        # 模型漏抽时用正则补一手，两边都没有才算真没有
        "event": _text("event"),
        "race_date": race_date,
        "result": _text("result") or extract_result(text),
        "search_query": _text("search_query") or text.strip(),
        "reason": _text("reason"),
    }


async def classify_photo_intent(
    text: str,
    has_attachments: bool,
    groups: list[dict[str, Any]],
    pending_id: str = "",
) -> dict[str, Any]:
    known_ids = {str(group.get("id")) for group in groups if group.get("id")}
    payload = f"""
用户消息：
{text.strip() or "（没有文字，只发了图片）"}

这条消息带了图片附件：{"是" if has_attachments else "否"}

现有的照片组（越靠前越新）：
{json.dumps(groups, ensure_ascii=False, indent=2) if groups else "（还没有任何照片）"}

正在等待补充信息的照片组 id：{pending_id or "无"}
""".strip()

    try:
        raw = await complete_json(INTENT_PROMPT, payload)
    except Exception as exc:
        print(f"photo intent failed, using fallback: {exc}", flush=True)
        return _fallback(text, has_attachments, pending_id)

    return _sanitize(raw, text, has_attachments, known_ids, pending_id)
