"""知识库订阅源：要定期同步哪些 UP 主。

原来这份名单硬编码在 `scripts/sync_bilibili.py` 里，加一个源要改代码再部署。
挪到 JSON 之后，Agent 可以在对话里加——用户发一个空间链接，
下一次定时任务就会把这个人的视频排队导入。

**只存 uid 和分类，不存视频列表。** 列表由同步脚本自己缓存，
两份状态各管各的：这里回答「要同步谁」，缓存回答「他有哪些视频」。
"""

import json
import re
from typing import Any

from src.runtime.paths import DATA_DIR

SOURCES_PATH = DATA_DIR / "knowledge" / "coros-report" / "sources.json"

# 内置的初始名单。文件不存在时用它，之后一切以文件为准。
DEFAULT_SOURCES: tuple[dict[str, Any], ...] = (
    {"uid": 32360754, "category": "shoes", "name": "亚平宁的蓝色"},
    {"uid": 37275219, "category": "shoes", "name": "奥尔里奇吴"},
    {"uid": 619863116, "category": "shoes", "name": "东哥真的很严格"},
    {"uid": 1879203169, "category": "training", "name": "云健身-仰望尾迹云"},
)

VALID_CATEGORIES = ("shoes", "training")
UID_PATTERN = re.compile(r"space\.bilibili\.com/(\d+)|^(\d{4,})$")


def extract_uid(text: str) -> int:
    """从空间链接或裸 uid 里取出 uid。取不到返回 0。"""
    match = UID_PATTERN.search(text.strip())
    if not match:
        return 0
    return int(match.group(1) or match.group(2) or 0)


def load_sources() -> list[dict[str, Any]]:
    if not SOURCES_PATH.exists():
        return [dict(item) for item in DEFAULT_SOURCES]
    try:
        data = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [dict(item) for item in DEFAULT_SOURCES]
    return [item for item in data if isinstance(item, dict) and item.get("uid")]


def save_sources(sources: list[dict[str, Any]]) -> None:
    SOURCES_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOURCES_PATH.write_text(
        json.dumps(sources, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def add_source(text: str, category: str, name: str = "") -> str:
    """把一个 UP 主加进订阅。返回给用户看的说明。"""
    uid = extract_uid(text)
    if not uid:
        return "没认出 UP 主 ID。发一个 space.bilibili.com/数字 的链接，或者直接给数字 ID。"

    if category not in VALID_CATEGORIES:
        return (
            f"分类只能是 {' 或 '.join(VALID_CATEGORIES)}。"
            "跑鞋测评用 shoes，训练理论用 training。"
        )

    sources = load_sources()
    for item in sources:
        if int(item.get("uid", 0)) == uid:
            # 已存在时允许改分类——用户可能一开始归错了类
            if item.get("category") != category:
                item["category"] = category
                save_sources(sources)
                return f"这个 UP 主已经在订阅里，分类已改成 {category}。"
            return f"这个 UP 主已经在订阅里（分类 {category}），不用重复添加。"

    sources.append({"uid": uid, "category": category, "name": name.strip()})
    save_sources(sources)
    return (
        f"已加入订阅：UID {uid}，分类 {category}。"
        "下一次同步会开始导入他的视频字幕，历史存量会分几天慢慢补齐，"
        "以免触发 B 站的接口限流。"
    )


def format_sources() -> str:
    sources = load_sources()
    if not sources:
        return "还没有订阅任何 UP 主。"
    lines = ["当前订阅的知识来源："]
    for item in sources:
        label = item.get("name") or f"UID {item['uid']}"
        lines.append(f"- {label}（{item['uid']}）→ {item.get('category', '?')}")
    return "\n".join(lines)
