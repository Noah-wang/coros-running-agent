"""模型用量的落盘统计和费用估算。

`trace.py` 里的 `Usage` 是**进程级**的：重启清零，而且只留最近 256 条 trace。
它的用途是在一次请求结束时汇总耗时和 token，不是长期账本。

想回答「这个月花了多少钱」就得另外存一份。这里按**天 × 模型**累计，
落在 `data/usage.json`，重启不丢。

**费用只是估算，不是账单。**
单价来自配置，默认值只是占位——你走的可能是中转站、可能有折扣、
可能按不同档计价。要准就去 `.env` 里按实际单价配 `LLM_PRICING`，
工具的输出会说明它用的是哪一套单价。
"""

import json
import os
import threading
from datetime import date, datetime
from typing import Any

from src.runtime.atomic import write_json_batch
from src.runtime.paths import DATA_DIR

USAGE_PATH = DATA_DIR / "usage.json"
_LOCK = threading.RLock()

# 只留最近这么多天。一天一个模型一条记录，60 天不会让文件变大到需要担心。
KEEP_DAYS = 60

# 默认单价：每百万 token，单位见 CURRENCY。来自 DeepSeek 官方公开价。
#
# **能看量级和趋势，不能用来对账**，有两个已知偏差：
#
# 1. **各来源对当前价格不一致。** 多数来源（2026 年 7~8 月核对）给的是
#    V4-Flash $0.14 / $0.28；有来源（9/3 核对）称已涨价并改成分时计价，
#    高峰是平峰两倍。这里取被引用最广的那组。
# 2. **缓存命中另算。** DeepSeek 对命中缓存的输入单独定价（远低于未命中），
#    这里没区分，所以估算通常**偏高**。
#
# 走中转站时以中转站的实际单价为准，用 LLM_PRICING 覆盖。
#
# 来源：https://api-docs.deepseek.com/quick_start/pricing
PRICING_NOTE = (
    "单价按 DeepSeek 官方公开价估算，未区分缓存命中（实际通常更低）；"
    "走中转站时以中转站计费为准。"
)

DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "deepseek-v4-flash": {"input": 0.14, "output": 0.28},
    "deepseek-v4-pro": {"input": 0.435, "output": 0.87},
    # qwen3.7-text-embedding 故意不填：没查到官方公开的每 token 单价。
    # 编一个数会让合计看起来精确而实际是错的——宁可显示「未配单价」，
    # 并在合计旁注明「实际花费高于这个数」。
}


def currency() -> str:
    return os.getenv("LLM_PRICE_CURRENCY", "USD")


def pricing() -> dict[str, dict[str, float]]:
    """单价表。环境变量里的条目覆盖默认值，其余保留。"""
    table = {name: dict(rates) for name, rates in DEFAULT_PRICING.items()}
    raw = os.getenv("LLM_PRICING", "").strip()
    if not raw:
        return table
    try:
        override = json.loads(raw)
    except json.JSONDecodeError:
        return table
    if not isinstance(override, dict):
        return table
    for name, rates in override.items():
        if isinstance(rates, dict):
            table[str(name)] = {
                "input": float(rates.get("input", 0) or 0),
                "output": float(rates.get("output", 0) or 0),
            }
    return table


def _load() -> dict[str, Any]:
    if not USAGE_PATH.exists():
        return {"daily": {}}
    try:
        data = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # 统计坏掉不该影响正常回答，当作空账本重新开始
        return {"daily": {}}
    return data if isinstance(data, dict) and "daily" in data else {"daily": {}}


def record(model: str, prompt_tokens: int, completion_tokens: int) -> None:
    """把一次调用累加进当天的账本。任何异常都不往上抛。"""
    if not model:
        return
    try:
        with _LOCK:
            data = _load()
            daily = data.setdefault("daily", {})
            today = date.today().isoformat()
            bucket = daily.setdefault(today, {}).setdefault(
                model, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
            )
            bucket["calls"] += 1
            bucket["prompt_tokens"] += int(prompt_tokens or 0)
            bucket["completion_tokens"] += int(completion_tokens or 0)

            for day in sorted(daily)[:-KEEP_DAYS]:
                del daily[day]

            data["updated_at"] = datetime.now().isoformat(timespec="seconds")
            write_json_batch([(USAGE_PATH, data)])
    except Exception:
        # 记账失败不能拖垮请求本身
        pass


def _cost(model: str, prompt_tokens: int, completion_tokens: int, table) -> float | None:
    rates = table.get(model)
    if not rates:
        return None
    return (
        prompt_tokens / 1_000_000 * rates.get("input", 0)
        + completion_tokens / 1_000_000 * rates.get("output", 0)
    )


def summary(days: int = 30) -> dict[str, Any]:
    """最近 N 天的用量和估算费用，按模型分组。"""
    data = _load()
    daily = data.get("daily", {})
    recent = sorted(daily)[-max(days, 1):]
    table = pricing()

    by_model: dict[str, dict[str, Any]] = {}
    for day in recent:
        for model, bucket in (daily.get(day) or {}).items():
            agg = by_model.setdefault(
                model, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
            )
            agg["calls"] += bucket.get("calls", 0)
            agg["prompt_tokens"] += bucket.get("prompt_tokens", 0)
            agg["completion_tokens"] += bucket.get("completion_tokens", 0)

    unpriced: list[str] = []
    total_cost = 0.0
    for model, agg in by_model.items():
        agg["total_tokens"] = agg["prompt_tokens"] + agg["completion_tokens"]
        cost = _cost(model, agg["prompt_tokens"], agg["completion_tokens"], table)
        if cost is None:
            # 没配单价的模型不能按 0 算——那会让总价看起来比实际低，
            # 而「偏低的数字」比「不知道」更容易让人放心地做错决定。
            agg["estimated_cost"] = None
            unpriced.append(model)
        else:
            agg["estimated_cost"] = round(cost, 4)
            total_cost += cost

    today = date.today().isoformat()
    today_bucket = daily.get(today, {})
    return {
        "active_model": os.getenv("LLM_MODEL") or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        "embedding_model": os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
        "window_days": len(recent),
        "by_model": by_model,
        "today": {
            model: {
                **bucket,
                "total_tokens": bucket.get("prompt_tokens", 0) + bucket.get("completion_tokens", 0),
            }
            for model, bucket in today_bucket.items()
        },
        "estimated_cost": round(total_cost, 4),
        "currency": currency(),
        "unpriced_models": unpriced,
        "pricing_used": {m: table[m] for m in by_model if m in table},
        "pricing_note": PRICING_NOTE if not os.getenv("LLM_PRICING", "").strip() else "单价来自 LLM_PRICING 配置。",
    }
