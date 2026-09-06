"""从历史运动记录里回填各距离的 PB。

`update_personal_bests_from_tool_results` 只在**生成新报告时**顺手更新 PB，
所以历史里的成绩一条都没进过记忆——memory.json 里 personal_bests 是空的。
这个脚本把历史补上，之后仍然由那条自动路径维护。

判定口径和自动路径保持一致（`personal_bests.TARGETS`）：
按**整场运动的总距离**落在目标距离的容差内来算，
也就是「你跑过的最快的一场 10 公里」，不是「长跑里最快的 10 公里分段」。
后者需要逐条拉分圈数据，五百多次运动就是五百多次接口调用。

只统计跑步。走路和骑行的 10 公里不该进 10K 的 PB。

默认只看不写。确认输出无误再加 --apply。

    uv run python scripts/backfill_personal_bests.py
    uv run python scripts/backfill_personal_bests.py --apply
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from agents.coros_report.personal_bests import (  # noqa: E402
    AGENT_NAME,
    TARGETS,
    _format_duration,
    _target_for_distance,
)
from scripts.archive_all_fit import collect_activities  # noqa: E402
from src.runtime.memory import get_agent_memory, update_agent_memory  # noqa: E402

# 只认跑步。COROS 的 sportName 是英文，走路/骑行/游泳都可能凑够距离，
# 但它们的 10 公里不是 10K 成绩。
RUN_KEYWORDS = ("run", "跑")


def is_run(activity: dict) -> bool:
    name = str(activity.get("sportName") or "").casefold()
    return any(keyword in name for keyword in RUN_KEYWORDS)


def duration_seconds(activity: dict) -> int | None:
    """列表里的 duration 形如 "2:22:53" 或 "48:12"。"""
    raw = str(activity.get("duration") or "").strip()
    if not raw:
        return None
    parts = raw.split(":")
    if not all(part.isdigit() for part in parts):
        return None
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    return None


def best_by_target(activities: list[dict]) -> dict[str, dict]:
    best: dict[str, dict] = {}
    for activity in activities:
        if not is_run(activity):
            continue
        try:
            distance = float(activity.get("distanceKm") or 0)
        except (TypeError, ValueError):
            continue
        if distance <= 0:
            continue

        target = _target_for_distance(distance)
        if target is None:
            continue
        seconds = duration_seconds(activity)
        if seconds is None or seconds <= 0:
            continue

        key = str(target["key"])
        if key in best and best[key]["seconds"] <= seconds:
            continue
        best[key] = {
            "distance": str(target["label"]),
            "distance_km": float(target["distance_km"]),
            "seconds": seconds,
            "time": _format_duration(seconds),
            "actual_km": round(distance, 2),
            "labelId": activity.get("labelId"),
            "sportType": activity.get("sportType"),
            "date": activity.get("date"),
            "sportName": activity.get("sportName"),
            "source": "coros_history_backfill",
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    return best


async def main() -> int:
    parser = argparse.ArgumentParser(description="从历史记录回填各距离 PB")
    parser.add_argument("--apply", action="store_true", help="真的写进长期记忆")
    parser.add_argument("--start-year", type=int, default=2015)
    args = parser.parse_args()

    print("正在按年拉取历史运动记录…")
    activities = await collect_activities(args.start_year, datetime.now().year)
    runs = [a for a in activities if is_run(a)]
    print(f"\n共 {len(activities)} 条运动，其中跑步 {len(runs)} 条")

    best = best_by_target(activities)
    existing = get_agent_memory(AGENT_NAME).get("personal_bests") or {}

    print("\n| 项目 | 成绩 | 实际距离 | 日期 | 现有记录 |")
    print("|---|---:|---:|---|---|")
    for target in TARGETS:
        key = str(target["key"])
        record = best.get(key)
        current = existing.get(key) if isinstance(existing, dict) else None
        current_text = current.get("time") if isinstance(current, dict) else "无"
        if record is None:
            print(f"| {target['label']} | 没有匹配的场次 | - | - | {current_text} |")
            continue
        print(
            f"| {target['label']} | {record['time']} | {record['actual_km']}km | "
            f"{record['date']} | {current_text} |"
        )

    if not best:
        print("\n没有可回填的成绩。")
        return 0

    if not args.apply:
        print("\n这是预演。确认无误后加 --apply 写入长期记忆。")
        return 0

    # 只在更快时覆盖：自动路径可能已经记了更好的成绩，回填不该把它压回去。
    merged = dict(existing) if isinstance(existing, dict) else {}
    written = 0
    for key, record in best.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(current.get("seconds"), int):
            if current["seconds"] <= record["seconds"]:
                continue
        merged[key] = record
        written += 1

    update_agent_memory(AGENT_NAME, {"personal_bests": merged})
    print(f"\n完成：写入 {written} 项 PB。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
