"""联网搜索。

系统里第一个把**开放互联网上的文本**引进上下文的通道。这决定了它的两条硬约束：

**一、结果是最不可信的内容。** 书籍原文和 B 站字幕至少是用户自己挑的，
搜索结果是任意第三方写的。所以对应的工具必须标 `returns_untrusted=True`——
读过它的那一轮不允许再调写工具，投毒→诱导写入这条链就断在中间。

**二、查询词是一条外发通道。** 之前系统里没有通用 HTTP 抓取工具，所以
「攻击者能让 agent 做错事，但很难把数据送出去」。搜索补上了这条腿：
查询词由模型生成，理论上可以把用户的成绩、目标编进去发出去。带宽很低，
但确实存在，所以**每一次查询词都记进日志**——挡不住就至少要看得见。

还有一条不是安全而是钱：搜索按次收费。公开网页入口没有认证，
限流只保证「每分钟不超过 N 次」，一天下来仍然可能烧掉整个额度。
所以有独立的**每日预算**，用完就明确告诉模型今天不能再搜了。
这是 COROS 那次配额耗尽的教训：外部配额一定要在自己这边也记一份，
否则只能等对方返回空，而对方返回空和「本来就没结果」长得一样。
"""

import asyncio
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

from src.runtime.memory import get_agent_cache, update_agent_cache
from src.runtime.trace import log_event

CACHE_NAME = "web-search"
DEFAULT_DAILY_LIMIT = 50
DEFAULT_MAX_RESULTS = 5
TIMEOUT_SECONDS = 20


class SearchUnavailable(RuntimeError):
    """没配置搜索服务，或今天的预算已经用完。"""


def _daily_limit() -> int:
    value = os.getenv("WEB_SEARCH_DAILY_LIMIT", str(DEFAULT_DAILY_LIMIT))
    try:
        return max(int(value), 0)
    except ValueError:
        return DEFAULT_DAILY_LIMIT


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def usage_today() -> int:
    cache = get_agent_cache(CACHE_NAME)
    if cache.get("date") != _today():
        return 0
    return int(cache.get("count", 0) or 0)


def _consume_budget() -> None:
    """占用一次预算。超了就抛 SearchUnavailable。"""
    limit = _daily_limit()
    used = usage_today()
    if limit and used >= limit:
        raise SearchUnavailable(
            f"今天的联网搜索次数已用完（{used}/{limit}）。明天会重置。"
        )
    update_agent_cache(CACHE_NAME, {"date": _today(), "count": used + 1})


def configured_provider() -> str:
    """按 key 是否存在自动判断用哪家。显式指定优先。"""
    explicit = os.getenv("WEB_SEARCH_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    if os.getenv("TAVILY_API_KEY"):
        return "tavily"
    if os.getenv("BRAVE_SEARCH_API_KEY"):
        return "brave"
    return ""


def search_configured() -> bool:
    return bool(configured_provider())


def _request_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": "COROSRunningAgent/0.1", **(headers or {})},
        method="POST" if data else "GET",
    )
    if data:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _tavily(query: str, max_results: int) -> list[dict[str, str]]:
    payload = _request_json(
        "https://api.tavily.com/search",
        payload={
            "api_key": os.getenv("TAVILY_API_KEY", ""),
            "query": query,
            "max_results": max_results,
        },
    )
    return [
        {
            "title": str(item.get("title", "")),
            "url": str(item.get("url", "")),
            "snippet": str(item.get("content", "")),
        }
        for item in (payload.get("results") or [])
        if isinstance(item, dict)
    ]


def _brave(query: str, max_results: int) -> list[dict[str, str]]:
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
        {"q": query, "count": max_results}
    )
    payload = _request_json(
        url,
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": os.getenv("BRAVE_SEARCH_API_KEY", ""),
        },
    )
    results = (payload.get("web") or {}).get("results") or []
    return [
        {
            "title": str(item.get("title", "")),
            "url": str(item.get("url", "")),
            "snippet": str(item.get("description", "")),
        }
        for item in results
        if isinstance(item, dict)
    ]


PROVIDERS = {"tavily": _tavily, "brave": _brave}


async def search_web(query: str, max_results: int = DEFAULT_MAX_RESULTS) -> dict[str, Any]:
    """搜一次网。返回标题、链接和摘要。"""
    query = query.strip()
    if not query:
        return {"error": "搜索词不能为空。"}

    provider = configured_provider()
    if provider not in PROVIDERS:
        raise SearchUnavailable(
            "没有配置联网搜索。在 .env 里设置 TAVILY_API_KEY 或 BRAVE_SEARCH_API_KEY。"
        )

    _consume_budget()
    # 查询词一定要记。它是模型生成的、会离开本机的字符串——
    # 这是系统里唯一的外发通道，挡不住就至少要看得见。
    log_event(
        "search_query",
        provider=provider,
        query=query[:200],
        used_today=usage_today(),
        limit=_daily_limit(),
    )

    runner = PROVIDERS[provider]
    try:
        results = await asyncio.to_thread(
            runner, query, max(1, min(max_results, 10))
        )
    except urllib.error.HTTPError as exc:
        return {"error": f"搜索服务返回 {exc.code}：{exc.reason}"}
    except Exception as exc:
        return {"error": f"搜索失败：{exc}"}

    log_event("search_result", provider=provider, count=len(results))
    if not results:
        return {"query": query, "results": [], "note": "没有搜到相关网页。"}
    return {"query": query, "results": results}
