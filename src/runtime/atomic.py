"""原子写入：先写临时文件，最后一起改名就位。

`write_text` 改的是内容——清空原内容块再逐字节写入，整个过程都是中间态。
`rename` 改的是目录里那条「名字 → 内容块」的指针，是不可分割的。
所以把慢的部分挪到一个没人在看的临时名字上，只留一次指针切换暴露给读者。

同一文件系统内才有这个保证，所以临时文件必须放在目标文件的同目录。

注意这只保证 atomicity 不保证 durability：没有 fsync，整机断电理论上仍可能丢。
进程崩溃、Ctrl+C、报错都能防住，这是实际会遇到的失败模式。
"""

import json
from pathlib import Path
from typing import Any


def stage(path: Path, text: str) -> tuple[Path, Path]:
    """把内容写进同目录的临时文件，返回 (临时文件, 目标路径)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    return tmp, path


def commit(staged: list[tuple[Path, Path]]) -> None:
    """把所有临时文件改名就位。"""
    for tmp, final in staged:
        tmp.replace(final)


def discard(staged: list[tuple[Path, Path]]) -> None:
    """清理临时文件。尽力而为——被 kill -9 时跑不到这里，
    但残留的 .tmp 用的是固定名字，下次会被覆盖，不会累积。"""
    for tmp, _ in staged:
        tmp.unlink(missing_ok=True)


def write_json_batch(items: list[tuple[Path, Any]]) -> None:
    """把多个 JSON 文件作为一批原子写入。

    任何一个写失败，全部回退，磁盘上仍是完整的旧版本。
    """
    staged: list[tuple[Path, Path]] = []
    try:
        for path, value in items:
            staged.append(stage(path, json.dumps(value, ensure_ascii=False, indent=2)))
        commit(staged)
    except BaseException:
        # BaseException 而不是 Exception：Ctrl+C 抛的 KeyboardInterrupt
        # 不属于 Exception，而它是最现实的中断方式之一。
        discard(staged)
        raise
