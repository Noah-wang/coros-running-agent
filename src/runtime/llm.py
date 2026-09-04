import json
import os
from collections.abc import Sequence
from typing import Any

from openai import AsyncOpenAI

from src.runtime.trace import (
    log_event,
    log_prompts_enabled,
    prompt_digest,
    record_usage,
)


def _client() -> AsyncOpenAI:
    api_key = os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("LLM_API_KEY is missing. Add it to .env.")

    return AsyncOpenAI(
        api_key=api_key,
        base_url=os.getenv("LLM_BASE_URL")
        or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )


def _model() -> str:
    return os.getenv("LLM_MODEL") or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")


def _observe(kind: str, messages: Sequence[dict[str, Any]], response: Any) -> None:
    """记录一次模型调用的用量和 Prompt 指纹。

    默认只记指纹不记明文：Prompt 里有成绩、伤病、目标这些个人数据，
    而服务器日志是 journalctl 里的明文。需要复现异常输出时用 LOG_PROMPTS=1 打开。
    """
    serialized = json.dumps(list(messages), ensure_ascii=False, default=str)
    log_event(
        f"{kind}_request",
        model=_model(),
        messages=len(messages),
        chars=len(serialized),
        digest=prompt_digest(serialized),
    )
    if log_prompts_enabled():
        log_event(f"{kind}_prompt", body=serialized[:8000])

    usage = getattr(response, "usage", None)
    if usage is not None:
        record_usage(
            _model(),
            int(getattr(usage, "prompt_tokens", 0) or 0),
            int(getattr(usage, "completion_tokens", 0) or 0),
        )


async def complete_text(
    system_prompt: str,
    user_prompt: str,
    history: Sequence[dict[str, str]] = (),
) -> str:
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    messages.extend(dict(message) for message in history)
    messages.append({"role": "user", "content": user_prompt})

    response = await _client().chat.completions.create(
        model=_model(),
        messages=messages,
    )
    _observe("text", messages, response)

    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("DeepSeek returned an empty response.")
    return content


# 哪些模型不支持 tool_choice="required"，探测一次就记住。
#
# 不写死模型名单：名单会过期，而且中转站可以把任何 ID 映射到任何后端。
# 让第一次失败告诉我们答案，之后不再重复付这次代价。
_REQUIRED_UNSUPPORTED: set[str] = set()


def _rejects_required(exc: Exception) -> bool:
    """这个报错是不是「不支持 required」，而不是别的问题。

    原来这里是裸的 except Exception——任何失败都退回 auto，
    于是鉴权失败、限流、schema 写错都会被当成「不支持 required」悄悄降级，
    真正的错误被这层回退盖住了。
    """
    text = str(exc).lower()
    return "tool_choice" in text and ("required" in text or "not support" in text)


async def complete_with_tools(
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]] | None = None,
    tool_choice: str = "auto",
) -> Any:
    """发一轮带工具的对话，返回模型的原始 message。

    返回的 message 可能带 tool_calls，也可能只有 content，由调用方的循环处理。

    tool_choice="required" 强制这一轮必须调工具。用它来保证模型不会
    拿对话历史里自己上一轮说过的话当数据源——那样它会连没查过的细节一起编出来。
    """
    model = _model()
    kwargs: dict[str, Any] = {"model": model, "messages": list(messages)}
    wants_required = bool(tools) and tool_choice == "required"

    if tools:
        kwargs["tools"] = list(tools)
        # 已知这个模型不支持就别再白试一次——那是每一轮都要多付的一次失败调用。
        kwargs["tool_choice"] = (
            "auto" if wants_required and model in _REQUIRED_UNSUPPORTED else tool_choice
        )

    try:
        response = await _client().chat.completions.create(**kwargs)
    except Exception as exc:
        if not wants_required or not _rejects_required(exc):
            raise
        # **这里丢掉的是防幻觉的那道闸门，必须留痕。**
        # required 保证第一轮一定去查数据；退回 auto 之后模型可以直接开口，
        # 也就可能拿对话历史里自己上一轮说过的话当数据源。
        _REQUIRED_UNSUPPORTED.add(model)
        log_event("tool_choice_downgraded", model=model, to="auto")
        kwargs["tool_choice"] = "auto"
        response = await _client().chat.completions.create(**kwargs)

    _observe("tools", messages, response)
    return response.choices[0].message


async def complete_json(system_prompt: str, user_prompt: str) -> dict[str, Any]:
    response = await _client().chat.completions.create(
        model=_model(),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
    )
    _observe("json", [{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_prompt}], response)

    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("DeepSeek returned an empty JSON response.")

    # 只取第一个 JSON 对象，忽略后面多出来的内容。
    #
    # 即使指定了 response_format=json_object，模型偶尔仍会把两个对象拼在一起
    # 返回，`json.loads` 直接抛 "Extra data: line 3 column 1"。
    # 实测踩过：照片意图识别间歇性失败，静默降级成关键词兜底——
    # **不报错、只是变笨**，所以很难发现。
    try:
        parsed, _ = json.JSONDecoder().raw_decode(content.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"模型返回的不是合法 JSON：{content[:120]}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"模型返回的 JSON 不是对象：{content[:120]}")
    return parsed
