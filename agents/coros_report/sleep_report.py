import asyncio
import hashlib
import json
import os
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from agents.coros_report.sleep_report_prompt import SLEEP_REPORT_SYSTEM_PROMPT
from src.integrations.coros_mcp import call_coros_tool
from src.integrations.discord_forum import create_report_post
from src.runtime.llm import complete_text
from src.runtime.memory import format_memory_for_prompt, get_agent_cache, update_agent_cache
from src.runtime.scheduler import add_interval_job
from src.runtime.trace import new_trace
from src.runtime.prompt_skills import active_skill
from src.runtime.runtime_settings import automation_enabled


AGENT_NAME = "coros-report"
SLEEP_REPORT_CACHE_KEY = "sleep_report_sent_dates"
SLEEP_STABILITY_CACHE_KEY = "sleep_report_stability"
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
    return automation_enabled("sleep_report")


def _poll_minutes() -> int:
    return _env_int("COROS_SLEEP_REPORT_POLL_MINUTES", 30)


def _check_timeout_seconds() -> int:
    return _env_int("COROS_SLEEP_REPORT_TIMEOUT_SECONDS", 240, minimum=30)


def _required_stable_checks() -> int:
    """要连续几次读到同样的数据，才认为「睡醒了」。

    原来的判定是「有睡眠数据就发」，但 COROS 在人还睡着的时候就会同步出
    部分数据——于是报告在还没睡醒的时候就发出去了，数字后来还会变。

    「有数据」和「数据不再变了」是两件事。这里要的是后者：
    连续 N 次读到完全一样的睡眠指标，才判定这一觉结束了。
    N=2 配合 30 分钟轮询，等于数据稳定一小时后才发。
    """
    return _env_int("COROS_SLEEP_REPORT_STABLE_CHECKS", 2, minimum=1)


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


# 指纹只看**睡眠本身**的两个工具。
#
# 踩过两个坑，都会让功能彻底失效：
#
# 1. COROS 的返回是 MCP 结构，指标全在 content[0].text 的纯文本里，
#    不是结构化字段。按键名去匹配一次都命中不了，指纹恒为空。
# 2. queryDailyHealthData 里有 Steps 和 Calories，**全天都在变**。
#    把它算进指纹，数据就永远「稳定不下来」，报告永远发不出去——
#    而且不报错，只是安静地一直不发。
#
# queryRecoveryStatus 和 queryTrainingLoadAssessment 同理：它们跟着当天
# 的活动更新，和「这一觉睡完没有」无关。
SIGNATURE_TOOLS = ("querySleepData", "querySleepHrv")


def _tool_text(item: dict[str, Any]) -> str:
    """把一个 MCP 工具返回里的文本抠出来。"""
    result = item.get("result")
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return ""
    parts: list[str] = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(parts)


def _sleep_signature(tool_results: list[dict[str, Any]]) -> str:
    """把这一轮读到的睡眠数据压成一个稳定字符串。"""
    texts: list[str] = []
    for item in tool_results:
        if not item.get("ok"):
            continue
        tool = item.get("tool")
        name = tool.get("name") if isinstance(tool, dict) else tool
        if name in SIGNATURE_TOOLS:
            texts.append(f"{name}:{_tool_text(item)}")
    if not any(t.split(":", 1)[1].strip() for t in texts):
        return ""
    return hashlib.sha256("|".join(sorted(texts)).encode("utf-8")).hexdigest()[:16]


def _stability_cache() -> dict[str, Any]:
    cache = get_agent_cache(AGENT_NAME).get(SLEEP_STABILITY_CACHE_KEY, {})
    return cache if isinstance(cache, dict) else {}


def _mark_stability(day: date, signature: str, unchanged: int) -> None:
    update_agent_cache(
        AGENT_NAME,
        {
            SLEEP_STABILITY_CACHE_KEY: {
                "date": day.isoformat(),
                "signature": signature,
                "unchanged_checks": unchanged,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        },
    )


def _sleep_stable_enough(
    day: date, tool_results: list[dict[str, Any]]
) -> tuple[bool, str]:
    """睡眠数据是否已经连续 N 次没有变化。"""
    signature = _sleep_signature(tool_results)
    if not signature:
        return False, "no sleep metrics to compare yet"

    state = _stability_cache()
    same_day = state.get("date") == day.isoformat()
    if not same_day or state.get("signature") != signature:
        # 第一次见到这份数据（或者数据刚变过），计数归零重新观察
        _mark_stability(day, signature, 0)
        reason = "sleep data changed" if same_day else "first sighting today"
        return False, f"{reason}; restarting stability count"

    unchanged = int(state.get("unchanged_checks", 0)) + 1
    _mark_stability(day, signature, unchanged)
    required = _required_stable_checks()
    if unchanged < required:
        return False, f"sleep data unchanged {unchanged}/{required} checks"
    return True, f"sleep data unchanged {unchanged}/{required} checks"


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


def _sleep_duration_text(tool_results: list[dict[str, Any]], report: str = "") -> str:
    text = "\n".join([*(_tool_text(item) for item in tool_results), report])
    patterns = (
        r"(?:total\s+sleep|sleep\s+duration|睡眠时长|睡眠总量|总睡眠)\D{0,12}(\d{1,2})\s*(?:h|hr|hour|小时)\s*(\d{1,2})?\s*(?:m|min|minute|分钟)?",
        r"(?:total\s+sleep|sleep\s+duration|睡眠时长|睡眠总量|总睡眠)\D{0,12}(\d{1,3})\s*(?:m|min|minute|分钟)",
    )
    match = re.search(patterns[0], text, flags=re.IGNORECASE)
    if match:
        hours = int(match.group(1))
        minutes = int(match.group(2) or 0)
        return f"{hours}小时{minutes:02d}分"
    match = re.search(patterns[1], text, flags=re.IGNORECASE)
    if match:
        total_minutes = int(match.group(1))
        return f"{total_minutes // 60}小时{total_minutes % 60:02d}分"
    return "时长待确认"


def _sleep_title(day: date, tool_results: list[dict[str, Any]], report: str) -> str:
    return f"{day.isoformat()} 睡眠 {_sleep_duration_text(tool_results, report)}"


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
            active_skill("sleep", "Morning Recovery Coach", SLEEP_REPORT_SYSTEM_PROMPT).content,
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
    if not force_send and not _enabled():
        return "COROS sleep report skipped: automation is disabled."
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

            # 有数据 ≠ 睡完了。要连续几次读到一样的指标才发。
            stable, reason = _sleep_stable_enough(target_day, tool_results)
            _log_sleep_report(f"sleep_stability date={target_day.isoformat()} stable={stable} reason={reason}")
            if not stable:
                return f"COROS sleep report skipped: {reason}."

        report = await generate_sleep_report(target_day, tool_results)
        results = tool_results or []
        forum_post = await create_report_post(
            client,
            _sleep_title(target_day, results, report),
            report,
            "DISCORD_COROS_SLEEP_FORUM_TAG_ID",
        )
        if forum_post is None:
            await channel.send(f"早上好，已完成 {target_day.isoformat()} 的 COROS 睡眠与恢复分析。")
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
    add_interval_job(
        "coros-sleep-report",
        _scheduled_check,
        _poll_minutes(),
        args=[client],
    )
    _log_sleep_report(
        f"scheduler_started poll_minutes={_poll_minutes()} "
        f"window={_window_start_time().strftime('%H:%M')}-{_window_end_time().strftime('%H:%M')} "
        f"timezone={_timezone().key} enabled={_enabled()}"
    )
