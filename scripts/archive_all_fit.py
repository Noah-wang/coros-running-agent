"""把 COROS 上的全部活动 FIT 文件归档到本地。

和 `!coros-fit-sync` 的区别是规模：那个命令默认只处理最近 10 条
（`COROS_FIT_ARCHIVE_SYNC_LIMIT`），这个脚本处理全部历史。

**整个任务是幂等的**：`archive_fit_for_activity` 会先检查本地是否已有文件，
有就直接跳过。所以中断了重跑即可，不会重复下载，也不需要记录断点。

**按年分段查询**而不是一次查全部历史。COROS 的列表接口单次返回有上限
（实测传 limit=2000 也只回 500 条），一次查全量在活动数超过上限时会
**静默截断**——返回条数正好等于上限，看不出少了东西。分年查再合并，
每一段都远小于上限，就不会踩这个。

每条之间有一个小停顿。近千次外部调用，稍微放慢一点是很便宜的保险。

**COROS 对 FIT 下载有每日配额**（实测约 50 次/天）。配额用完之后，
`downloadActivityFitFiles` 不报错，只是返回空——**和「这条活动本来就没有 FIT」
长得一模一样**。所以脚本带一个熔断：连续失败到阈值就停。

这个坑值得记一笔：第一次跑的时候是按日期从新到旧处理的，于是「配额耗尽」
和「超过某个日期就没有文件」产生了**完全一样的信号**——前 N 条成功、
之后全失败、分界线正好落在某个日期上。当时据此得出了「COROS 只保留 90 天」
的结论，是错的。真正的判据是把一条**刚成功下载过**的活动再下一次：
如果它也失败，那就与日期无关。
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT_DIR / ".env")

from agents.coros_report.activity_browser import _date_range_arguments, _normalize_activity  # noqa: E402
from agents.coros_report.auto_report import _activity_records  # noqa: E402
from agents.coros_report.fit_archive import (  # noqa: E402
    _existing_fit_paths,
    archive_fit_for_activity,
    fit_archive_enabled,
)

from src.integrations.coros_mcp import call_coros_tool  # noqa: E402

DEFAULT_START_YEAR = 2015
PAUSE_SECONDS = 1.0
# 连续失败到这个数就停。配额耗尽和系统性故障都会表现为连续失败，
# 两种情况下继续跑都没有意义，只是白烧调用次数。
CONSECUTIVE_FAILURE_LIMIT = 5


async def collect_activities(start_year: int, end_year: int) -> list[dict]:
    """按年查询并合并。返回按时间倒序的活动列表。"""
    seen: dict[str, dict] = {}
    for year in range(start_year, end_year + 1):
        arguments = _date_range_arguments(f"{year}0101", f"{year}1231", limit=2000)
        try:
            payload = await call_coros_tool("querySportRecords", arguments)
        except Exception as exc:
            print(f"  {year}: 查询失败 {exc}", flush=True)
            continue
        records = [_normalize_activity(r) for r in _activity_records(payload)]
        for record in records:
            seen[record["activityKey"]] = record
        print(f"  {year}: {len(records)} 条", flush=True)

    return sorted(
        seen.values(), key=lambda r: int(r.get("sortTimestamp") or 0), reverse=True
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条，0 表示全部")
    parser.add_argument(
        "--max-downloads",
        type=int,
        default=0,
        help="本次最多成功下载多少条，0 表示不限。用它把单次运行控制在每日配额以内",
    )
    parser.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR)
    parser.add_argument("--end-year", type=int, default=time.gmtime().tm_year)
    args = parser.parse_args()

    if not fit_archive_enabled():
        print("FIT 归档被关闭（COROS_FIT_ARCHIVE_ENABLED）。", flush=True)
        return

    print("按年查询活动列表：", flush=True)
    activities = await collect_activities(args.start_year, args.end_year)
    print(f"合计 {len(activities)} 条活动", flush=True)

    pending = [a for a in activities if not _existing_fit_paths(a)]
    print(f"已归档 {len(activities) - len(pending)} 条，待处理 {len(pending)} 条", flush=True)

    if args.limit:
        pending = pending[: args.limit]
        print(f"本次只处理前 {len(pending)} 条", flush=True)
    if not pending:
        print("没有需要处理的活动。", flush=True)
        return

    started = time.monotonic()
    archived = failed = 0
    consecutive_failures = 0
    stopped_early = ""
    for index, activity in enumerate(pending, start=1):
        label = f"{activity.get('date', '?')} {activity.get('sportName') or activity.get('name') or '运动'}"
        try:
            result = await archive_fit_for_activity(activity)
        except Exception as exc:
            failed += 1
            print(f"[{index}/{len(pending)}] {label} 异常：{exc}", flush=True)
        else:
            if result.paths:
                archived += 1
                consecutive_failures = 0
                print(f"[{index}/{len(pending)}] {label} ✓ {len(result.paths)} 个文件", flush=True)
            else:
                failed += 1
                consecutive_failures += 1
                print(f"[{index}/{len(pending)}] {label} ✗ {result.message}", flush=True)

        if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
            stopped_early = (
                f"连续 {consecutive_failures} 条失败，停止。"
                "最可能是当天的下载配额已用完，明天再跑即可——"
                "已下载的不会重复下载。"
            )
            break
        if args.max_downloads and archived >= args.max_downloads:
            stopped_early = f"已达到本次上限 {args.max_downloads} 条，停止。"
            break

        elapsed = time.monotonic() - started
        rate = elapsed / index
        remaining = rate * (len(pending) - index)
        if index % 10 == 0 or index == len(pending):
            print(
                f"  —— 进度 {index}/{len(pending)}  成功 {archived}  失败 {failed}  "
                f"平均 {rate:.1f}s/条  预计还需 {remaining / 60:.0f} 分钟",
                flush=True,
            )
        await asyncio.sleep(PAUSE_SECONDS)

    if stopped_early:
        print(stopped_early, flush=True)
    print(
        f"完成：成功 {archived}，失败 {failed}，"
        f"耗时 {(time.monotonic() - started) / 60:.1f} 分钟",
        flush=True,
    )
    print(f"剩余待处理约 {len(pending) - archived} 条。重跑本脚本即可续传。", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
