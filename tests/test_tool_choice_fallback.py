"""tool_choice="required" 不被支持时的降级。

required 是防幻觉的结构性机制：强制第一轮必须查数据，
模型就没机会拿对话历史里自己上一轮说过的话当数据源。

不是所有服务端都支持它（实测 qwen3.8-flash / qwen3.8-27b 直接 400）。
降级可以接受，但必须**精确、只付一次代价、留得下痕迹**。
"""

import pytest

from src.runtime import llm


@pytest.fixture(autouse=True)
def _clear_cache():
    llm._REQUIRED_UNSUPPORTED.clear()
    yield
    llm._REQUIRED_UNSUPPORTED.clear()


def test_recognises_the_unsupported_error():
    err = Exception(
        "Error code: 400 - InvalidParameter: The tool_choice parameter "
        "does not support being set to required or object"
    )
    assert llm._rejects_required(err) is True


def test_other_failures_are_not_treated_as_downgrade():
    """裸 except Exception 会把这些也当成「不支持 required」悄悄降级，
    真正的错误就被回退盖住了。"""
    for other in (
        "Error code: 401 - Invalid API key",
        "Error code: 402 - Insufficient Balance",
        "Error code: 429 - Rate limit exceeded",
        "Connection timeout",
    ):
        assert llm._rejects_required(Exception(other)) is False


@pytest.mark.parametrize("model", ["qwen3.8-flash", "some-other-model"])
def test_cache_is_per_model(model):
    llm._REQUIRED_UNSUPPORTED.add(model)
    assert model in llm._REQUIRED_UNSUPPORTED
    assert "deepseek-v4-flash" not in llm._REQUIRED_UNSUPPORTED
