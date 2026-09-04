"""模型用量账本。

费用是**估算**，不是账单。这里主要钉两件事：
没配单价的模型不能按 0 计入合计，以及账本坏掉不能拖垮请求。
"""

import json
import os
import tempfile
from pathlib import Path

import pytest

from src.runtime import usage_store as us


@pytest.fixture(autouse=True)
def _tmp_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(us, "USAGE_PATH", tmp_path / "usage.json")
    monkeypatch.delenv("LLM_PRICING", raising=False)
    yield


def test_accumulates_by_model():
    us.record("deepseek-chat", 1000, 200)
    us.record("deepseek-chat", 2000, 300)
    s = us.summary()
    agg = s["by_model"]["deepseek-chat"]
    assert agg["calls"] == 2
    assert agg["prompt_tokens"] == 3000
    assert agg["total_tokens"] == 3500


def test_unpriced_model_is_excluded_not_zeroed():
    """按 0 计会让合计偏低——偏低的数字比「不知道」更容易让人做错决定。"""
    us.record("some-unknown-model", 1_000_000, 0)
    s = us.summary()
    assert s["by_model"]["some-unknown-model"]["estimated_cost"] is None
    assert "some-unknown-model" in s["unpriced_models"]
    assert s["estimated_cost"] == 0.0


def test_pricing_can_be_overridden(monkeypatch):
    monkeypatch.setenv("LLM_PRICING", json.dumps({"deepseek-chat": {"input": 10, "output": 20}}))
    us.record("deepseek-chat", 1_000_000, 1_000_000)
    s = us.summary()
    assert s["by_model"]["deepseek-chat"]["estimated_cost"] == 30.0


def test_broken_ledger_does_not_raise(monkeypatch, tmp_path):
    bad = tmp_path / "usage.json"
    bad.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(us, "USAGE_PATH", bad)
    us.record("deepseek-chat", 100, 10)      # 不能抛
    assert us.summary()["by_model"]["deepseek-chat"]["calls"] == 1


def test_survives_restart(monkeypatch, tmp_path):
    """进程级统计重启清零，落盘的这份不能。"""
    us.record("deepseek-chat", 500, 50)
    assert us.summary()["by_model"]["deepseek-chat"]["calls"] == 1
    # 重新读盘（模拟新进程）
    assert us._load()["daily"]
