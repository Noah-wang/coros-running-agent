"""链路追踪与用量统计。

原来的日志是 `print(f"orchestrator {message}")`——没有请求标识，没有结构化字段。
2026-08-22 那次卡死暴露了它的代价：日志里只有一行「进了循环」然后没有下文，
既不知道卡在哪个工具上，也无法把这一行和同时段其他请求区分开。

这里补三样东西：

- **trace_id**：一次用户请求从入口到工具执行共用一个 id，用 ContextVar 传递，
  所以跨 await 自动带过去，不需要每个函数多加一个参数。
- **结构化事件**：一行一个事件，`key=value` 形式，能直接 grep 也能喂给日志系统。
- **用量累计**：每次模型调用的 token 数。改成工具循环之后，一条用户消息会触发
  2-5 次模型调用，而在此之前**没有任何地方在数这个**。

刻意没有引入 OpenTelemetry。这个系统只有一个进程，跨服务追踪用不上，
而多一个依赖就多一处会在半夜自己升级的东西——mcp-remote 那次教训还热着。
"""

import contextvars
import hashlib
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")
_lock = threading.Lock()


@dataclass
class Usage:
    """进程启动以来的模型用量累计。"""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    by_trace: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


_usage = Usage()


def new_trace(prefix: str = "req") -> str:
    """给一次请求开一个新的 trace，返回它的 id。"""
    trace_id = f"{prefix}-{uuid.uuid4().hex[:10]}"
    _trace_id.set(trace_id)
    return trace_id


def current_trace() -> str:
    return _trace_id.get()


def _format_value(value: Any) -> str:
    text = str(value)
    if any(char in text for char in " \t\"="):
        text = text.replace('"', "'")
        return f'"{text}"'
    return text or "-"


def log_event(event: str, **fields: Any) -> None:
    """打一条结构化事件。

    单行 key=value，既能 grep 也能被日志系统解析。第一个字段固定是 trace，
    因为排查时最常用的动作是「把这次请求的所有行捞出来」。
    """
    parts = [f"evt={event}", f"trace={current_trace()}"]
    parts.extend(f"{key}={_format_value(value)}" for key, value in fields.items())
    print(" ".join(parts), flush=True)


def log_prompts_enabled() -> bool:
    """是否把完整 Prompt 打进日志。

    默认关闭。Prompt 里有用户的成绩、伤病、目标这些个人数据，而服务器日志
    是 journalctl 里的明文。需要复现模型异常输出时再临时打开。
    """
    value = os.getenv("LOG_PROMPTS", "false")
    return value.lower() in {"1", "true", "yes", "on"}


def prompt_digest(text: str) -> str:
    """Prompt 的指纹，用于在不落明文的前提下确认两次送进去的是不是同一份。"""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def record_usage(model: str, prompt_tokens: int, completion_tokens: int) -> None:
    trace_id = current_trace()
    with _lock:
        _usage.calls += 1
        _usage.prompt_tokens += prompt_tokens
        _usage.completion_tokens += completion_tokens
        bucket = _usage.by_trace.setdefault(
            trace_id, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        )
        bucket["calls"] += 1
        bucket["prompt_tokens"] += prompt_tokens
        bucket["completion_tokens"] += completion_tokens
        # 只留最近的若干条，否则长期运行会把这个字典撑爆——
        # 它是用来在请求结束时汇总的，不是长期存储。
        if len(_usage.by_trace) > 256:
            for key in list(_usage.by_trace)[:128]:
                if key != trace_id:
                    del _usage.by_trace[key]

    log_event(
        "llm_call",
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def trace_usage(trace_id: str | None = None) -> dict[str, int]:
    """某一次请求消耗了多少。"""
    with _lock:
        return dict(
            _usage.by_trace.get(
                trace_id or current_trace(),
                {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0},
            )
        )


def usage_snapshot() -> dict[str, int]:
    """进程启动以来的总用量。"""
    with _lock:
        return {
            "calls": _usage.calls,
            "prompt_tokens": _usage.prompt_tokens,
            "completion_tokens": _usage.completion_tokens,
            "total_tokens": _usage.total_tokens,
        }


class Span:
    """给一段处理计时，进入和退出各打一条事件。"""

    def __init__(self, name: str, **fields: Any) -> None:
        self._name = name
        self._fields = fields
        self._started = 0.0

    def __enter__(self) -> "Span":
        self._started = time.monotonic()
        log_event(f"{self._name}_start", **self._fields)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        elapsed = time.monotonic() - self._started
        usage = trace_usage()
        log_event(
            f"{self._name}_end",
            elapsed_ms=int(elapsed * 1000),
            llm_calls=usage["calls"],
            total_tokens=usage["prompt_tokens"] + usage["completion_tokens"],
            failed=exc is not None,
            **self._fields,
        )
