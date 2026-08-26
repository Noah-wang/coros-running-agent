"""分块配置的唯一来源。

单独抽出来是因为踩过一次坑：父子块实验时用命令行环境变量
`CHILD_CHUNK_SIZE=700` 建了索引，但代码默认值还是 400。
服务器的索引和产生它的代码从那一刻起就对不上，而评测量的是索引本身，
发现不了这种漂移——直到有人重跑一次 ingest 才会悄悄退回劣化配置。

现在配置只在这里定义，建索引时把指纹写进 build_info.json，
体检时拿它和当前代码比对。
"""

import os
from typing import Any


CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "1200"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "180"))

# 父子块：父块投喂给模型（上下文完整），子块参与向量匹配（不被稀释）。
# 700 是实测选中的一档：400 更差（hit@1 0.80 对 0.87），
# 单层块与 700 打平但没有语料增长后的余量。
CHILD_CHUNK_SIZE = int(os.getenv("CHILD_CHUNK_SIZE", "700"))
CHILD_CHUNK_OVERLAP = int(os.getenv("CHILD_CHUNK_OVERLAP", "100"))

MIN_CHUNK_CHARS = int(os.getenv("MIN_CHUNK_CHARS", "40"))

# 铺满全书 5% 以上页面的短行按页眉页脚处理。
# 用绝对次数会误伤正文——「热身，然后进行下列训练：」这种句子本来就会重复出现。
BOILERPLATE_PAGE_RATIO = 0.05
BOILERPLATE_MIN_REPEATS = 5
BOILERPLATE_MIN_CHARS = 10
BOILERPLATE_MAX_CHARS = 120


def current_config() -> dict[str, Any]:
    """当前代码生效的分块配置。建索引时写盘，体检时比对。"""
    return {
        "chunk_size": CHUNK_SIZE,
        "chunk_overlap": CHUNK_OVERLAP,
        "child_chunk_size": CHILD_CHUNK_SIZE,
        "child_chunk_overlap": CHILD_CHUNK_OVERLAP,
        "min_chunk_chars": MIN_CHUNK_CHARS,
        "boilerplate_page_ratio": BOILERPLATE_PAGE_RATIO,
    }


def diff_config(recorded: dict[str, Any] | None) -> dict[str, str]:
    """对比索引记录的配置和当前代码，返回不一致的项。"""
    if not recorded:
        return {}
    current = current_config()
    return {
        key: f"索引={recorded.get(key)} 当前代码={value}"
        for key, value in current.items()
        if key in recorded and recorded[key] != value
    }
