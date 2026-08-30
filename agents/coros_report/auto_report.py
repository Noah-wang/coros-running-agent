import asyncio
import json
import os
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

import discord

from src.runtime.trace import new_trace

from agents.coros_report.fit_archive import archive_fit_for_activities, render_route_map_for_activity
from agents.coros_report.shadowrunner_prompt import REPORT_SYSTEM_PROMPT
from src.integrations.coros_mcp import call_coros_tool
from src.runtime.llm import complete_text
from src.runtime.memory import (
    format_memory_for_prompt,
    get_agent_cache,
    get_agent_memory,
    update_agent_cache,
    update_agent_memory,
)
from src.runtime.scheduler import add_interval_job
from agents.coros_report.personal_bests import update_personal_bests_from_tool_results


AGENT_NAME = "coros-report"
AUTO_REPORT_CANDIDATE_CACHE_KEY = "auto_report_candidate_activity"
_job_running = False


def _date_text(day: date) -> str:
    return day.strftime("%Y%m%d")


def _log_timestamp() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _activity_log_summary(activity: dict[str, Any]) -> str:
    return (
        f"activity_key={activity_key(activity)} "
        f"labelId={activity.get('labelId')} "
        f"sportType={activity.get('sportType')} "
        f"startTimestamp={activity.get('startTimestamp')} "
        f"endTimestamp={activity.get('endTimestamp')}"
    )


def _log_auto_report(message: str) -> None:
    print(f"[{_log_timestamp()}] coros-auto-report {message}", flush=True)


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return max(int(raw_value), minimum)
    except ValueError:
        _log_auto_report(f"invalid_env name={name} value={raw_value!r} using={default}")
        return default


async def _with_timeout(label: str, awaitable: Any, timeout_seconds: int) -> Any:
    _log_auto_report(f"{label}_start timeout_seconds={timeout_seconds}")
    try:
        result = await asyncio.wait_for(awaitable, timeout=timeout_seconds)
    except TimeoutError as exc:
        _log_auto_report(f"{label}_timeout timeout_seconds={timeout_seconds}")
        raise TimeoutError(f"{label} timed out after {timeout_seconds}s") from exc
    except Exception as exc:
        _log_auto_report(f"{label}_failed error={exc}")
        raise
    _log_auto_report(f"{label}_end")
    return result


def _activity_query_arguments() -> dict[str, Any]:
    lookback_days = int(os.getenv("COROS_AUTO_REPORT_LOOKBACK_DAYS", "7"))
    today = date.today()
    start_date = today - timedelta(days=max(lookback_days, 1))
    return {
        "startDate": _date_text(start_date),
        "endDate": _date_text(today),
        "sportTypeCodes": [65535],
        "minDistanceKm": 0,
        "maxDistanceKm": 1000,
        "minDurationMinutes": 0,
        "maxDurationMinutes": 1440,
        "maxAveragePace": "",
        "locationKeyword": "",
        "limit": 10,
    }


def _parse_json_text(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _records_from_text(text: str) -> list[dict[str, Any]]:
    parsed = _parse_json_text(text)
    if isinstance(parsed, str):
        text = parsed

    records: list[dict[str, Any]] = []
    blocks = re.split(r"\n\s*(?=\d+\.\s+)", text)
    for block in blocks:
        label_match = re.search(r"LabelId:\s*(\d+)", block)
        sport_match = re.search(r"SportType:\s*(\d+)", block)
        start_match = re.search(r"startTimestamp=(\d+)", block)
        end_match = re.search(r"endTimestamp=(\d+)", block)
        if label_match is None or sport_match is None:
            continue

        record: dict[str, Any] = {
            "labelId": label_match.group(1),
            "sportType": int(sport_match.group(1)),
        }
        if start_match is not None:
            record["startTimestamp"] = int(start_match.group(1))
        if end_match is not None:
            record["endTimestamp"] = int(end_match.group(1))

        title_match = re.search(r"\d+\.\s+(.+?)\s+—\s+(\d{4}-\d{2}-\d{2})", block)
        if title_match is not None:
            record["sportName"] = title_match.group(1).strip()
            record["date"] = title_match.group(2)

        duration_match = re.search(r"Duration:\s*([0-9:]+)", block)
        if duration_match is not None:
            record["duration"] = duration_match.group(1)

        location_match = re.search(r"Location:\s*(.+)", block)
        if location_match is not None:
            record["location"] = location_match.group(1).strip()

        pace_match = re.search(r"Average Pace:\s*([^|]+)", block)
        if pace_match is not None:
            record["averagePace"] = pace_match.group(1).strip()

        heart_rate_match = re.search(r"Avg HR:\s*(\d+)\s*bpm", block)
        if heart_rate_match is not None:
            record["averageHeartRate"] = int(heart_rate_match.group(1))

        distance_match = re.search(r"Distance:\s*([0-9.]+)\s*(km|m)\b", block)
        if distance_match is not None:
            distance = float(distance_match.group(1))
            if distance_match.group(2) == "m":
                distance = distance / 1000
            record["distanceKm"] = distance

        records.append(record)
    return records


def _expand_payload(value: Any) -> list[Any]:
    expanded = [value]
    if isinstance(value, dict):
        content = value.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    text = item["text"]
                    parsed = _parse_json_text(text)
                    if parsed is not None:
                        expanded.extend(_expand_payload(parsed))
                    expanded.extend(_records_from_text(text))
        for key in ("data", "records", "activities", "list", "items", "result"):
            child = value.get(key)
            if child is not None:
                expanded.extend(_expand_payload(child))
    elif isinstance(value, list):
        for item in value:
            expanded.extend(_expand_payload(item))
    return expanded


def _activity_records(payload: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in _expand_payload(payload):
        if not isinstance(item, dict):
            continue
        if item.get("labelId") is not None and item.get("sportType") is not None:
            records.append(item)
    return records


def _timestamp(activity: dict[str, Any]) -> int:
    for key in ("endTimestamp", "startTimestamp", "timestamp"):
        value = activity.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return 0


def activity_key(activity: dict[str, Any]) -> str:
    parts = [
        str(activity.get("labelId", "")),
        str(activity.get("sportType", "")),
        str(activity.get("startTimestamp", "")),
        str(activity.get("endTimestamp", "")),
    ]
    return ":".join(parts)


async def recent_coros_activities() -> list[dict[str, Any]]:
    timeout_seconds = _coros_tool_timeout_seconds()
    arguments = _activity_query_arguments()
    try:
        payload = await _with_timeout(
            "tool_querySportRecords",
            call_coros_tool("querySportRecords", arguments),
            timeout_seconds,
        )
    except Exception as exc:
        _log_auto_report(f"tool_querySportRecords_retry_empty_args reason={exc}")
        payload = await _with_timeout(
            "tool_querySportRecords_empty_args",
            call_coros_tool("querySportRecords", {}),
            timeout_seconds,
        )
    records = _activity_records(payload)
    records.sort(key=_timestamp, reverse=True)
    return records


async def latest_coros_activity() -> dict[str, Any] | None:
    records = await recent_coros_activities()
    if not records:
        return None
    return records[0]


def _activity_signature(activity: dict[str, Any]) -> str:
    parts = [
        activity_key(activity),
        str(activity.get("distanceKm") or ""),
        str(activity.get("duration") or ""),
        str(activity.get("averagePace") or ""),
        str(activity.get("averageHeartRate") or ""),
    ]
    return "|".join(parts)


def should_send_activity(activity: dict[str, Any]) -> bool:
    memory = get_agent_memory(AGENT_NAME)
    latest_reported_signature = memory.get("latest_reported_activity_signature")
    current_signature = _activity_signature(activity)
    if isinstance(latest_reported_signature, str) and latest_reported_signature:
        return latest_reported_signature != current_signature

    latest_reported_id = memory.get("latest_reported_activity_id")
    if latest_reported_id == activity_key(activity):
        return True
    return latest_reported_id != activity_key(activity)


def has_reported_activity() -> bool:
    return get_agent_memory(AGENT_NAME).get("latest_reported_activity_id") is not None


def mark_activity_reported(activity: dict[str, Any]) -> None:
    update_agent_memory(
        AGENT_NAME,
        {
            "latest_reported_activity_id": activity_key(activity),
            "latest_reported_activity_signature": _activity_signature(activity),
            "latest_reported_activity": {
                "labelId": activity.get("labelId"),
                "sportType": activity.get("sportType"),
                "startTimestamp": activity.get("startTimestamp"),
                "endTimestamp": activity.get("endTimestamp"),
                "distanceKm": activity.get("distanceKm"),
                "duration": activity.get("duration"),
                "averagePace": activity.get("averagePace"),
                "averageHeartRate": activity.get("averageHeartRate"),
            },
        },
    )


def _activity_age_minutes(activity: dict[str, Any]) -> float | None:
    timestamp = _timestamp(activity)
    if timestamp <= 0:
        return None
    return (datetime.now(UTC).timestamp() - timestamp) / 60


def _stable_minutes() -> int:
    return _env_int("COROS_AUTO_REPORT_STABLE_MINUTES", 60, minimum=1)


def _candidate_cache() -> dict[str, Any]:
    cache = get_agent_cache(AGENT_NAME).get(AUTO_REPORT_CANDIDATE_CACHE_KEY, {})
    return cache if isinstance(cache, dict) else {}


def _mark_candidate(activity: dict[str, Any]) -> None:
    update_agent_cache(
        AGENT_NAME,
        {
            AUTO_REPORT_CANDIDATE_CACHE_KEY: {
                "activity_id": activity_key(activity),
                "activity_signature": _activity_signature(activity),
                "first_seen_at": datetime.now(UTC).isoformat(),
                "activity": {
                    "labelId": activity.get("labelId"),
                    "sportType": activity.get("sportType"),
                    "startTimestamp": activity.get("startTimestamp"),
                    "endTimestamp": activity.get("endTimestamp"),
                    "distanceKm": activity.get("distanceKm"),
                    "duration": activity.get("duration"),
                },
            }
        },
    )


def _clear_candidate() -> None:
    update_agent_cache(AGENT_NAME, {AUTO_REPORT_CANDIDATE_CACHE_KEY: {}})


def _activity_stable_enough(activity: dict[str, Any]) -> tuple[bool, str]:
    age_minutes = _activity_age_minutes(activity)
    if age_minutes is None:
        _mark_candidate(activity)
        return False, "missing activity end timestamp"

    stable_minutes = _stable_minutes()
    if age_minutes < stable_minutes:
        _mark_candidate(activity)
        return False, f"activity ended {age_minutes:.1f} minutes ago; waiting {stable_minutes} minutes"

    activity_signature = _activity_signature(activity)
    candidate = _candidate_cache()
    if candidate.get("activity_signature") != activity_signature:
        _mark_candidate(activity)
        return False, "activity first seen after stable window; waiting one more check"

    return True, "activity is stable"


async def generate_activity_report(
    activity: dict[str, Any],
    user_request: str,
    source_arguments: dict[str, Any] | None = None,
) -> str:
    label_id = activity.get("labelId")
    sport_type = activity.get("sportType")
    if label_id is None or sport_type is None:
        raise RuntimeError("Selected COROS activity is missing labelId or sportType.")

    tool_results: list[dict[str, Any]] = [
        {
            "tool": {"name": "querySportRecords", "arguments": source_arguments or {}},
            "ok": True,
            "result": {"selected_activity": activity},
        }
    ]
    detail_args = {"labelId": str(label_id), "sportType": int(sport_type)}
    for tool_name, arguments in [
        ("getActivityDetail", detail_args),
        ("queryActivityLapData", detail_args),
        ("queryRecoveryStatus", {}),
        ("queryTrainingLoadAssessment", {}),
    ]:
        try:
            result = await _with_timeout(
                f"tool_{tool_name}",
                call_coros_tool(tool_name, arguments),
                _coros_tool_timeout_seconds(),
            )
            tool_results.append(
                {"tool": {"name": tool_name, "arguments": arguments}, "ok": True, "result": result}
            )
        except Exception as exc:
            tool_results.append(
                {"tool": {"name": tool_name, "arguments": arguments}, "ok": False, "error": str(exc)}
            )

    pb_updates = update_personal_bests_from_tool_results(activity, tool_results)
    if pb_updates:
        tool_results.append(
            {
                "tool": {"name": "personalBestMemory", "arguments": {}},
                "ok": True,
                "result": {"updates": pb_updates},
            }
        )

    memory = format_memory_for_prompt(AGENT_NAME)
    return await _with_timeout(
        "llm_report_generation",
        complete_text(
            REPORT_SYSTEM_PROMPT,
            f"""
User request:
{user_request}

请只分析 selected_activity 对应的这一次运动，并生成运动后报告。

User memory:
{memory}

COROS tool calls and results:
{json.dumps(tool_results, ensure_ascii=False, indent=2)}

Generate the workout report from the available COROS data.
""".strip(),
        ),
        _llm_timeout_seconds(),
    )


async def generate_auto_activity_report(activity: dict[str, Any]) -> str:
    return await generate_activity_report(
        activity,
        "自动检测到一条新的 COROS 运动。",
        _activity_query_arguments(),
    )


def _auto_report_enabled() -> bool:
    return os.getenv("COROS_AUTO_REPORT_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _send_on_first_run() -> bool:
    return os.getenv("COROS_AUTO_REPORT_SEND_ON_FIRST_RUN", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _poll_minutes() -> int:
    return _env_int("COROS_AUTO_REPORT_POLL_MINUTES", 15)


def _check_timeout_seconds() -> int:
    return _env_int("COROS_AUTO_REPORT_TIMEOUT_SECONDS", 300, minimum=30)


def _coros_tool_timeout_seconds() -> int:
    return _env_int("COROS_MCP_TOOL_TIMEOUT_SECONDS", 75, minimum=10)


def _llm_timeout_seconds() -> int:
    return _env_int("COROS_AUTO_REPORT_LLM_TIMEOUT_SECONDS", 180, minimum=30)


def _configured_channel_id() -> int | None:
    value = os.getenv("DISCORD_RUNNING_CHANNEL_ID")
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


async def _send_chunks(channel: discord.abc.Messageable, text: str) -> None:
    chunk_size = 1800
    for start in range(0, len(text), chunk_size):
        await channel.send(text[start : start + chunk_size])


async def _report_channel(client: discord.Client) -> discord.abc.Messageable | None:
    channel_id = _configured_channel_id()
    if channel_id is None:
        return None

    channel = client.get_channel(channel_id)
    if channel is not None:
        return channel

    fetched = await client.fetch_channel(channel_id)
    if callable(getattr(fetched, "send", None)):
        return fetched
    return None


async def _archive_recent_fit_files(records: list[dict[str, Any]]) -> None:
    if not records:
        return
    try:
        results = await archive_fit_for_activities(records)
    except Exception as exc:
        _log_auto_report(f"fit_archive_failed error={exc}")
        return
    archived = sum(1 for result in results if result.paths)
    downloaded = sum(1 for result in results if result.downloaded)
    _log_auto_report(f"fit_archive_done checked={len(results)} archived={archived} downloaded={downloaded}")


async def _send_route_map_if_available(
    channel: discord.abc.Messageable,
    activity: dict[str, Any],
) -> None:
    try:
        result = await render_route_map_for_activity(activity)
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        _log_auto_report(f"route_map_failed error={message}")
        if "MAPBOX_ACCESS_TOKEN" not in message:
            await channel.send(f"路线图生成失败：{message}")
        return

    if result.path is None:
        _log_auto_report(f"route_map_skipped reason={result.message}")
        return

    _log_auto_report(f"route_map_send path={result.path} points={result.point_count}")
    await channel.send("本次室外运动路线图：", file=discord.File(result.path))


async def check_and_send_coros_auto_report(
    client: discord.Client,
    notify_no_change: bool = False,
    send_on_first_run: bool | None = None,
    force_send: bool = False,
) -> str:
    global _job_running
    if _job_running:
        return "COROS auto report skipped: previous job is still running."

    # 定时任务也要有 trace。后台跑的活出问题时最难查——没人在旁边看着，
    # 只能靠事后翻日志，而没有 trace 就串不起一次完整的执行。
    new_trace("cron")

    _job_running = True
    try:
        _log_auto_report("channel_lookup_start")
        channel = await _report_channel(client)
        if channel is None:
            return "COROS auto report skipped: DISCORD_RUNNING_CHANNEL_ID is invalid."
        _log_auto_report("channel_lookup_end")

        _log_auto_report("latest_activity_lookup_start")
        records = await recent_coros_activities()
        if not records:
            _log_auto_report("activity_lookup records=0")
            return "COROS auto report skipped: no recent activity found."

        await _archive_recent_fit_files(records)
        activity = records[0]
        _log_auto_report(f"activity_lookup records>=1 {_activity_log_summary(activity)}")

        if not force_send and not should_send_activity(activity):
            _clear_candidate()
            message = "COROS auto report skipped: no new activity."
            if notify_no_change:
                _log_auto_report("discord_notify_no_change_start")
                await channel.send("没有检测到新的 COROS 运动。")
                _log_auto_report("discord_notify_no_change_end")
            return message

        if not force_send:
            stable, reason = _activity_stable_enough(activity)
            _log_auto_report(f"activity_stability stable={stable} reason={reason}")
            if not stable:
                return f"COROS auto report skipped: {reason}."

        should_send_first = (
            _send_on_first_run() if send_on_first_run is None else send_on_first_run
        )
        if not force_send and not has_reported_activity() and not should_send_first:
            _log_auto_report("first_run_initialize_latest_activity")
            mark_activity_reported(activity)
            _clear_candidate()
            return "COROS auto report initialized with latest activity."

        _log_auto_report("discord_detected_message_start")
        await channel.send("检测到新的 COROS 运动，正在自动生成报告...")
        _log_auto_report("discord_detected_message_end")
        _log_auto_report("report_generation_start")
        report = await generate_auto_activity_report(activity)
        _log_auto_report(f"report_generation_end chars={len(report)}")
        _log_auto_report("discord_report_send_start")
        await _send_chunks(channel, report)
        _log_auto_report("discord_report_send_end")
        await _send_route_map_if_available(channel, activity)
        mark_activity_reported(activity)
        _clear_candidate()
        _log_auto_report("mark_activity_reported")
        return "COROS auto report sent."
    except Exception as exc:
        return f"COROS auto report failed: {exc}"
    finally:
        _job_running = False


async def _scheduled_check(client: discord.Client) -> None:
    started_at = datetime.now(UTC)
    timeout_seconds = _check_timeout_seconds()
    _log_auto_report(f"check_start timeout_seconds={timeout_seconds}")
    try:
        result = await asyncio.wait_for(
            check_and_send_coros_auto_report(client),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        result = f"COROS auto report failed: check timed out after {timeout_seconds}s."
    elapsed = (datetime.now(UTC) - started_at).total_seconds()
    _log_auto_report(f"check_end elapsed={elapsed:.1f}s result={result}")


def register_coros_auto_report(client: discord.Client) -> None:
    if not _auto_report_enabled():
        _log_auto_report("scheduler_disabled")
        return

    add_interval_job(
        "coros-auto-report",
        _scheduled_check,
        _poll_minutes(),
        args=[client],
    )
    _log_auto_report(f"scheduler_started poll_minutes={_poll_minutes()}")
