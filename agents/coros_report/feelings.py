from datetime import datetime
from typing import Any

from agents.coros_report.auto_report import activity_key, latest_coros_activity
from src.runtime.memory import get_agent_memory, update_agent_memory


AGENT_NAME = "coros-report"
MAX_FEELING_NOTES = 30


async def record_feeling(note: str) -> str:
    cleaned_note = note.strip()
    if not cleaned_note:
        return "请写下你的运动感受，例如：`!feel 今天腿很沉，RPE 7，左膝有点紧。`"

    activity: dict[str, Any] | None = None
    activity_id: str | None = None
    try:
        activity = await latest_coros_activity()
        if activity is not None:
            activity_id = activity_key(activity)
    except Exception:
        activity = None

    agent_memory = get_agent_memory(AGENT_NAME)
    notes = agent_memory.get("feeling_notes", [])
    if not isinstance(notes, list):
        notes = []

    entry = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "note": cleaned_note,
        "activity_id": activity_id,
    }
    if activity is not None:
        entry["activity"] = {
            "labelId": activity.get("labelId"),
            "sportType": activity.get("sportType"),
            "startTimestamp": activity.get("startTimestamp"),
            "endTimestamp": activity.get("endTimestamp"),
        }

    notes.append(entry)
    notes = notes[-MAX_FEELING_NOTES:]
    update_agent_memory(AGENT_NAME, {"feeling_notes": notes})

    if activity_id is None:
        return "已记录你的感受，但这次没有成功关联到 COROS 最新运动。"
    return "已记录你的感受，并关联到 COROS 最新运动。"


def list_recent_feelings(limit: int = 5) -> str:
    agent_memory = get_agent_memory(AGENT_NAME)
    notes = agent_memory.get("feeling_notes", [])
    if not isinstance(notes, list) or not notes:
        return "还没有记录运动感受。"

    recent_notes = notes[-limit:]
    lines = ["最近记录的运动感受："]
    for index, item in enumerate(reversed(recent_notes), start=1):
        if not isinstance(item, dict):
            continue
        created_at = item.get("created_at", "未知时间")
        note = item.get("note", "")
        activity_id = item.get("activity_id")
        suffix = f" | activity: {activity_id}" if activity_id else ""
        lines.append(f"{index}. {created_at}{suffix}\n{note}")
    return "\n\n".join(lines)
