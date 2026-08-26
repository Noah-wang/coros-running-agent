"""知识库分块质量体检。

不评价内容好坏，只看形状：空块、过短块、被硬截断的块、从句中切开的块、
重复块、来源占比、embedding 是否对齐。

既给 scripts/inspect_chunks.py 当后端，也注册成工具供 Agent 调用。
"""

import collections
import json
import statistics
from pathlib import Path
from typing import Any

from src.runtime.chunking import current_config, diff_config


ROOT_DIR = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIR = ROOT_DIR / "data" / "knowledge" / "coros-report"
CHUNKS_PATH = KNOWLEDGE_DIR / "chunks.json"
EMBEDDINGS_PATH = KNOWLEDGE_DIR / "embeddings.json"
BUILD_INFO_PATH = KNOWLEDGE_DIR / "build_info.json"

SHORT_CHUNK_CHARS = 100
# 只列真正不可能作句首的助词和闭合符号。
# 「在」「其」「但」「是」这些本来就能开头，放进来会大量误判。
MID_SENTENCE_HEADS = "的了着地得，、）】》」"


def _percentile(values: list[int], ratio: float) -> int:
    ordered = sorted(values)
    return ordered[min(int(len(ordered) * ratio), len(ordered) - 1)]


def inspect_knowledge_index(source: str | None = None) -> dict[str, Any]:
    """体检知识库索引。传 source 则只看该来源（支持部分匹配）。"""
    if not CHUNKS_PATH.exists():
        return {"error": "知识库索引不存在，需要先运行一次导入。"}

    all_chunks = json.loads(CHUNKS_PATH.read_text(encoding="utf-8"))
    if not all_chunks:
        return {"error": "知识库索引是空的。"}

    chunks = all_chunks
    if source:
        chunks = [c for c in all_chunks if source in c.get("source", "")]
        if not chunks:
            return {
                "error": f"索引里没有匹配 {source} 的来源",
                "已有来源": sorted({c.get("source", "") for c in all_chunks}),
            }

    lengths = [len(c["text"]) for c in chunks]
    empty = [c for c in chunks if not c["text"].strip()]
    short = [c for c in chunks if 0 < len(c["text"]) < SHORT_CHUNK_CHARS]
    mid_sentence = [
        c for c in chunks if c["text"] and c["text"].lstrip()[:1] in MID_SENTENCE_HEADS
    ]
    duplicate_counts = collections.Counter(c["text"].strip() for c in chunks if c["text"].strip())
    duplicates = {text[:40]: count for text, count in duplicate_counts.most_common(3) if count > 1}

    report: dict[str, Any] = {
        "检查范围": source or "全部来源",
        "块数": len(chunks),
        "来源分布": {
            name: count
            for name, count in collections.Counter(c["source"] for c in all_chunks).most_common()
        },
        "长度": {
            "最短": min(lengths),
            "p25": _percentile(lengths, 0.25),
            "中位": int(statistics.median(lengths)),
            "最长": max(lengths),
        },
        "问题": {
            "空块": len(empty),
            f"短于{SHORT_CHUNK_CHARS}字符": len(short),
            "从句子中间开头": len(mid_sentence),
            "内容完全重复": duplicates,
        },
    }

    if BUILD_INFO_PATH.exists():
        build_info = json.loads(BUILD_INFO_PATH.read_text(encoding="utf-8"))
        report["索引构建"] = {"时间": build_info.get("built_at")}
        drift = diff_config(build_info.get("config"))
        if drift:
            # 索引是用一套配置建的，代码现在是另一套。
            # 下次重建索引就会悄悄换掉分块策略，这里必须报出来。
            report["索引构建"]["⚠ 配置漂移"] = drift
        else:
            report["索引构建"]["配置"] = "与当前代码一致"
    else:
        report["索引构建"] = {"状态": "没有 build_info.json，无法确认索引是用什么配置建的"}
        report["索引构建"]["当前代码配置"] = current_config()

    if EMBEDDINGS_PATH.exists():
        payload = json.loads(EMBEDDINGS_PATH.read_text(encoding="utf-8"))
        vector_ids = {item["id"] for item in payload.get("items", [])}
        missing = [c["id"] for c in chunks if c["id"] not in vector_ids]
        report["向量"] = {
            "模型": payload.get("model"),
            "总数": len(payload.get("items", [])),
            "缺向量的块": len(missing),
        }
    else:
        report["向量"] = {"状态": "没有 embeddings.json，只能走关键词检索"}

    samples = sorted(chunks, key=lambda c: len(c["text"]))[:2]
    report["最短的块样例"] = [c["text"][:60] for c in samples]
    if mid_sentence:
        report["被切断的块样例"] = [c["text"][:60] for c in mid_sentence[:2]]

    return report


def format_report(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)
