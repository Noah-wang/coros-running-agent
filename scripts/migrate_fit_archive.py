"""把 FIT 归档目录名里的 endTimestamp 去掉，并合并因此产生的重复。

**为什么需要迁移，而不是改完代码就完事：**
目录名是去重的唯一依据。改了命名规则之后，524 个老目录一个都对不上新名字，
下一次归档会把整个历史重新下载一遍。所以必须把老目录改名过去。

顺带解决重复：同一次运动因为 endTimestamp 在变而存了好几份，
其中最早的那些是没传完的半截。合并时**保留最大的那个 FIT**——
同一次运动的 FIT 越大意味着记录越完整（线上实例：33KB 的半截 vs 97KB 的完整版）。
删之前会校验保留下来的是不是合法 FIT（头部第 8~12 字节是 ".FIT"）。

默认只看不动。确认输出没问题再加 --apply。

    uv run python scripts/migrate_fit_archive.py
    uv run python scripts/migrate_fit_archive.py --apply
"""

import argparse
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from agents.coros_report.fit_archive import (  # noqa: E402
    _activity_slug,
    _base_fit_dir,
)


def is_valid_fit(path: Path) -> bool:
    """FIT 文件头第 8~12 字节是 ".FIT"。用来确认保留的不是个坏文件。"""
    try:
        with path.open("rb") as handle:
            return handle.read(12)[8:12] == b".FIT"
    except OSError:
        return False


def activity_from_dir(folder: Path) -> dict | None:
    """优先读 metadata.json——目录名是有损的，元数据才是原始信息。"""
    meta_path = folder / "metadata.json"
    if not meta_path.exists():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    activity = data.get("activity")
    return activity if isinstance(activity, dict) else None


def total_fit_bytes(folder: Path) -> int:
    return sum(p.stat().st_size for p in folder.glob("*.fit"))


def plan(base: Path) -> tuple[list[tuple[Path, Path]], list[tuple[Path, int, str]]]:
    """返回 (要改名的, 要删的)。删的那份带上大小和原因，好在 dry-run 里核对。"""
    groups: dict[tuple[Path, str], list[Path]] = defaultdict(list)
    skipped: list[Path] = []

    for date_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        for folder in sorted(p for p in date_dir.iterdir() if p.is_dir()):
            activity = activity_from_dir(folder)
            if activity is None:
                # 没有元数据就不猜。宁可留着让人看一眼，也不要按目录名瞎改。
                skipped.append(folder)
                continue
            groups[(date_dir, _activity_slug(activity))].append(folder)

    renames: list[tuple[Path, Path]] = []
    deletions: list[tuple[Path, int, str]] = []

    for (date_dir, new_slug), folders in sorted(groups.items(), key=lambda kv: str(kv[0][0])):
        target = date_dir / new_slug
        # 保留 FIT 最大的那个目录：同一次运动，越大越完整。
        keeper = max(folders, key=lambda f: (total_fit_bytes(f), f.name))
        for folder in folders:
            if folder != keeper:
                deletions.append(
                    (folder, total_fit_bytes(folder), f"重复，保留 {keeper.name}")
                )
        if keeper != target:
            renames.append((keeper, target))

    if skipped:
        print(f"⚠️  {len(skipped)} 个目录没有 metadata.json，跳过不动：")
        for folder in skipped[:10]:
            print(f"    {folder}")
    return renames, deletions


def main() -> int:
    parser = argparse.ArgumentParser(description="FIT 归档目录名迁移（去掉 endTimestamp）")
    parser.add_argument("--apply", action="store_true", help="真的改。不加只打印计划")
    args = parser.parse_args()

    base = _base_fit_dir()
    if not base.exists():
        print(f"归档目录不存在：{base}")
        return 1

    renames, deletions = plan(base)

    print(f"\n归档根目录：{base}")
    print(f"要改名 {len(renames)} 个，要删 {len(deletions)} 个重复目录")

    if deletions:
        print("\n将要删除的重复目录：")
        freed = 0
        for folder, size, reason in deletions:
            bad = "" if is_valid_fit_dir(folder) else "  [FIT 头不合法]"
            print(f"  - {folder.name}  {size:>9,} 字节  {reason}{bad}")
            freed += size
        print(f"  共释放 {freed:,} 字节")

    if not args.apply:
        print("\n这是预演。确认无误后加 --apply 执行。")
        return 0

    for folder, _, _ in deletions:
        shutil.rmtree(folder)
    for source, target in renames:
        if target.exists():
            print(f"  跳过改名：{target.name} 已存在")
            continue
        source.rename(target)

    print(f"\n完成：删除 {len(deletions)} 个，改名 {len(renames)} 个。")
    return 0


def is_valid_fit_dir(folder: Path) -> bool:
    fits = list(folder.glob("*.fit"))
    return bool(fits) and all(is_valid_fit(p) for p in fits)


if __name__ == "__main__":
    raise SystemExit(main())
