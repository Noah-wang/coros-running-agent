import re
from datetime import UTC, datetime
from typing import Any

from src.runtime.memory import get_agent_memory, update_agent_memory


AGENT_NAME = "coros-report"

TARGETS: tuple[dict[str, object], ...] = (
    {"key": "1k", "label": "1 公里", "distance_km": 1.0, "tolerance": 0.04},
    {"key": "3k", "label": "3 公里", "distance_km": 3.0, "tolerance": 0.035},
    {"key": "5k", "label": "5 公里", "distance_km": 5.0, "tolerance": 0.03},
    {"key": "10k", "label": "10 公里", "distance_km": 10.0, "tolerance": 0.03},
    {"key": "half_marathon", "label": "半马", "distance_km": 21.0975, "tolerance": 0.025},
    {"key": "marathon", "label": "全马", "distance_km": 42.195, "tolerance": 0.02},
)


def _activity_key(activity: dict[str, Any]) -> str:
    parts = [
        str(activity.get("labelId", "")),
        str(activity.get("sportType", "")),
        str(activity.get("startTimestamp", "")),
        str(activity.get("endTimestamp", "")),
    ]
    return ":".join(parts)


def _collect_text(value: Any) -> str:
    parts: list[str] = []
    if isinstance(value, str):
        parts.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            parts.append(_collect_text(item))
    elif isinstance(value, list):
        for item in value:
            parts.append(_collect_text(item))
    return "\n".join(part for part in parts if part)


def _parse_duration_seconds(text: str) -> int | None:
    match = re.search(
        r"(?:Workout Time|Activity Time|Moving Time|Total Time):\s*([0-9:]+)",
        text,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None

    parts = [int(part) for part in match.group(1).split(":") if part.isdigit()]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 1:
        return parts[0]
    return None


def _parse_distance_km(text: str) -> float | None:
    match = re.search(r"Distance:\s*([0-9.]+)\s*km", text, flags=re.IGNORECASE)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _format_duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _target_for_distance(distance_km: float) -> dict[str, object] | None:
    for target in TARGETS:
        target_distance = float(target["distance_km"])
        tolerance = float(target["tolerance"])
        if abs(distance_km - target_distance) / target_distance <= tolerance:
            return target
    return None


def update_personal_bests_from_tool_results(
    activity: dict[str, Any],
    tool_results: list[dict[str, Any]],
) -> list[dict[str, object]]:
    detail_result = next(
        (
            item
            for item in tool_results
            if item.get("ok") and (item.get("tool") or {}).get("name") == "getActivityDetail"
        ),
        None,
    )
    if detail_result is None:
        return []

    text = _collect_text(detail_result.get("result"))
    distance_km = _parse_distance_km(text)
    duration_seconds = _parse_duration_seconds(text)
    if distance_km is None or duration_seconds is None:
        return []

    target = _target_for_distance(distance_km)
    if target is None:
        return []

    target_key = str(target["key"])
    memory = get_agent_memory(AGENT_NAME)
    personal_bests = memory.get("personal_bests")
    if not isinstance(personal_bests, dict):
        personal_bests = {}

    current = personal_bests.get(target_key)
    if isinstance(current, dict):
        current_seconds = current.get("seconds")
        if isinstance(current_seconds, int) and current_seconds <= duration_seconds:
            return []

    updated = dict(personal_bests)
    record = {
        "distance": str(target["label"]),
        "distance_km": float(target["distance_km"]),
        "seconds": duration_seconds,
        "time": _format_duration(duration_seconds),
        "activity_id": _activity_key(activity),
        "labelId": activity.get("labelId"),
        "sportType": activity.get("sportType"),
        "date": activity.get("date"),
        "source": "coros_mcp",
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    updated[target_key] = record
    update_agent_memory(AGENT_NAME, {"personal_bests": updated})
    return [{"key": target_key, **record}]


def format_personal_bests() -> str:
    personal_bests = get_agent_memory(AGENT_NAME).get("personal_bests")
    if not isinstance(personal_bests, dict) or not personal_bests:
        return (
            "还没有自动记录到 PB。\n"
            "之后当 COROS 运动详情匹配 1K、3K、5K、10K、半马或全马，并且成绩更快时，我会自动更新。"
        )

    lines = ["你的 COROS 自动 PB：", "", "| 项目 | 成绩 | 日期 | 来源 |", "|---|---:|---|---|"]
    for target in TARGETS:
        record = personal_bests.get(str(target["key"]))
        if not isinstance(record, dict):
            lines.append(f"| {target['label']} | - | - | - |")
            continue
        lines.append(
            "| "
            f"{target['label']} | "
            f"{record.get('time', '-')} | "
            f"{record.get('date') or '-'} | "
            "COROS 自动检测 |"
        )
    lines.append("")
    lines.append("PB 只能由 COROS 运动详情自动更新，不能通过聊天手动修改。")
    return "\n".join(lines)
