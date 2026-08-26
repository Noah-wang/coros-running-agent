import os
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from agents.coros_report.auto_report import _activity_records, _timestamp, activity_key, generate_activity_report
from src.integrations.coros_mcp import call_coros_tool
from src.runtime.memory import (
    get_agent_cache,
    get_agent_memory,
    update_agent_cache,
    update_agent_memory,
)


AGENT_NAME = "coros-report"
DEFAULT_LIST_DAYS = 90
DEFAULT_LIST_LIMIT = 20
DEFAULT_ALL_HISTORY_START = "20100101"


@dataclass(frozen=True)
class ActivityReportResult:
    report: str
    activity: dict[str, Any] | None = None
    title: str = ""


def _date_text(day: date) -> str:
    return day.strftime("%Y%m%d")


def _today() -> date:
    return date.today()


def _max_limit() -> int:
    return _env_int("COROS_ACTIVITY_LIST_MAX_LIMIT", 200, minimum=20)


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return max(int(raw_value), minimum)
    except ValueError:
        return default


def _clamp_limit(limit: int) -> int:
    return min(max(limit, 1), _max_limit())


def _parse_list_arguments(argument: str) -> tuple[dict[str, Any], str]:
    text = argument.strip().lower()
    today = _today()
    limit = DEFAULT_LIST_LIMIT
    days = DEFAULT_LIST_DAYS
    use_all = any(term in text for term in ("all", "全部", "所有", "历史"))

    for match in re.finditer(r"(?:limit|条数|数量)\s*[=:：]?\s*(\d+)", text):
        limit = int(match.group(1))
    for match in re.finditer(r"(?:days|天数|最近)\s*[=:：]?\s*(\d+)", text):
        days = int(match.group(1))

    plain_numbers = [int(item) for item in re.findall(r"(?<![=:：])\b\d+\b", text)]
    if plain_numbers:
        days = plain_numbers[0]
    if len(plain_numbers) >= 2:
        limit = plain_numbers[1]

    if use_all:
        start_date = os.getenv("COROS_ACTIVITY_HISTORY_START_DATE", DEFAULT_ALL_HISTORY_START)
        label = f"从 {start_date} 到今天"
        limit = _clamp_limit(limit if "limit" in text or len(plain_numbers) >= 2 else _max_limit())
    else:
        start = today - timedelta(days=max(days, 1))
        start_date = _date_text(start)
        label = f"最近 {max(days, 1)} 天"
        limit = _clamp_limit(limit)

    arguments = {
        "startDate": start_date,
        "endDate": _date_text(today),
        "sportTypeCodes": [65535],
        "minDistanceKm": 0,
        "maxDistanceKm": 1000,
        "minDurationMinutes": 0,
        "maxDurationMinutes": 1440,
        "maxAveragePace": "",
        "locationKeyword": "",
        "limit": limit,
    }
    return arguments, label


def _date_range_arguments(
    start_date: str,
    end_date: str,
    limit: int = DEFAULT_LIST_LIMIT,
) -> dict[str, Any]:
    return {
        "startDate": start_date,
        "endDate": end_date,
        "sportTypeCodes": [65535],
        "minDistanceKm": 0,
        "maxDistanceKm": 1000,
        "minDurationMinutes": 0,
        "maxDurationMinutes": 1440,
        "maxAveragePace": "",
        "locationKeyword": "",
        "limit": _clamp_limit(limit),
    }


def _first_present(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def _normalize_activity(record: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(record)
    normalized["activityKey"] = activity_key(record)
    normalized["labelId"] = str(record.get("labelId", ""))
    sport_type = record.get("sportType")
    if isinstance(sport_type, str) and sport_type.isdigit():
        normalized["sportType"] = int(sport_type)
    normalized["sortTimestamp"] = _timestamp(record)
    return normalized


def _activity_title(record: dict[str, Any]) -> str:
    return str(
        _first_present(
            record,
            ("sportName", "name", "activityName", "title", "workoutName", "type"),
        )
        or "运动"
    )


def _activity_date(record: dict[str, Any]) -> str:
    value = _first_present(record, ("date", "startDate", "day"))
    if value is not None:
        return str(value)

    timestamp = _timestamp(record)
    if timestamp:
        return datetime.fromtimestamp(timestamp, UTC).astimezone().strftime("%Y-%m-%d")
    return "未知日期"


def _distance_text(record: dict[str, Any]) -> str:
    value = _first_present(record, ("distanceKm", "distance", "totalDistanceKm"))
    if value is None:
        return "距离未知"
    try:
        distance = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{distance:.2f} km"


def _duration_text(record: dict[str, Any]) -> str:
    value = _first_present(
        record,
        ("duration", "durationText", "workoutTime", "totalTime", "time"),
    )
    if value not in (None, ""):
        return str(value)

    start = record.get("startTimestamp")
    end = record.get("endTimestamp")
    try:
        seconds = int(end) - int(start)
    except (TypeError, ValueError):
        return "时长未知"
    if seconds <= 0:
        return "时长未知"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def summarize_activity(record: dict[str, Any]) -> dict[str, str]:
    """把一条运动记录压成四个字段，给主 Agent 的自由问答用。

    命令的输出是给人看的（带编号、带选择菜单），塞回模型里既占上下文
    又会诱导它照抄那个格式。这里只给事实。
    """
    return {
        "date": _activity_date(record),
        "type": _activity_title(record),
        "distance": _distance_text(record),
        "duration": _duration_text(record),
    }


def _format_activity_line(index: int, record: dict[str, Any]) -> str:
    return (
        f"{index}. {_activity_date(record)} | {_activity_title(record)} | "
        f"{_distance_text(record)} | {_duration_text(record)}"
    )


async def query_activity_records(argument: str) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    arguments, label = _parse_list_arguments(argument)
    payload = await call_coros_tool("querySportRecords", arguments)
    records = [_normalize_activity(record) for record in _activity_records(payload)]
    records.sort(key=lambda item: int(item.get("sortTimestamp") or 0), reverse=True)
    update_agent_cache(
        AGENT_NAME,
        {
            "last_activity_list": records,
            "last_activity_list_query": arguments,
            "last_activity_list_label": label,
            "last_activity_list_updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    )
    return records, arguments, label


async def list_activity_records(argument: str) -> str:
    records, arguments, label = await query_activity_records(argument)
    if not records:
        return f"没有查到 {label} 的 COROS 运动记录。"

    lines = [
        f"查到 {label} 的 COROS 运动记录，共 {len(records)} 条，本次最多显示 {arguments['limit']} 条。",
        "",
        "```text",
    ]
    for index, record in enumerate(records, start=1):
        lines.append(_format_activity_line(index, record))
    lines.extend(
        [
            "```",
            "",
            "选择方式：",
            "- `!coros-activity 1`：分析第 1 条",
            "- `!coros-activity 1 重点看后半程心率和配速`：带问题分析第 1 条",
            "- 也可以直接说：分析第 1 条运动记录",
        ]
    )
    return "\n".join(lines)


def _split_selection(argument: str) -> tuple[str, str]:
    stripped = argument.strip()
    if not stripped:
        return "", ""
    head, _, tail = stripped.partition(" ")
    if re.fullmatch(r"\d+", head):
        return head, tail.strip()

    match = re.search(r"第\s*(\d+)\s*条", stripped)
    if match is not None:
        selection = match.group(1)
        question = (stripped[: match.start()] + stripped[match.end() :]).strip()
        return selection, question

    key_match = re.search(r"(\d+:\d+:\d+:\d+|\d{12,})", stripped)
    if key_match is not None:
        selection = key_match.group(1)
        question = (stripped[: key_match.start()] + stripped[key_match.end() :]).strip()
        return selection, question

    return "", stripped


def _load_cached_activities() -> list[dict[str, Any]]:
    records = get_agent_cache(AGENT_NAME).get("last_activity_list")
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)]


def _find_activity(selection: str, records: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not selection:
        return None
    if selection.isdigit():
        index = int(selection)
        if 1 <= index <= len(records):
            return records[index - 1]
    for record in records:
        if selection in {
            str(record.get("activityKey", "")),
            str(record.get("labelId", "")),
        }:
            return record
    return None


async def report_selected_activity(argument: str) -> str:
    selection, question = _split_selection(argument)
    records = _load_cached_activities()
    if not records:
        return "还没有可选择的运动列表。请先发送 `!coros-list`。"

    activity = _find_activity(selection, records)
    if activity is None:
        return "没有找到这条运动记录。请先发送 `!coros-list`，然后用列表里的序号选择。"

    request = question or "分析这条运动记录，重点看配速、心率、恢复和下一次训练建议。"
    title = _format_activity_line(records.index(activity) + 1, activity)
    report = await generate_activity_report(
        activity,
        f"用户选择了运动记录：{title}\n用户问题：{request}",
        get_agent_cache(AGENT_NAME).get("last_activity_list_query"),
    )
    return f"已选择：{title}\n\n{report}"


async def report_selected_activity_for_conversation(
    argument: str,
    conversation_id: str,
) -> str:
    result = await generate_selected_activity_report_for_conversation(
        argument,
        conversation_id,
    )
    return result.report


async def generate_selected_activity_report_for_conversation(
    argument: str,
    conversation_id: str,
) -> ActivityReportResult:
    selection, question = _split_selection(argument)
    records = _load_cached_activities()

    if not records:
        return ActivityReportResult("还没有可选择的运动列表。请先发送 `!coros-list`。")

    activity = _find_activity(selection, records)
    if activity is None:
        return ActivityReportResult("没有找到这条运动记录。请先发送 `!coros-list`，然后用列表里的序号选择。")

    request = question or "分析这条运动记录，重点看配速、心率、恢复和下一次训练建议。"
    title = _format_activity_line(records.index(activity) + 1, activity)
    report = await generate_activity_report(
        activity,
        f"用户选择了运动记录：{title}\n用户问题：{request}",
        get_agent_cache(AGENT_NAME).get("last_activity_list_query"),
    )
    return ActivityReportResult(
        f"已选择：{title}\n\n{report}",
        activity,
        title,
    )
