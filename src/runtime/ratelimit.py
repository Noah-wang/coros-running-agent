"""公开入口的限流。

网页控制台没有认证，任何人都能对话。改成工具循环之后一条消息要触发
2-5 次模型调用，所以「有人一直发消息」不只是吵，是直接烧钱。

这里做两层：

- **按来源限流**：挡住单个 IP 的高频请求。
- **全局限流**：挡住分散来源。只按 IP 限流对付不了「很多 IP 各发几条」，
  而账单是按总量算的——**能保护预算的是全局那层，不是按 IP 那层**。

用滑动窗口而不是令牌桶：窗口实现更短，而且这里不需要允许突发。
ThreadingHTTPServer 是多线程的，所以状态要上锁。
"""

import os
import threading
import time
from collections import deque

DEFAULT_PER_SOURCE_PER_MINUTE = 12
DEFAULT_GLOBAL_PER_MINUTE = 60
WINDOW_SECONDS = 60.0
# 超过这个数量的来源就清理一次，防止长期运行时字典无限增长
MAX_TRACKED_SOURCES = 4096

_lock = threading.Lock()
_hits: dict[str, deque[float]] = {}
_global_hits: deque[float] = deque()


def _limit(name: str, default: int) -> int:
    value = os.getenv(name, str(default))
    try:
        return max(int(value), 0)
    except ValueError:
        return default


def _trim(window: deque[float], now: float) -> None:
    cutoff = now - WINDOW_SECONDS
    while window and window[0] < cutoff:
        window.popleft()


def check(source: str) -> tuple[bool, int]:
    """这次请求是否放行。返回 (放行, 建议等待秒数)。

    只有放行时才计数——被拒的请求不该把窗口填得更满，否则一次超限会
    把后面的正常请求一起拖长，变成惩罚而不是限流。
    """
    per_source = _limit("WEB_RATE_LIMIT_PER_MINUTE", DEFAULT_PER_SOURCE_PER_MINUTE)
    per_global = _limit("WEB_RATE_LIMIT_GLOBAL_PER_MINUTE", DEFAULT_GLOBAL_PER_MINUTE)
    if per_source <= 0 and per_global <= 0:
        return True, 0

    now = time.monotonic()
    with _lock:
        _trim(_global_hits, now)
        if per_global > 0 and len(_global_hits) >= per_global:
            return False, max(int(WINDOW_SECONDS - (now - _global_hits[0])), 1)

        window = _hits.setdefault(source, deque())
        _trim(window, now)
        if per_source > 0 and len(window) >= per_source:
            return False, max(int(WINDOW_SECONDS - (now - window[0])), 1)

        window.append(now)
        _global_hits.append(now)

        if len(_hits) > MAX_TRACKED_SOURCES:
            for key in [k for k, v in _hits.items() if not v][:MAX_TRACKED_SOURCES // 2]:
                del _hits[key]

    return True, 0


def reset() -> None:
    """清空计数。给评测用，让每个用例从干净状态开始。"""
    with _lock:
        _hits.clear()
        _global_hits.clear()
