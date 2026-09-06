"""推理模型的「思考」开关。

qwen3.8-flash 是推理模型：回答「用一句话说你好」用了 138 个 completion token，
其中 133 个是思考，正文只有 3 个字。思考按输出 token 计费，也占生成时间。

线上后果是运动报告压着 180 秒超时线，抖一下整份就丢。
同一次运动 A/B：思考开 109 秒 2078 字，思考关 34 秒 2100 字。

这里测的是开关本身的行为，不是模型质量。
"""

import os
import tempfile

import pytest

os.environ.setdefault("COROS_RUNTIME_SETTINGS_PATH", tempfile.mktemp())

from src.runtime import llm  # noqa: E402


def _off(kwargs) -> bool:
    body = kwargs.get("extra_body")
    return bool(body) and body.get("enable_thinking") is False


def test_default_is_off_everywhere(monkeypatch):
    monkeypatch.delenv("LLM_THINKING", raising=False)
    assert _off(llm._thinking_kwargs(default_on=False))
    assert not _off(llm._thinking_kwargs(default_on=True))


def test_env_on_overrides_call_site(monkeypatch):
    """总开关要能盖过调用点的默认值，否则关不回去。"""
    monkeypatch.setenv("LLM_THINKING", "on")
    assert llm._thinking_kwargs(default_on=False) == {}
    assert llm._thinking_kwargs(default_on=True) == {}


def test_env_off_overrides_call_site(monkeypatch):
    monkeypatch.setenv("LLM_THINKING", "off")
    assert _off(llm._thinking_kwargs(default_on=True))
    assert _off(llm._thinking_kwargs(default_on=False))


def test_unrecognised_value_falls_back_to_call_site(monkeypatch):
    """写错值不该静默变成「全开」——那会让报告悄悄慢三倍。"""
    monkeypatch.setenv("LLM_THINKING", "maybe")
    assert _off(llm._thinking_kwargs(default_on=False))


def test_body_is_not_shared_between_calls(monkeypatch):
    """返回的必须是副本。调用方往 extra_body 里塞东西不能污染模块常量。"""
    monkeypatch.delenv("LLM_THINKING", raising=False)
    first = llm._thinking_kwargs(default_on=False)
    first["extra_body"]["enable_thinking"] = "污染"
    second = llm._thinking_kwargs(default_on=False)
    assert second["extra_body"]["enable_thinking"] is False


@pytest.mark.parametrize("field", ["enable_thinking", "chat_template_kwargs"])
def test_both_spellings_are_sent(monkeypatch, field):
    """两种写法一起发：换模型/换中转站时不至于一种失效就全失效。"""
    monkeypatch.delenv("LLM_THINKING", raising=False)
    assert field in llm._thinking_kwargs(default_on=False)["extra_body"]
