"""睡眠报告的「睡醒了没」判定。

原来的判定是「有睡眠数据就发」。COROS 在人还睡着的时候就会同步出部分数据，
于是报告提前发出去，里面的数字之后还会变。

这里测的是改后的规则：连续 N 次读到完全一样的睡眠指标，才判定这一觉结束。
"""

import os
import tempfile

import pytest

os.environ.setdefault("COROS_RUNTIME_SETTINGS_PATH", tempfile.mktemp())

from agents.coros_report import sleep_report as sr


def _sleep(score: int, duration: str, steps: int = 2271):
    """按 COROS 的真实返回形状构造：MCP 结构，指标在 content[0].text 的纯文本里。

    同时带上 queryDailyHealthData——它含 Steps，全天都在变，
    **绝不能进指纹**，否则永远稳定不下来。
    """
    return [
        {
            "ok": True,
            "tool": {"name": "querySleepData"},
            "result": {
                "content": [{"type": "text", "text":
                    f"Sleep Data\n2026-09-01\nSleep Score: {score}\n"
                    f"Main Sleep: {duration}\nDeep Sleep Ratio: 20%"}],
            },
        },
        {
            "ok": True,
            "tool": {"name": "queryDailyHealthData"},
            "result": {
                "content": [{"type": "text", "text": f"Steps: {steps} | Calories: 191 kcal"}],
            },
        },
    ]


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(sr, "get_agent_cache", lambda _: store)
    monkeypatch.setattr(
        sr, "update_agent_cache", lambda _, patch: store.update(patch)
    )
    monkeypatch.setenv("COROS_SLEEP_REPORT_STABLE_CHECKS", "2")
    yield


def test_first_sighting_does_not_send():
    day = sr.date(2026, 8, 31)
    stable, reason = sr._sleep_stable_enough(day, _sleep(70, "5h 00min"))
    assert stable is False
    assert "first sighting" in reason


def test_changing_data_resets_the_count():
    day = sr.date(2026, 8, 31)
    sr._sleep_stable_enough(day, _sleep(70, "5h 00min"))       # 首次
    sr._sleep_stable_enough(day, _sleep(70, "5h 00min"))       # 未变 1/2
    stable, reason = sr._sleep_stable_enough(day, _sleep(74, "6h 00min"))  # 又睡了一小时
    assert stable is False
    assert "changed" in reason


def test_sends_after_two_unchanged_checks():
    day = sr.date(2026, 8, 31)
    assert sr._sleep_stable_enough(day, _sleep(82, "6h 57min"))[0] is False  # 首次
    assert sr._sleep_stable_enough(day, _sleep(82, "6h 57min"))[0] is False  # 1/2
    stable, reason = sr._sleep_stable_enough(day, _sleep(82, "6h 57min"))    # 2/2
    assert stable is True
    assert "2/2" in reason


def test_all_day_counters_do_not_count_as_change():
    """步数全天都在涨。它要是进了指纹，报告就永远发不出去——而且不报错。"""
    a = _sleep(82, "6h 57min", steps=2271)
    b = _sleep(82, "6h 57min", steps=9840)
    assert sr._sleep_signature(a) == sr._sleep_signature(b)


def test_sleep_change_does_count():
    a = _sleep(82, "6h 57min")
    b = _sleep(88, "7h 40min")
    assert sr._sleep_signature(a) != sr._sleep_signature(b)


def test_no_metrics_is_not_stable():
    day = sr.date(2026, 8, 31)
    stable, _ = sr._sleep_stable_enough(day, [{"ok": False, "tool": {"name": "querySleepData"}, "result": None}])
    assert stable is False


# ── 报告哪一天 ────────────────────────────────────────────────────────

def _has_sleep(day_label: str):
    return [{
        "ok": True,
        "tool": {"name": "querySleepData"},
        "result": {"content": [{"type": "text", "text":
            f"Sleep Data\n{day_label}\nSleep Score: 82\nMain Sleep: 6h 57min"}]},
    }]


def _no_sleep():
    """COROS 当天没睡也会返回一个带标题的空壳，这是真实返回形状。"""
    return [{
        "ok": True,
        "tool": {"name": "querySleepData"},
        "result": {"content": [{"type": "text", "text":
            "Sleep Data\n========\nNote: each record below is dated by its wake-up day."
            "\n\n2026-09-03\nNaps Total: 0 min"}]},
    }]


def test_empty_shell_is_not_counted_as_sleep():
    """标题里就有 "Sleep"，宽松匹配会误判成有数据——报告会声称在讲一个空的日子。"""
    assert sr._has_main_sleep(_no_sleep()) is False
    assert sr._has_main_sleep(_has_sleep("2026-09-03")) is True


def test_prefers_today_because_coros_dates_by_wakeup_day(monkeypatch):
    """COROS 按醒来那天标日期，所以早上跑的时候要问「今天」。"""
    import asyncio
    today = sr.date(2026, 9, 3)
    monkeypatch.setattr(sr, "_today", lambda: today)
    monkeypatch.setattr(sr, "_collect_sleep_tool_results",
                        lambda d: _async(_has_sleep("2026-09-03")))
    day, results = asyncio.run(sr._resolve_sleep_day())
    assert day == today
    assert results is not None


def test_falls_back_to_yesterday_when_today_not_synced(monkeypatch):
    """今天的还没从手表同步上来时，退回昨天——总比不发强。"""
    import asyncio
    today = sr.date(2026, 9, 3)
    monkeypatch.setattr(sr, "_today", lambda: today)
    monkeypatch.setattr(sr, "_collect_sleep_tool_results", lambda d: _async(_no_sleep()))
    day, results = asyncio.run(sr._resolve_sleep_day())
    assert day == sr.date(2026, 9, 2)
    assert results is None


async def _async(value):
    return value
