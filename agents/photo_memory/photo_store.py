import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from src.runtime.atomic import write_json_batch
from src.runtime.capability import RuntimeAttachment


from src.runtime.paths import ROOT_DIR  # noqa: E402
DATA_DIR = ROOT_DIR / "data" / "photo-memory"
MEDIA_DIR = ROOT_DIR / "data" / "media" / "photo-memory"
PHOTOS_PATH = DATA_DIR / "photos.json"
PENDING_PATH = DATA_DIR / "pending.json"

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/heic", "image/heif"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _load_json(path: Path, empty: object) -> object:
    if not path.exists():
        return empty
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    write_json_batch([(path, value)])


def _write_photos_and_pending(photos: object, pending: object) -> None:
    """两个文件要一起生效，否则中途失败会让 photos 和 pending 对不上。"""
    write_json_batch([(PHOTOS_PATH, photos), (PENDING_PATH, pending)])


def _safe_filename(name: str) -> str:
    stem = Path(name).stem or "photo"
    suffix = Path(name).suffix.lower() or ".jpg"
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", stem).strip("-") or "photo"
    return f"{cleaned[:80]}{suffix}"


def _is_image(attachment: RuntimeAttachment) -> bool:
    suffix = Path(attachment.filename).suffix.lower()
    return (attachment.content_type in IMAGE_TYPES) or suffix in IMAGE_SUFFIXES


def _photos() -> list[dict[str, object]]:
    value = _load_json(PHOTOS_PATH, [])
    return value if isinstance(value, list) else []


def _pending() -> dict[str, object]:
    value = _load_json(PENDING_PATH, {})
    return value if isinstance(value, dict) else {}


# 赛事名前面常见的动词和代词，抽出来之后要剥掉，
# 否则「帮我存一下柏林马拉松的照片」会把整个前缀当成赛事名。
EVENT_FILLERS = (
    "帮我存一下", "帮我保存", "帮我记一下", "帮我存", "帮忙存",
    "存一下", "记一下", "保存", "上传", "这是我", "这是", "我的", "我",
)
EVENT_SUFFIXES = ("马拉松", "半马", "全马", "越野赛", "路跑", "接力赛", "越野")

CHINESE_DIGITS = {
    "〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
NUMBER_CHARS = "".join(CHINESE_DIGITS) + "十0123456789"

# 「四小时三十分48」「4小时30分48秒」「4:30:48」都要能认出来
DURATION_RE = re.compile(
    rf"(?P<h>[{NUMBER_CHARS}]{{1,3}})\s*(?:个)?\s*(?:小时|小時|时|時|h)"
    rf"\s*(?P<m>[{NUMBER_CHARS}]{{1,3}})\s*(?:分钟|分鐘|分|min|m)?"
    rf"(?:\s*(?P<s>[{NUMBER_CHARS}]{{1,3}})\s*(?:秒|s)?)?"
)
FULL_CLOCK_RE = re.compile(r"(?<!\d)(\d{1,2}):([0-5]\d):([0-5]\d)(?!\d)")
SHORT_CLOCK_RE = re.compile(r"(?<!\d)(\d{1,2}):([0-5]\d)(?!\d|:)")
RESULT_KEYWORDS = ("成绩", "完赛", "用时", "跑了", "跑出", "净成绩", "枪声成绩")


def _to_int(text: str) -> int | None:
    """把「48」「四」「三十」「三十一」转成数字。"""
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if "十" in text:
        left, _, right = text.partition("十")
        if left and left not in CHINESE_DIGITS:
            return None
        if right and right not in CHINESE_DIGITS:
            return None
        return (CHINESE_DIGITS[left] if left else 1) * 10 + (
            CHINESE_DIGITS[right] if right else 0
        )
    # 「零五」这类补位写法：零本身是 0，跟在后面的才是有效数字
    if len(text) == 2 and text[0] in "〇零":
        return CHINESE_DIGITS.get(text[1])
    return CHINESE_DIGITS.get(text)


def load_event_aliases() -> dict[str, str]:
    """可选的赛事别名表：正则 → 规范名。

    英文赛事名（"LA Marathon"）不以中文后缀结尾，通用规则认不出来。
    但**具体有哪些别名是每个人自己的事**，写死在代码里等于把作者的
    比赛清单发给所有人。所以放在数据目录里，缺省为空：

        data/photo-memory/event-aliases.json
        {"LA\\s*Marathon": "洛杉矶马拉松"}
    """
    path = DATA_DIR / "event-aliases.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def extract_event(text: str) -> str:
    """抽赛事名。

    先查可选的别名表，再找以「马拉松 / 半马 / 越野赛」等结尾的短名，
    最后剥掉句首的动词前缀。通用模式不能用贪婪起点，
    否则「帮我存一下柏林马拉松的照片」会整个吞进去。
    """
    for pattern, canonical in load_event_aliases().items():
        try:
            if re.search(pattern, text, flags=re.IGNORECASE):
                return canonical
        except re.error:
            continue

    suffixes = "|".join(EVENT_SUFFIXES)
    match = re.search(rf"([^\s，。,.、；;！!？?的]{{1,12}}(?:{suffixes}))", text)
    if not match:
        return "未命名照片"

    name = match.group(1).strip()
    changed = True
    while changed:
        changed = False
        for filler in EVENT_FILLERS:
            if name.startswith(filler) and len(name) > len(filler):
                name = name[len(filler) :]
                changed = True
                break
    return name or "未命名照片"


def _format_date(year: str, month: str, day: str = "") -> str:
    year_value = int(year)
    month_value = _to_int(month)
    day_value = _to_int(day) if day else None
    if month_value is None or not 1 <= month_value <= 12:
        return ""
    if day and (day_value is None or not 1 <= day_value <= 31):
        return ""
    if day_value is None:
        return f"{year_value:04d}-{month_value:02d}"
    return f"{year_value:04d}-{month_value:02d}-{day_value:02d}"


def extract_race_date(text: str) -> str:
    """抽比赛日期，支持数字和中文月份。

    用户经常会写成「2024 五月26」或「2024年五月二十六号」。
    这类表达里年份和月份之间没有固定分隔符，旧正则会只抓到年月。
    """
    number = rf"[{NUMBER_CHARS}]{{1,3}}"
    patterns = (
        rf"(20\d{{2}})\s*(?:年|[-/.])?\s*({number})\s*月\s*({number})?\s*(?:日|号)?",
        r"(20\d{2})[-/.年](\d{1,2})(?:[-/.月](\d{1,2})\s*(?:日|号)?)?",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        year, month, day = match.groups()
        formatted = _format_date(year, month, day or "")
        if formatted:
            return formatted
    return ""


def extract_result(text: str) -> str:
    """抽比赛成绩，统一规范成 H:MM:SS。

    原来的实现要求关键词和数字之间只能有冒号或空格，
    所以「比赛成绩是4:30:48」里的「是」就把它挡住了；
    中文数字「四小时三十分48」也完全不认。
    """
    match = DURATION_RE.search(text)
    if match:
        hours = _to_int(match.group("h"))
        minutes = _to_int(match.group("m"))
        seconds = _to_int(match.group("s") or "0")
        if hours is not None and minutes is not None and seconds is not None:
            if minutes < 60 and seconds < 60:
                return f"{hours}:{minutes:02d}:{seconds:02d}"

    match = FULL_CLOCK_RE.search(text)
    if match:
        return f"{int(match.group(1))}:{match.group(2)}:{match.group(3)}"

    # 两段式（4:30）既可能是时:分也可能是分:秒，不猜，原样保留，
    # 但要求附近出现成绩类关键词，避免把配速或日期当成成绩。
    for match in SHORT_CLOCK_RE.finditer(text):
        prefix = text[max(0, match.start() - 12) : match.start()]
        if any(keyword in prefix for keyword in RESULT_KEYWORDS):
            return match.group(0)
    return ""


def _missing_fields(record: dict[str, object]) -> list[str]:
    missing = []
    if not record.get("race_date"):
        missing.append("比赛年月日")
    if not record.get("result"):
        missing.append("比赛成绩")
    return missing


async def save_photo_batch(
    text: str,
    attachments: tuple[RuntimeAttachment, ...],
    conversation_id: str,
    event: str = "",
    race_date: str = "",
    result: str = "",
) -> str:
    """新建一组照片。

    event / race_date / result 由意图识别层传入，为空时才退回正则抽取——
    正则只认得「XX马拉松」这类字面模式，认不出「再加上这张奖牌的照片」。
    """
    images = [attachment for attachment in attachments if _is_image(attachment)]
    if not images:
        return "我没有看到图片附件。你可以把照片发上来，并写：这是我柏林马拉松的比赛照片，帮我存一下。"

    event = event or extract_event(text)
    race_date = race_date or extract_race_date(text)
    result = result or extract_result(text)
    photo_id = f"photo-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}"
    folder = MEDIA_DIR / photo_id
    folder.mkdir(parents=True, exist_ok=True)

    files: list[dict[str, object]] = []
    for index, attachment in enumerate(images, start=1):
        filename = f"{index:02d}-{_safe_filename(attachment.filename)}"
        target = folder / filename
        await attachment.save(target)
        files.append(
            {
                "path": str(target.relative_to(ROOT_DIR)),
                "filename": attachment.filename,
                "content_type": attachment.content_type,
                "size": attachment.size,
                "source_url": attachment.url,
            }
        )

    record: dict[str, object] = {
        "id": photo_id,
        "event": event,
        "caption": text.strip(),
        # 不再塞「比赛照片/跑步/马拉松」这类恒定标签：它们对每条记录都成立，
        # 检索时只会放大噪声，区分不出任何东西。
        "tags": [event] if event != "未命名照片" else [],
        "race_date": race_date,
        "result": result,
        "notes": "",
        "files": files,
        "created_at": _now(),
        "updated_at": _now(),
    }

    photos = _photos()
    photos.append(record)

    missing = _missing_fields(record)
    pending = _pending()
    if missing:
        pending[conversation_id] = {
            "photo_id": photo_id,
            "missing": missing,
            "updated_at": _now(),
        }
        _write_photos_and_pending(photos, pending)
        return (
            f"已保存 {len(files)} 张「{event}」照片。\n\n"
            f"还差这些信息：{'、'.join(missing)}。\n"
            "你可以直接回复，例如：比赛日期是 2024-03-17，成绩 4:30:00。"
        )

    pending.pop(conversation_id, None)
    _write_photos_and_pending(photos, pending)
    return f"已保存 {len(files)} 张「{event}」照片，比赛日期和成绩也一起记录好了。"


def _is_expired(item: object) -> bool:
    """pending 用和会话历史相同的过期时间。

    两份状态必须同寿：内存里的 pending_questions 60 分钟就过期，
    而 pending.json 原来是永久的。超时之后主 Agent 因为内存没了不再走
    照片补充这条捷径，磁盘上却还留着一条待办，两边说法不一致。
    """
    if not isinstance(item, dict):
        return True
    stamp = str(item.get("updated_at", ""))
    if not stamp:
        return False
    try:
        updated = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    minutes = float(os.getenv("CONVERSATION_IDLE_MINUTES", "60"))
    return (datetime.now(UTC) - updated).total_seconds() > max(minutes, 1.0) * 60


def live_pending() -> dict[str, object]:
    """去掉过期项之后的待补充列表。"""
    return {
        conversation_id: item
        for conversation_id, item in _pending().items()
        if not _is_expired(item)
    }


def has_pending_update(conversation_id: str) -> bool:
    return conversation_id in live_pending()


def pending_photo_id(conversation_id: str) -> str:
    """当前会话正在等待补充信息的那组照片 id。"""
    item = live_pending().get(conversation_id)
    if isinstance(item, dict):
        return str(item.get("photo_id", ""))
    return ""


def restore_pending_questions(set_questions) -> int:
    """进程启动时把磁盘上的待补充状态回填进内存。

    内存 pending 是进程内的，重启就没了；磁盘上那条却还在。
    不回填的话，重启后用户回一句「成绩是 4:30:48」不会被认成补充信息。
    """
    restored = 0
    for conversation_id, item in live_pending().items():
        missing = item.get("missing") if isinstance(item, dict) else None
        questions = [f"这批照片的{field}是什么？" for field in (missing or [])]
        if questions:
            set_questions(conversation_id, questions)
            restored += 1
    return restored


def update_pending_photo(text: str, conversation_id: str) -> str:
    pending = _pending()
    item = pending.get(conversation_id)
    if not isinstance(item, dict):
        return "现在没有正在等待补充信息的照片。"

    photo_id = str(item.get("photo_id", ""))
    photos = _photos()
    for record in photos:
        if record.get("id") != photo_id:
            continue
        race_date = extract_race_date(text)
        result = extract_result(text)
        if race_date:
            record["race_date"] = race_date
        if result:
            record["result"] = result
        notes = str(record.get("notes", "")).strip()
        record["notes"] = f"{notes}\n{text.strip()}".strip()
        record["updated_at"] = _now()
        missing = _missing_fields(record)
        if missing:
            pending[conversation_id] = {
                "photo_id": photo_id,
                "missing": missing,
                "updated_at": _now(),
            }
            _write_photos_and_pending(photos, pending)
            return f"已补充信息。还差：{'、'.join(missing)}。"
        pending.pop(conversation_id, None)
        _write_photos_and_pending(photos, pending)
        return "已补充完整：比赛日期和成绩都记录好了。"

    pending.pop(conversation_id, None)
    _write_json(PENDING_PATH, pending)
    return "这批待补充照片已经找不到了，我已清除待办状态。"


def list_recent_groups(limit: int = 12) -> list[dict[str, object]]:
    """最近的照片组摘要，给意图识别当上下文。

    模型要靠它判断「再加上这张」是往哪一组加，所以必须带上 id、
    赛事名和已有的元数据，但不能带 files 这种大字段。
    """
    groups = []
    for record in sorted(_photos(), key=lambda r: str(r.get("created_at", "")), reverse=True):
        groups.append(
            {
                "id": record.get("id"),
                "event": record.get("event"),
                "race_date": record.get("race_date", ""),
                "result": record.get("result", ""),
                "photo_count": len(record.get("files", []) or []),
                "created_at": record.get("created_at", ""),
            }
        )
        if len(groups) >= limit:
            break
    return groups


def find_group(photo_id: str) -> dict[str, object] | None:
    for record in _photos():
        if record.get("id") == photo_id:
            return record
    return None


async def append_photos(
    photo_id: str,
    attachments: tuple[RuntimeAttachment, ...],
) -> str:
    """把新图片追加进已有的一组，而不是新建一组。"""
    images = [attachment for attachment in attachments if _is_image(attachment)]
    if not images:
        return "我没有看到图片附件。"

    photos = _photos()
    record = next((item for item in photos if item.get("id") == photo_id), None)
    if record is None:
        return "找不到这组照片，可能已经被删掉了。"

    files = record.get("files")
    if not isinstance(files, list):
        files = []
        record["files"] = files

    folder = MEDIA_DIR / photo_id
    folder.mkdir(parents=True, exist_ok=True)
    start = len(files)
    for index, attachment in enumerate(images, start=start + 1):
        filename = f"{index:02d}-{_safe_filename(attachment.filename)}"
        target = folder / filename
        await attachment.save(target)
        files.append(
            {
                "path": str(target.relative_to(ROOT_DIR)),
                "filename": attachment.filename,
                "content_type": attachment.content_type,
                "size": attachment.size,
                "source_url": attachment.url,
            }
        )

    record["updated_at"] = _now()
    _write_json(PHOTOS_PATH, photos)
    return (
        f"已把 {len(images)} 张照片加进「{record.get('event')}」这组，"
        f"现在一共 {len(files)} 张。"
    )


def update_photo_meta(
    photo_id: str,
    event: str = "",
    race_date: str = "",
    result: str = "",
    note: str = "",
    conversation_id: str = "",
) -> str:
    """更新一组照片的元数据。

    原来的 update_pending_photo 只认日期和成绩，所以用户说
    「这个是洛杉矶马拉松的照片」时赛事名根本改不掉，只能塞进 notes。
    """
    photos = _photos()
    record = next((item for item in photos if item.get("id") == photo_id), None)
    if record is None:
        return "找不到这组照片。"

    changed = []
    if event and event != "未命名照片" and event != record.get("event"):
        record["event"] = event
        tags = record.get("tags")
        record["tags"] = sorted({*(tags if isinstance(tags, list) else []), event})
        changed.append(f"赛事改成「{event}」")
    if race_date and race_date != record.get("race_date"):
        record["race_date"] = race_date
        changed.append(f"日期 {race_date}")
    if result and result != record.get("result"):
        record["result"] = result
        changed.append(f"成绩 {result}")
    if note:
        existing = str(record.get("notes", "")).strip()
        record["notes"] = f"{existing}\n{note}".strip()
    record["updated_at"] = _now()

    pending = _pending()
    missing = _missing_fields(record)
    if missing and conversation_id:
        pending[conversation_id] = {
            "photo_id": photo_id,
            "missing": missing,
            "updated_at": _now(),
        }
    elif conversation_id:
        pending.pop(conversation_id, None)
    _write_photos_and_pending(photos, pending)

    head = "已更新：" + "、".join(changed) if changed else "记下了"
    if missing:
        return f"{head}。还差：{'、'.join(missing)}。"
    return f"{head}。这组照片的信息齐了。"


def merge_groups(source_id: str, target_id: str, conversation_id: str = "") -> str:
    """把 source 组并入 target 组，然后删掉 source。

    用于修正分组错误：意图识别之前，「再加上这张」会被当成新建，
    结果同一场比赛被拆成两组。

    文件不做物理搬移，只把 files 记录挪过去。搬文件要么全成要么全不成，
    中途失败会留下一半在旧目录一半在新目录，而记录已经改了——
    照片就找不回来了。只改记录的话，最坏情况是旧目录留在磁盘上，无害。
    """
    if not source_id or not target_id:
        return "没说清要把哪组并到哪组。"
    if source_id == target_id:
        return "这两个是同一组照片。"

    photos = _photos()
    source = next((item for item in photos if item.get("id") == source_id), None)
    target = next((item for item in photos if item.get("id") == target_id), None)
    if source is None or target is None:
        return "找不到要合并的照片组。"

    source_files = source.get("files") or []
    target_files = target.get("files")
    if not isinstance(target_files, list):
        target_files = []
        target["files"] = target_files
    before = len(target_files)
    target_files.extend(source_files)

    # 目标缺的字段用来源补上，已有的不覆盖
    for field in ("race_date", "result"):
        if not target.get(field) and source.get(field):
            target[field] = source[field]
    source_tags = source.get("tags") or []
    target_tags = target.get("tags") or []
    if isinstance(source_tags, list) and isinstance(target_tags, list):
        target["tags"] = sorted({*target_tags, *source_tags})
    notes = [str(target.get("notes", "")).strip(), str(source.get("notes", "")).strip()]
    target["notes"] = "\n".join(part for part in notes if part)
    target["updated_at"] = _now()

    photos = [item for item in photos if item.get("id") != source_id]

    pending = _pending()
    # source 的待办要么转移到 target，要么直接清掉
    for key, item in list(pending.items()):
        if isinstance(item, dict) and item.get("photo_id") == source_id:
            pending.pop(key, None)
    missing = _missing_fields(target)
    if missing and conversation_id:
        pending[conversation_id] = {
            "photo_id": target_id,
            "missing": missing,
            "updated_at": _now(),
        }
    elif conversation_id:
        pending.pop(conversation_id, None)

    _write_photos_and_pending(photos, pending)

    moved = len(target_files) - before
    head = (
        f"已把 {moved} 张照片并进「{target.get('event')}」这组，"
        f"现在一共 {len(target_files)} 张。"
    )
    if missing:
        return f"{head}还差：{'、'.join(missing)}。"
    return head


# 通用的中英对照，和具体是谁跑过哪场比赛无关
SEARCH_ALIASES = {
    "marathon": "马拉松",
    "half": "半马",
    "trail": "越野",
    "race": "比赛",
}


def load_search_aliases() -> dict[str, str]:
    """可选的检索别名：查询词片段 → 追加的检索词。

    例如 {"la marathon": "洛杉矶"}，让英文缩写能对上中文标注。
    """
    path = DATA_DIR / "search-aliases.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k).lower(): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def search_photos(query: str) -> list[dict[str, object]]:
    normalized = query.strip().lower()
    if not normalized:
        return []

    terms = [term for term in re.split(r"\s+", normalized) if term]

    # 检索同义词。英文查询词要能对上中文标注（marathon → 马拉松），
    # 否则「marathon photos」一条都搜不到。
    #
    # 城市名这类**具体到某个人参加过哪些比赛**的别名不写在这里，
    # 放进 data/photo-memory/search-aliases.json，缺省为空。
    expanded = terms + [
        SEARCH_ALIASES[term] for term in terms if term in SEARCH_ALIASES
    ]
    for term, canonical in load_search_aliases().items():
        if term in normalized:
            expanded.append(canonical)

    # 必须全部命中。原来写成 if all(...) elif any(...)，
    # all 成立时 any 必然成立，所以那一支是死代码，实际等于纯 OR——
    # 而每条记录的 tags 都硬编码了「马拉松」，
    # 结果任何含「马拉松」的查询都会命中全部照片，越具体反而越不准。
    needles = [term for term in expanded if term]
    results = []
    for record in _photos():
        haystack = " ".join(
            [
                str(record.get("event", "")),
                str(record.get("caption", "")),
                str(record.get("race_date", "")),
                str(record.get("result", "")),
                " ".join(str(tag) for tag in record.get("tags", [])),
            ]
        ).lower()
        if all(term in haystack for term in needles):
            results.append(record)
    return results


def photo_paths(record: dict[str, object]) -> list[Path]:
    files = record.get("files", [])
    if not isinstance(files, list):
        return []
    paths = []
    for item in files:
        if isinstance(item, dict) and item.get("path"):
            path = ROOT_DIR / str(item["path"])
            if path.exists():
                paths.append(path)
    return paths


def photo_urls(record: dict[str, object]) -> list[str]:
    urls = []
    for path in photo_paths(record):
        try:
            relative = path.relative_to(MEDIA_DIR)
        except ValueError:
            continue
        urls.append(
            "/media/photo-memory/"
            + quote(relative.as_posix(), safe="/")
        )
    return urls


def format_photo_summary(records: list[dict[str, object]]) -> str:
    lines = [f"找到 {len(records)} 组照片："]
    for record in records:
        count = len(photo_paths(record))
        parts = [str(record.get("event", "未命名照片"))]
        if record.get("race_date"):
            parts.append(str(record["race_date"]))
        if record.get("result"):
            parts.append(f"成绩 {record['result']}")
        lines.append(f"- {' · '.join(parts)}：{count} 张")
    return "\n".join(lines)
