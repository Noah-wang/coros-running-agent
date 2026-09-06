"""自动报告的「这次运动同步完了没有」判定。

原来的规则是按时钟等：运动结束满 60 分钟，再确认一次列表摘要没变。
两个毛病，合起来就是「报告不完整」：

1. 盯错了数据。摘要（距离/时长/配速/心率）来自 querySportRecords，
   手表一上传就定了；报告却是拿 getActivityDetail 和 queryActivityLapData
   生成的，那时候还在补。盯着封面判断书印完了没有。
2. 只确认一次。时间一到、摘要一对上就发。

改后的规则：连续 N 次读到完全一样的**报告数据**才发。
"""

import asyncio
import os
import tempfile

import pytest

os.environ.setdefault("COROS_RUNTIME_SETTINGS_PATH", tempfile.mktemp())

from agents.coros_report import auto_report as ar  # noqa: E402

ACTIVITY = {
    "labelId": "480137651821772802",
    "sportType": 100,
    "startTimestamp": 1788649485,
    "endTimestamp": 1788658676,
    "distanceKm": 25.01,
    "duration": "2:22:53",
    "averagePace": "5:43",
    "averageHeartRate": 154,
}


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(ar, "get_agent_cache", lambda _: store)
    monkeypatch.setattr(ar, "update_agent_cache", lambda _, patch: store.update(patch))
    monkeypatch.setenv("COROS_AUTO_REPORT_STABLE_CHECKS", "2")
    monkeypatch.setenv("COROS_AUTO_REPORT_STABLE_MINUTES", "0")
    yield


def _serve(payloads, calls=None):
    """按工具名返回固定 payload，模拟 COROS 详情接口。"""

    async def fake(tool_name, arguments):
        if calls is not None:
            calls.append(tool_name)
        value = payloads[tool_name]
        if isinstance(value, Exception):
            raise value
        return value

    return fake


def _stable(monkeypatch, detail, laps, calls=None):
    monkeypatch.setattr(
        ar, "call_coros_tool", _serve({"getActivityDetail": detail,
                                       "queryActivityLapData": laps}, calls)
    )
    return asyncio.run(ar._activity_stable_enough(ACTIVITY))


def test_first_sighting_does_not_send(monkeypatch):
    stable, reason = _stable(monkeypatch, {"d": 1}, {"l": 1})
    assert stable is False
    assert "first sighting" in reason


def test_sends_after_two_unchanged_checks(monkeypatch):
    assert _stable(monkeypatch, {"d": 1}, {"l": 1})[0] is False   # 首次
    assert _stable(monkeypatch, {"d": 1}, {"l": 1})[0] is False   # 未变 1/2
    stable, reason = _stable(monkeypatch, {"d": 1}, {"l": 1})     # 未变 2/2
    assert stable is True
    assert "2/2" in reason


def test_detail_still_syncing_resets_the_count(monkeypatch):
    """这条是「报告不完整」的正题：摘要没动，详情还在补，就不能发。"""
    _stable(monkeypatch, {"d": 1}, {"l": 1})
    _stable(monkeypatch, {"d": 1}, {"l": 1})            # 已经 1/2
    stable, reason = _stable(monkeypatch, {"d": 1}, {"l": 2})  # 分圈数据又多了一圈
    assert stable is False
    assert "changed" in reason


def test_lap_data_is_actually_watched(monkeypatch):
    """指纹必须真的盖住分圈数据——只看摘要的话上一条测试会假绿。"""
    calls: list[str] = []
    _stable(monkeypatch, {"d": 1}, {"l": 1}, calls)
    assert "getActivityDetail" in calls
    assert "queryActivityLapData" in calls


def test_probe_failure_is_not_treated_as_stable(monkeypatch):
    """取不到 ≠ 没变化。把失败当稳定，会在 COROS 抽风时把半份报告发出去。"""
    _stable(monkeypatch, {"d": 1}, {"l": 1})
    _stable(monkeypatch, {"d": 1}, {"l": 1})            # 已经 1/2，再一次就发
    stable, reason = _stable(monkeypatch, {"d": 1}, RuntimeError("mcp down"))
    assert stable is False
    assert "unavailable" in reason


def test_probe_failure_does_not_reset_progress(monkeypatch):
    """抽风一次不该把已经数到的次数清零，否则 COROS 不稳时报告永远发不出去。"""
    _stable(monkeypatch, {"d": 1}, {"l": 1})
    _stable(monkeypatch, {"d": 1}, {"l": 1})            # 1/2
    _stable(monkeypatch, {"d": 1}, RuntimeError("mcp down"))
    stable, reason = _stable(monkeypatch, {"d": 1}, {"l": 1})
    assert stable is True
    assert "2/2" in reason


def test_new_activity_starts_over(monkeypatch):
    other = dict(ACTIVITY, labelId="999")
    _stable(monkeypatch, {"d": 1}, {"l": 1})
    _stable(monkeypatch, {"d": 1}, {"l": 1})
    monkeypatch.setattr(
        ar, "call_coros_tool", _serve({"getActivityDetail": {"d": 1},
                                       "queryActivityLapData": {"l": 1}})
    )
    stable, reason = asyncio.run(ar._activity_stable_enough(other))
    assert stable is False
    assert "first sighting" in reason


def test_stable_minutes_floor_still_honored(monkeypatch):
    """服务器 .env 里写着这个钮。删掉代码会让那行配置静默失效。"""
    monkeypatch.setenv("COROS_AUTO_REPORT_STABLE_MINUTES", "60")
    monkeypatch.setattr(ar, "_activity_age_minutes", lambda _: 5.0)
    stable, reason = _stable(monkeypatch, {"d": 1}, {"l": 1})
    assert stable is False
    assert "waiting 60 minutes" in reason
