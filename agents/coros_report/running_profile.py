import json
from copy import deepcopy
from typing import Any

from src.runtime.llm import complete_json
from src.runtime.memory import get_agent_memory, update_agent_memory


AGENT_NAME = "coros-report"

PROFILE_EXTRACT_PROMPT = """
你从用户消息中抽取跑步长期记忆。只返回 JSON，不要解释。

只抽取用户明确说出的信息；不要推断、不要补全、不要根据常识猜。

返回格式：
{
  "profile_patch": {
    "body_metrics": {
      "age": null,
      "height_cm": null,
      "weight_kg": null
    },
    "current_times": {
      "half_marathon": null,
      "marathon": null,
      "five_k": null,
      "ten_k": null
    },
    "goals": [
      {
        "distance": "marathon",
        "target_time": "3:30:00",
        "target_date": "2026-12-01"
      }
    ],
    "training_context": {
      "training_days_per_week": null,
      "weekly_mileage_km": null,
      "recent_long_run_km": null
    },
    "race_notes": [
      {
        "distance": "marathon",
        "time": "4:30:00",
        "issue": "补给不足 / 抽筋 / 后半程掉速 / 天气热 / 未说明"
      }
    ],
    "injury_notes": [],
    "preferences": []
  },
  "should_update": true,
  "update_summary": "一句话说明抽取到了什么；没有抽取到则为空字符串"
}

规则：
- “半马140”规范化为 "1:40:00"；“全马430”规范化为 "4:30:00"。
- “半马1:40”“全马4小时30”也规范化为 HH:MM:SS。
- 如果用户只是提问，没有提供明确个人资料，should_update=false。
- 不要抽取医疗诊断。疼痛、伤病历史只放 injury_notes 的自然语言备注。
- profile_patch 中没有值的字段保留 null 或空数组。
""".strip()


def _clean_scalar(value: Any) -> Any:
    if value in ("", None, [], {}):
        return None
    return value


def _merge_dict(existing: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(existing)
    for key, value in patch.items():
        if isinstance(value, dict):
            current = merged.get(key)
            if not isinstance(current, dict):
                current = {}
            merged[key] = _merge_dict(current, value)
            continue

        cleaned = _clean_scalar(value)
        if cleaned is not None:
            merged[key] = cleaned
    return merged


# 模型有时会把抽取提示里的占位说明当成可填的值写进来
PLACEHOLDER_VALUES = {"未说明", "未知", "不清楚", "没说", "无", "未提供", "n/a", "null"}

# 同一实体的判定字段：只要这些字段不冲突，就认为是同一条记录的不同完整度版本
MERGE_KEYS = {
    "goals": ("distance",),
    "race_notes": ("distance", "time"),
}


def _is_placeholder(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in PLACEHOLDER_VALUES


def _clean_record(record: Any) -> Any:
    """去掉记录里的空值和占位值。"""
    if not isinstance(record, dict):
        return record
    return {
        key: value
        for key, value in record.items()
        if _clean_scalar(value) is not None and not _is_placeholder(value)
    }


def _same_entity(left: dict[str, Any], right: dict[str, Any], keys: tuple[str, ...]) -> bool:
    """两条记录是否指向同一件事。

    只有当某个判定字段两边都有值且不相等时才算不同实体。
    一边为空视为"这一版没写"，不构成冲突——这正是
    {marathon, time=null} 和 {marathon, time=4:30:00} 需要合并的情况。
    """
    for key in keys:
        a, b = left.get(key), right.get(key)
        if a is not None and b is not None and a != b:
            return False
    return True


def _append_unique(
    existing_items: list[Any],
    patch_items: list[Any],
    limit: int = 20,
    merge_keys: tuple[str, ...] = (),
) -> list[Any]:
    """合并列表型字段。

    原来只做精确 JSON 相等去重，结果同一场比赛每补充一点信息就多出一条，
    档案里堆满同一件事的不同版本。现在按实体合并，新值覆盖旧值。
    """
    items = [_clean_record(item) for item in existing_items]
    items = [item for item in items if item not in (None, "", [], {})]

    for raw in patch_items:
        if raw in (None, "", [], {}) or _is_placeholder(raw):
            continue
        item = _clean_record(raw)
        if not item:
            continue

        if not isinstance(item, dict) or not merge_keys:
            if item not in items:
                items.append(item)
            continue

        # 清掉占位值之后只剩判定字段，说明这条没带任何信息，直接丢弃。
        # 「半马 / 时间未知 / 情况未说明」就属于这种空壳。
        if not set(item) - set(merge_keys):
            continue

        merged_into = None
        for index, existing in enumerate(items):
            if isinstance(existing, dict) and _same_entity(existing, item, merge_keys):
                merged_into = index
                break

        if merged_into is None:
            items.append(item)
        else:
            items[merged_into] = {**items[merged_into], **item}

    return items[-limit:]


def _normalize_patch(raw_patch: Any) -> dict[str, Any]:
    if not isinstance(raw_patch, dict):
        return {}

    normalized: dict[str, Any] = {}
    for key in ("body_metrics", "current_times", "training_context"):
        value = raw_patch.get(key)
        if isinstance(value, dict):
            cleaned = {
                item_key: item_value
                for item_key, item_value in value.items()
                if _clean_scalar(item_value) is not None
            }
            if cleaned:
                normalized[key] = cleaned

    for key in ("goals", "race_notes", "injury_notes", "preferences"):
        value = raw_patch.get(key)
        if isinstance(value, list):
            cleaned_items = [item for item in value if _clean_scalar(item) is not None]
            if cleaned_items:
                normalized[key] = cleaned_items

    return normalized


def _merge_profile(existing_profile: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = _merge_dict(existing_profile, patch)
    for key in ("goals", "race_notes", "injury_notes", "preferences"):
        existing_items = existing_profile.get(key, [])
        patch_items = patch.get(key, [])
        if isinstance(existing_items, list) and isinstance(patch_items, list):
            merged[key] = _append_unique(
                existing_items, patch_items, merge_keys=MERGE_KEYS.get(key, ())
            )
    return merged


def read_athlete_profile() -> dict[str, Any]:
    """读取当前的跑步长期档案。"""
    agent_memory = get_agent_memory(AGENT_NAME)
    profile = agent_memory.get("athlete_profile", {})
    return profile if isinstance(profile, dict) else {}


def apply_profile_patch(raw_patch: dict[str, Any]) -> str:
    """把一份档案补丁合并进长期记忆。

    供 save_running_profile 工具调用，由模型决定什么时候写。
    """
    patch = _normalize_patch(raw_patch)
    if not patch:
        return "没有可写入的内容。"

    updated_profile = _merge_profile(read_athlete_profile(), patch)
    update_agent_memory(AGENT_NAME, {"athlete_profile": updated_profile})
    return f"已写入长期记忆：{', '.join(sorted(patch))}"


async def update_running_profile_from_message(message: str) -> str:
    extraction = await complete_json(
        PROFILE_EXTRACT_PROMPT,
        f"用户消息：\n{message}",
    )
    if not extraction.get("should_update"):
        return ""

    patch = _normalize_patch(extraction.get("profile_patch"))
    if not patch:
        return ""

    agent_memory = get_agent_memory(AGENT_NAME)
    existing_profile = agent_memory.get("athlete_profile", {})
    if not isinstance(existing_profile, dict):
        existing_profile = {}

    updated_profile = _merge_profile(existing_profile, patch)
    update_agent_memory(AGENT_NAME, {"athlete_profile": updated_profile})

    summary = extraction.get("update_summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    return "已更新跑步长期记忆。"
