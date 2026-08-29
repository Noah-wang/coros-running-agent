import asyncio
import json
import os
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from agents.coros_report.sleep_report_prompt import SLEEP_REPORT_SYSTEM_PROMPT
from src.integrations.coros_mcp import call_coros_tool
from src.runtime.llm import complete_text
from src.runtime.memory import format_memory_for_prompt, get_agent_cache, update_agent_cache
from src.runtime.scheduler import add_interval_job
from src.runtime.trace import new_trace


AGENT_NAME = "coros-report"
SLEEP_REPORT_CACHE_KEY = "sleep_report_sent_dates"
_job_running = False


def _log_timestamp() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _log_sleep_report(message: str) -> None:
    print(f"[{_log_timestamp()}] coros-sleep-report {message}", flush=True)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return max(int(value), minimum)
    except ValueError:
        _log_sleep_report(f"invalid_env name={name} value={value!r} using={default}")
        return default


def _timezone() -> ZoneInfo:
    name = os.getenv("COROS_SLEEP_REPORT_TIMEZONE", "America/Los_Angeles")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        _log_sleep_report(f"invalid_timezone name={name!r} using=UTC")
        return ZoneInfo("UTC")


def _env_time(name: str, default: str) -> time:
    value = os.getenv(name, default)
    try:
        hour_text, minute_text = value.split(":", 1)
        return time(hour=max(0, min(int(hour_text), 23)), minute=max(0, min(int(minute_text), 59)))
    except ValueError:
        _log_sleep_report(f"invalid_time name={name} value={value!r} using={default}")
        hour_text, minute_text = default.split(":", 1)
        return time(hour=int(hour_text), minute=int(minute_text))


def _window_start_time() -> time:
    return _env_time("COROS_SLEEP_REPORT_START_TIME", os.getenv("COROS_SLEEP_REPORT_TIME", "07:00"))


def _window_end_time() -> time:
    return _env_time("COROS_SLEEP_REPORT_END_TIME", "12:00")


def _today() -> date:
    return datetime.now(_timezone()).date()


def _target_sleep_day() -> date:
    return _today() - timedelta(days=1)


def _date_text(day: date) -> str:
    return day.strftime("%Y%m%d")


def _daily_job_due() -> bool:
    now = datetime.now(_timezone())
    start = _window_start_time()
    end = _window_end_time()
    current = (now.hour, now.minute)
    return (start.hour, start.minute) <= current <= (end.hour, end.minute)


def _enabled() -> bool:
    return _env_bool("COROS_SLEEP_REPORT_ENABLED", True)


def _poll_minutes() -> int:
    return _env_int("COROS_SLEEP_REPORT_POLL_MINUTES", 30)


def _check_timeout_seconds() -> int:
    return _env_int("COROS_SLEEP_REPORT_TIMEOUT_SECONDS", 240, minimum=30)


def _coros_tool_timeout_seconds() -> int:
    return _env_int("COROS_MCP_TOOL_TIMEOUT_SECONDS", 75, minimum=10)


def _llm_timeout_seconds() -> int:
    return _env_int("COROS_SLEEP_REPORT_LLM_TIMEOUT_SECONDS", 180, minimum=30)


def _configured_channel_id() -> int | None:
    value = os.getenv("DISCORD_RUNNING_CHANNEL_ID")
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


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


async def _send_chunks(channel: discord.abc.Messageable, text: str) -> None:
    chunk_size = 1800
    for start in range(0, len(text), chunk_size):
        await channel.send(text[start : start + chunk_size])


async def _with_timeout(label: str, awaitable: Any, timeout_seconds: int) -> Any:
    _log_sleep_report(f"{label}_start timeout_seconds={timeout_seconds}")
    try:
        result = await asyncio.wait_for(awaitable, timeout=timeout_seconds)
    except TimeoutError as exc:
        _log_sleep_report(f"{label}_timeout timeout_seconds={timeout_seconds}")
        raise TimeoutError(f"{label} timed out after {timeout_seconds}s") from exc
    except Exception as exc:
        _log_sleep_report(f"{label}_failed error={exc}")
        raise
    _log_sleep_report(f"{label}_end")
    return result


def _sent_dates() -> set[str]:
    raw = get_agent_cache(AGENT_NAME).get(SLEEP_REPORT_CACHE_KEY, [])
    if not isinstance(raw, list):
        return set()
    return {str(item) for item in raw if item}


def _has_sent(day: date) -> bool:
    return day.isoformat() in _sent_dates()


def _mark_sent(day: date) -> None:
    dates = sorted([*_sent_dates(), day.isoformat()])[-60:]
    update_agent_cache(AGENT_NAME, {SLEEP_REPORT_CACHE_KEY: dates})


async def _call_coros_with_fallbacks(tool_name: str, day: date) -> dict[str, Any]:
    date_value = _date_text(day)
    attempts = (
        {"startDate": date_value, "endDate": date_value},
        {"date": date_value},
        {"day": date_value},
        {},
    )
    last_error: Exception | None = None
    for arguments in attempts:
        try:
            result = await _with_timeout(
                f"tool_{tool_name}",
                call_coros_tool(tool_name, arguments),
                _coros_tool_timeout_seconds(),
            )
            return {"tool": {"name": tool_name, "arguments": arguments}, "ok": True, "result": result}
        except Exception as exc:
            last_error = exc

    return {
        "tool": {"name": tool_name, "arguments": {"date": date_value}},
        "ok": False,
        "error": str(last_error) if last_error is not None else "unknown error",
    }


# 五个 COROS 工具**互不依赖**，并行取。
#
# 原来是串行 for 循环，单次上限 75 秒，最坏情况 375 秒。定时任务能等，
# 但包成 Agent 工具之后循环只给 75 秒——串行版本必然超时。
# 并行之后墙钟时间回到「最慢的那一个」。
SLEEP_TOOL_NAMES = (
    "querySleepData",
    "querySleepHrv",
    "queryDailyHealthData",
    "queryRecoveryStatus",
    "queryTrainingLoadAssessment",
)


async def _collect_sleep_tool_results(day: date) -> list[dict[str, Any]]:
    return list(
        await asyncio.gather(
            *(_call_coros_with_fallbacks(name, day) for name in SLEEP_TOOL_NAMES)
        )
    )


def _contains_sleep_signal(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        text = value.lower()
        empty_markers = ("no sleep", "not found", "暂无", "无睡眠", "没有睡眠", "no data")
        sleep_markers = ("sleep", "asleep", "rem", "deep", "light", "睡眠", "深睡", "浅睡")
        return any(marker in text for marker in sleep_markers) and not any(marker in text for marker in empty_markers)
    if isinstance(value, list):
        return any(_contains_sleep_signal(item) for item in value)
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).lower()
            if any(marker in key_text for marker in ("sleep", "asleep", "rem", "deep", "light", "睡眠")):
                if item not in (None, "", [], {}):
                    return True
            if _contains_sleep_signal(item):
                return True
    return False


def _sleep_data_available(tool_results: list[dict[str, Any]]) -> bool:
    for item in tool_results:
        if not item.get("ok"):
            continue
        tool = item.get("tool")
        if not isinstance(tool, dict):
            continue
        if tool.get("name") not in {"querySleepData", "queryDailyHealthData"}:
            continue
        if _contains_sleep_signal(item.get("result")):
            return True
    return False


async def generate_sleep_report(
    day: date | None = None,
    tool_results: list[dict[str, Any]] | None = None,
) -> str:
    target_day = day or _target_sleep_day()
    if tool_results is None:
        tool_results = await _collect_sleep_tool_results(target_day)
    memory = format_memory_for_prompt(AGENT_NAME)
    return await _with_timeout(
        "llm_sleep_report_generation",
        complete_text(
            SLEEP_REPORT_SYSTEM_PROMPT,
            f"""
Target sleep date:
{target_day.isoformat()}

User memory:
{memory}

COROS tool calls and results:
{json.dumps(tool_results, ensure_ascii=False, indent=2)}

Generate the morning sleep and recovery report.
""".strip(),
        ),
        _llm_timeout_seconds(),
    )


async def check_and_send_coros_sleep_report(
    client: discord.Client,
    force_send: bool = False,
) -> str:
    global _job_running
    if _job_running:
        return "COROS sleep report skipped: previous job is still running."

    if not force_send and not _daily_job_due():
        return "COROS sleep report skipped: not scheduled time yet."

    target_day = _target_sleep_day()
    if not force_send and _has_sent(target_day):
        return "COROS sleep report skipped: already sent today."

    new_trace("cron")
    _job_running = True
    try:
        _log_sleep_report("channel_lookup_start")
        channel = await _report_channel(client)
        if channel is None:
            return "COROS sleep report skipped: DISCORD_RUNNING_CHANNEL_ID is invalid."
        _log_sleep_report("channel_lookup_end")

        tool_results: list[dict[str, Any]] | None = None
        if not force_send:
            _log_sleep_report(f"sleep_data_lookup_start date={target_day.isoformat()}")
            tool_results = await _collect_sleep_tool_results(target_day)
            if not _sleep_data_available(tool_results):
                _log_sleep_report(f"sleep_data_unavailable date={target_day.isoformat()}")
                return "COROS sleep report skipped: sleep data not available yet."
            _log_sleep_report(f"sleep_data_lookup_end date={target_day.isoformat()}")

        await channel.send(f"早上好，正在分析 {target_day.isoformat()} 的 COROS 睡眠与恢复数据...")
        report = await generate_sleep_report(target_day, tool_results)
        await _send_chunks(channel, report)
        _mark_sent(target_day)
        _log_sleep_report(f"mark_sleep_report_sent date={target_day.isoformat()}")
        return "COROS sleep report sent."
    except Exception as exc:
        return f"COROS sleep report failed: {exc}"
    finally:
        _job_running = False


async def _scheduled_check(client: discord.Client) -> None:
    started_at = datetime.now(UTC)
    timeout_seconds = _check_timeout_seconds()
    _log_sleep_report(f"check_start timeout_seconds={timeout_seconds}")
    try:
        result = await asyncio.wait_for(
            check_and_send_coros_sleep_report(client),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        result = f"COROS sleep report failed: check timed out after {timeout_seconds}s."
    elapsed = (datetime.now(UTC) - started_at).total_seconds()
    _log_sleep_report(f"check_end elapsed={elapsed:.1f}s result={result}")


def register_coros_sleep_report(client: discord.Client) -> None:
    if not _enabled():
        _log_sleep_report("scheduler_disabled")
        return

    add_interval_job(
        "coros-sleep-report",
        _scheduled_check,
        _poll_minutes(),
        args=[client],
    )
    _log_sleep_report(
        f"scheduler_started poll_minutes={_poll_minutes()} "
        f"window={_window_start_time().strftime('%H:%M')}-{_window_end_time().strftime('%H:%M')} "
        f"timezone={_timezone().key}"
    )
