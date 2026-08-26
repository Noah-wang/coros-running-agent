"""把指定 UP 主的新视频字幕同步进知识库。

每半小时跑一次：列出这些 UP 主的视频，跳过已经导入过的，抓字幕存进对应的分类目录，
最后重建索引。**新加进来的视频只需要嵌入自己那几块**，因为嵌入按内容哈希缓存。

**跑得勤 ≠ 抓得多。** 原来是每天一次抓 20 条，现在是每半小时一次抓 8 条：
单次的请求爆发反而更小，但一天能过三百多条，几百条的历史存量从「两周」压到「一天」。
代价是限流时也会每半小时撞一次，所以熔断之后要写冷静期文件，让后续几轮直接退出。

**状态就是磁盘上的文件本身**，不额外记断点。`videos/<分类>/` 下每个 md 的头部有
`Source: BV...`，扫一遍就知道哪些导过了。这样中断了重跑即可，也不会出现
「记录说导过、文件其实不在」的漂移——FIT 归档用的是同一个思路。

**限流卡在「列视频」而不是「抓字幕」。** 实测：字幕连抓 6 条（间隔 4 秒）
毫无问题，而空间列表接口打几次就 -799/412。所以视频列表**缓存到磁盘**，
默认 20 小时内不重复请求；列表拿不到时**退回用缓存继续回填**——
限流不该让整个回填停摆，它只该让「发现新视频」推迟到下一次。

这个顺序一开始是反的：每次运行都把所有 UP 主的列表翻一遍，
**把最贵的操作放在了每次都做的位置**。

**限流是这条链上最需要小心的地方。** B 站的空间接口在短时间多次请求后会返回
-799 或 412，而且**这两种回应和「这个 UP 主没有视频」在结构上很接近**。所以：

- 请求之间留足间隔，宁可慢
- 连续出错就熔断，不硬撑（硬撑只会把限流窗口拉得更长）
- **区分「这条视频本来就没字幕」和「被限流了」**：前者是正常情况，不该计入熔断，
  否则一串没字幕的视频会误触发停止。这是 FIT 那次的教训——
  当时「配额耗尽」和「本来就没文件」返回值一样，导致误判成日期截止。
"""

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT_DIR / ".env")

from src.integrations.bilibili_subtitle import (  # noqa: E402
    fetch_bilibili_subtitle,
    list_user_videos,
)
from src.runtime.knowledge_sources import load_sources  # noqa: E402
from src.runtime.trace import log_event, new_trace  # noqa: E402

VIDEOS_DIR = ROOT_DIR / "data" / "knowledge" / "coros-report" / "videos"
LIST_CACHE_DIR = ROOT_DIR / "data" / "knowledge" / "coros-report" / ".video-index"
# 列表缓存的有效期。定时器现在每半小时来一次，但**列表接口才是限流的瓶颈**，
# 所以刷新频率仍然按天算：20 小时保证一天能发现一次新视频，
# 中间那四十几轮全部命中缓存，一个列表请求都不发。
LIST_TTL_HOURS = 20

# 订阅名单不再硬编码，改由 data/knowledge/coros-report/sources.json 维护，
# 这样 Agent 能在对话里加新的 UP 主，不用改代码重新部署。

# 抓字幕之间的间隔。实测短时间密集请求会触发 -799，
# 而限流一旦触发，恢复要等的时间远超这里省下的。
VIDEO_PAUSE_SECONDS = 4.0
# UP 主之间也要留间隔。第一次实测就栽在这里：两次列表请求挨太近，
# 第二个 UP 主直接返回空——**而「被限流」和「这个人没发过视频」
# 在返回结构上一模一样**，不留意就会以为是数据问题。
SOURCE_PAUSE_SECONDS = 8.0
CONSECUTIVE_ERROR_LIMIT = 3
# 实测 15 条连抓（间隔 4 秒）零失败，瓶颈在列表接口不在字幕接口。
#
# 定时器改成每半小时一次之后，这个数从 20 降到 8：**总吞吐是靠跑得勤，
# 不是靠每次抓得多**。一次 8 条只占 32 秒，剩下 29 分钟全是静默期，
# 平均请求密度比原来每天一次抓 20 条还低，但一天能过 300 多条。
DEFAULT_MAX_VIDEOS = 8

# 熔断后的冷静期。每半小时来一次意味着限流时也会每半小时撞一次，
# 那只会让封禁时间越拖越长。踩到之后就先停两小时，
# 下一次定时任务读到这个文件直接退出。
COOLDOWN_HOURS = 2.0
COOLDOWN_PATH = LIST_CACHE_DIR / "cooldown.json"


def cooldown_remaining() -> float:
    """还要冷静多少秒。没在冷静期返回 0。"""
    if not COOLDOWN_PATH.exists():
        return 0.0
    try:
        until = float(json.loads(COOLDOWN_PATH.read_text(encoding="utf-8"))["until"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return 0.0
    return max(until - time.time(), 0.0)


def start_cooldown(reason: str) -> None:
    COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
    until = time.time() + COOLDOWN_HOURS * 3600
    COOLDOWN_PATH.write_text(
        json.dumps({"until": until, "reason": reason[:120]}, ensure_ascii=False),
        encoding="utf-8",
    )
    log_event("bilibili_sync_cooldown", hours=COOLDOWN_HOURS, reason=reason[:60])


def clear_cooldown() -> None:
    COOLDOWN_PATH.unlink(missing_ok=True)


def _cache_path(uid: int) -> Path:
    return LIST_CACHE_DIR / f"{uid}.json"


def load_cached_list(uid: int) -> tuple[list[dict], float]:
    """读缓存的视频列表，返回 (列表, 缓存时间戳)。"""
    path = _cache_path(uid)
    if not path.exists():
        return [], 0.0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], 0.0
    return data.get("videos", []), float(data.get("fetched_at", 0) or 0)


def save_cached_list(uid: int, videos: list[dict]) -> None:
    """把新拿到的列表**并入**缓存，而不是覆盖。

    翻页中途被限流时只能拿到前几页。直接覆盖的话，
    一次不完整的请求会把之前更完整的缓存冲掉——**部分结果不该让状态倒退**。
    实测踩过：某个 UP 主先缓存了较完整的历史列表，下一次只拿到前几页就把它盖了。

    按 bvid 合并，新数据优先（标题可能改过），顺序按发布时间从新到旧。
    """
    existing, _ = load_cached_list(uid)
    merged: dict[str, dict] = {v["bvid"]: v for v in existing if v.get("bvid")}
    merged.update({v["bvid"]: v for v in videos if v.get("bvid")})
    ordered = sorted(
        merged.values(), key=lambda v: int(v.get("created", 0) or 0), reverse=True
    )

    LIST_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(uid).write_text(
        json.dumps(
            {"uid": uid, "fetched_at": time.time(), "videos": ordered},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


async def get_video_list(uid: int, force_refresh: bool) -> tuple[list[dict], str]:
    """拿视频列表。优先用缓存，过期才去请求。返回 (列表, 来源说明)。

    请求失败时**退回缓存**而不是放弃：列表接口限流只应该推迟
    「发现新视频」，不该让历史回填也停下来。
    """
    cached, fetched_at = load_cached_list(uid)
    fresh_enough = time.time() - fetched_at < LIST_TTL_HOURS * 3600
    if cached and fresh_enough and not force_refresh:
        age_hours = (time.time() - fetched_at) / 3600
        return cached, f"缓存（{age_hours:.1f} 小时前）"

    try:
        videos = await list_user_videos(uid)
    except Exception as exc:
        # 截到 40 字会把最有用的部分砍掉：新用户第一次跑最常见的错误是
        # 「Bilibili config not found: <路径>」，而路径正好从第 40 个字开始。
        message = str(exc)[:160]
        if cached:
            return cached, f"请求失败退回缓存（{message}）"
        return [], f"请求失败且无缓存（{message}）"

    if not videos:
        if cached:
            return cached, "请求返回空，退回缓存"
        return [], "请求返回空且无缓存"

    save_cached_list(uid, videos)
    return videos, "已刷新"


def imported_bvids() -> set[str]:
    """扫描已有的 md，取出导过的 BV 号。磁盘即状态。"""
    found: set[str] = set()
    if not VIDEOS_DIR.exists():
        return found
    for path in VIDEOS_DIR.rglob("*.md"):
        match = re.search(r"Source:\s*(\S+)", path.read_text(encoding="utf-8")[:400])
        if match:
            found.add(match.group(1).strip())
    return found


def _safe_name(text: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\n\r\t]', "_", text).strip()
    return cleaned[:60] or "untitled"


def write_document(
    category: str, bvid: str, title: str, subtitle: str,
    uploader: str = "", uid: int = 0,
) -> Path:
    folder = VIDEOS_DIR / category
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{bvid}-{_safe_name(title)}.md"
    path.write_text(
        "# Running Video Knowledge\n\n"
        f"Source: {bvid}\n"
        f"Title: {title}\n"
        f"Uploader: {uploader}\n"
        f"UploaderId: {uid}\n"
        f"Imported at: {datetime.now(UTC).isoformat(timespec='seconds')}\n"
        "Content type: Bilibili subtitle transcript\n\n"
        "## Transcript\n\n"
        f"{subtitle.strip()}\n",
        encoding="utf-8",
    )
    return path


async def sync_source(
    uid: int, category: str, budget: int, dry_run: bool, force_refresh: bool
) -> tuple[int, int, bool]:
    """同步一个 UP 主。返回 (新导入数, 剩余预算, 是否触发熔断)。"""
    print(f"\n=== UP主 {uid} → {category} ===", flush=True)
    videos, origin = await get_video_list(uid, force_refresh)
    print(f"  列表来源：{origin}", flush=True)

    if not videos:
        print("  拿不到列表，本轮跳过这个来源。", flush=True)
        return 0, budget, False

    already = imported_bvids()
    pending = [v for v in videos if v.get("bvid") not in already]
    print(f"  该 UP 主 {len(videos)} 条，已导入 {len(videos) - len(pending)} 条，"
          f"待处理 {len(pending)} 条", flush=True)

    if dry_run:
        for video in pending[:budget]:
            print(f"    [dry-run] {video['bvid']}  {video.get('title','')[:44]}", flush=True)
        return 0, budget, False

    imported = 0
    consecutive_errors = 0
    no_subtitle = 0
    tripped = False

    for video in pending:
        if budget <= 0:
            print("  已用完本次配额，剩下的留给下一次。", flush=True)
            break

        bvid = video.get("bvid", "")
        title = str(video.get("title", "")).strip()
        try:
            subtitle = await fetch_bilibili_subtitle(bvid)
        except Exception as exc:
            message = str(exc)
            # 「没有中文字幕」是正常情况，不计入熔断——否则一串无字幕视频
            # 会被误判成被限流。这正是 FIT 那次踩过的坑。
            if "没有中文字幕" in message:
                no_subtitle += 1
                print(f"  - {bvid} 无字幕，跳过", flush=True)
            else:
                consecutive_errors += 1
                print(f"  ✗ {bvid} {message[:70]}", flush=True)
                if consecutive_errors >= CONSECUTIVE_ERROR_LIMIT:
                    tripped = True
                    print(f"  连续出错，停止本次同步（多半是触发了限流，"
                          f"进入 {COOLDOWN_HOURS:.0f} 小时冷静期，"
                          f"已导入的不会重复）。", flush=True)
                    break
            await asyncio.sleep(VIDEO_PAUSE_SECONDS)
            continue

        consecutive_errors = 0
        path = write_document(
            category, bvid, title, subtitle,
            uploader=str(video.get("author", "")).strip(), uid=uid,
        )
        imported += 1
        budget -= 1
        print(f"  ✓ {bvid} {title[:40]}  ({len(subtitle)} 字符 → {path.name})", flush=True)
        await asyncio.sleep(VIDEO_PAUSE_SECONDS)

    print(f"  小结：新导入 {imported}，无字幕 {no_subtitle}", flush=True)
    return imported, budget, tripped


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-videos", type=int, default=DEFAULT_MAX_VIDEOS,
                        help="本次最多导入多少条，控制在限流以内")
    parser.add_argument("--dry-run", action="store_true", help="只列出会导入什么，不抓不写")
    parser.add_argument("--no-reindex", action="store_true", help="导入后不重建索引")
    parser.add_argument("--refresh-list", action="store_true",
                        help="强制刷新视频列表，忽略缓存有效期")
    parser.add_argument("--ignore-cooldown", action="store_true",
                        help="无视熔断冷静期，手动排查时用")
    args = parser.parse_args()

    new_trace("bilisync")

    remaining = 0.0 if args.ignore_cooldown else cooldown_remaining()
    if remaining:
        print(f"上一轮触发了限流，还要冷静 {remaining / 60:.0f} 分钟，本次跳过。", flush=True)
        log_event("bilibili_sync_skipped", reason="cooldown", minutes=int(remaining / 60))
        return

    started = time.monotonic()
    budget = args.max_videos
    total = 0

    sources = [(int(item["uid"]), str(item.get("category", "training")))
               for item in load_sources()]
    print(f"订阅了 {len(sources)} 个来源", flush=True)

    # 配额在来源之间**平分**，而不是先到先得。
    #
    # 顺序处理加共享配额的话，排在前面的来源会把额度吃光：历史视频很多的
    # 来源会让排在后面的来源长期轮不到。
    # 对知识库来说广度比深度更有用——四个来源各有五条，
    # 比一个来源有二十条更能覆盖问题。
    share = max(budget // max(len(sources), 1), 1)
    print(f"本次配额 {budget}，每个来源分得 {share} 条", flush=True)

    for index, (uid, category) in enumerate(sources):
        if budget <= 0:
            break
        if index:
            # 只有可能真的去请求列表时才需要这个间隔。
            # 全部命中缓存时空等 8 秒 × 来源数纯属浪费。
            _, fetched_at = load_cached_list(uid)
            if args.refresh_list or time.time() - fetched_at >= LIST_TTL_HOURS * 3600:
                await asyncio.sleep(SOURCE_PAUSE_SECONDS)

        # 最后一个来源可以用掉剩余全部配额，避免整除后有零头浪费
        allotment = budget if index == len(sources) - 1 else min(share, budget)
        imported, left, tripped = await sync_source(
            uid, category, allotment, args.dry_run, args.refresh_list
        )
        # sync_source 返回的是它那份额度的余量，换算回全局
        budget -= allotment - left
        total += imported
        if tripped:
            # 限流是账号级的，换一个 UP 主继续抓只会撞得更狠
            start_cooldown(f"uid {uid} 连续 {CONSECUTIVE_ERROR_LIMIT} 次失败")
            break

    print(f"\n合计新导入 {total} 条，耗时 {(time.monotonic() - started) / 60:.1f} 分钟",
          flush=True)
    log_event("bilibili_sync_done", imported=total)
    if total:
        # 抓成功说明限流已经过去了，不用等满冷静期
        clear_cooldown()

    if total and not args.no_reindex:
        print("\n=== 重建索引 ===", flush=True)
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(ROOT_DIR / "scripts" / "ingest_books.py"),
            cwd=str(ROOT_DIR),
        )
        await process.wait()


if __name__ == "__main__":
    asyncio.run(main())
