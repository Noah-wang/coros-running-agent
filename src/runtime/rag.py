import json
import math
import os
import re
from pathlib import Path
from typing import Any

from src.runtime.embeddings import embed_texts, embedding_configured, get_embedding_model

try:  # numpy 是软依赖，没装就退回纯 Python 逐条计算
    import numpy as np
except ImportError:  # pragma: no cover
    np = None


# 检索返回条数。评测必须用同一个值，否则会测出一个生产并不使用的配置。
# 实测：22 个用例的命中块全部落在前 3 名，hit@3 与 hit@5 同为 1.00，
# 第 4、5 名从未被用到却要占掉约 40% 的检索上下文，而且 tool loop 每轮都会重发。
DEFAULT_TOP_K = 3

# BM25 参数。k1 控制词频饱和速度，b 控制长度归一化强度，取业界常用值。
BM25_K1 = float(os.getenv("BM25_K1", "1.2"))
BM25_B = float(os.getenv("BM25_B", "0.75"))
# 短语加权的强度，单位是「一个最强查询词的 IDF」。设 0 即关闭。
PHRASE_BOOST_WEIGHT = float(os.getenv("KEYWORD_PHRASE_BOOST", "1.0"))

ROOT_DIR = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIR = ROOT_DIR / "data" / "knowledge" / "coros-report"
RAG_DIR = KNOWLEDGE_DIR
CHUNKS_PATH = KNOWLEDGE_DIR / "chunks.json"
INDEX_PATH = KNOWLEDGE_DIR / "index.json"
EMBEDDINGS_PATH = KNOWLEDGE_DIR / "embeddings.json"


def _tokenize(text: str) -> list[str]:
    lowered = text.lower()
    english = re.findall(r"[a-z0-9]+", lowered)
    chinese = []
    for sequence in re.findall(r"[\u4e00-\u9fff]+", lowered):
        for size in (2, 3, 4):
            chinese.extend(
                sequence[index : index + size]
                for index in range(0, max(len(sequence) - size + 1, 0))
            )

    synonyms = {
        "阈值": ["threshold", "tempo", "lactate", "乳酸", "门槛"],
        "阈值跑": ["threshold", "tempo", "lactate", "乳酸", "门槛", "门槛跑"],
        "节奏": ["tempo", "threshold"],
        "节奏跑": ["tempo", "threshold"],
        "轻松": ["easy", "recovery"],
        "轻松跑": ["easy", "recovery"],
        "恢复": ["recovery", "easy"],
        "间歇": ["interval", "repetition"],
        "配速": ["pace"],
        "心率": ["heart", "rate"],
        "马拉松": ["marathon"],
    }
    expanded = []
    for term, related_tokens in synonyms.items():
        if term in lowered:
            expanded.extend(related_tokens)

    return english + chinese + expanded


# 索引文件按 (路径, mtime) 缓存在进程内。
# 之前每次检索都要重新解析 embeddings.json，光这一步就占掉大部分耗时，
# 而文件只在重建索引时才变。mtime 作为键，重建后会自动失效。
_file_cache: dict[str, tuple[float, Any]] = {}


def _load_json_cached(path: Path, empty: Any) -> Any:
    if not path.exists():
        return empty

    mtime = path.stat().st_mtime
    cached = _file_cache.get(str(path))
    if cached is not None and cached[0] == mtime:
        return cached[1]

    value = json.loads(path.read_text(encoding="utf-8"))
    _file_cache[str(path)] = (mtime, value)
    return value


def _load_chunks() -> list[dict[str, Any]]:
    return _load_json_cached(CHUNKS_PATH, [])


def _load_index() -> dict[str, Any]:
    return _load_json_cached(INDEX_PATH, {})


def _load_embeddings() -> dict[str, Any]:
    return _load_json_cached(EMBEDDINGS_PATH, {})


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for left_value, right_value in zip(left, right, strict=False):
        dot += left_value * right_value
        left_norm += left_value * left_value
        right_norm += right_value * right_value
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (math.sqrt(left_norm) * math.sqrt(right_norm))


def _phrase_boost(query: str, chunk_text: str) -> float:
    boost = 0.0
    phrase_groups = [
        (["阈值", "阈值跑"], ["乳酸门槛", "门槛跑", "t（乳酸门槛）"]),
        (["节奏", "节奏跑"], ["节奏跑", "t配速", "乳酸门槛"]),
        (["轻松", "轻松跑"], ["轻松跑", "e代表轻松跑", "e跑"]),
        (["间歇", "间歇训练"], ["间歇训练", "i（间歇）", "i训练"]),
    ]
    lowered_query = query.lower()
    lowered_chunk = chunk_text.lower()
    for query_terms, chunk_terms in phrase_groups:
        if any(term in lowered_query for term in query_terms) and any(
            term in lowered_chunk for term in chunk_terms
        ):
            boost += 100.0
    return boost


_keyword_cache: dict[str, tuple[float, Any]] = {}


def _keyword_index(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """预计算 BM25 需要的统计量，按 index.json 的 mtime 缓存。

    原来每次检索都要遍历三十多万个 token 重建这些表，单次要两百多毫秒。
    它们完全由索引文件决定，只在重建索引后才需要重算。
    """
    mtime = INDEX_PATH.stat().st_mtime if INDEX_PATH.exists() else 0.0
    cached = _keyword_cache.get("keyword")
    if cached is not None and cached[0] == mtime:
        return cached[1]

    token_index = _load_index().get("tokens", {})
    counts_by_chunk: dict[str, dict[str, int]] = {}
    lengths: dict[str, int] = {}
    document_frequency: dict[str, int] = {}

    for chunk in chunks:
        tokens = token_index.get(chunk["id"]) or _tokenize(chunk["text"])
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        counts_by_chunk[chunk["id"]] = counts
        lengths[chunk["id"]] = len(tokens)
        for token in counts:
            document_frequency[token] = document_frequency.get(token, 0) + 1

    average_length = (sum(lengths.values()) / len(lengths)) if lengths else 1.0
    value = {
        "counts": counts_by_chunk,
        "document_frequency": document_frequency,
        "lengths": lengths,
        "average_length": max(average_length, 1.0),
    }
    _keyword_cache["keyword"] = (mtime, value)
    return value


def _bm25_idf(document_frequency: int, total_chunks: int) -> float:
    """BM25 的 IDF，用 Lucene 的变体保证非负。

    普通 TF-IDF 的 idf 在词出现在超过一半文档时会变成负数，
    对中文 2/3/4 字滑窗切出来的高频碎片尤其容易触发。
    """
    return math.log(
        1 + (total_chunks - document_frequency + 0.5) / (document_frequency + 0.5)
    )


def filter_by_category(
    chunks: list[dict[str, Any]], category: str | None
) -> list[dict[str, Any]]:
    """按分类收窄候选。category 为空表示不过滤，全库查。

    过滤放在打分**之前**：不同主题不该互相挤占名额。语料里书占 94%，
    再灌入跑鞋测评这类新主题之后，训练类问题会被逐渐挤下去。

    分类不认识时返回全部而不是空——**宁可查得宽，也不能因为模型写错一个词
    就变成查不到**。查得宽最多是结果不够准，返回空则是功能直接失效。
    """
    if not category:
        return chunks
    matched = [c for c in chunks if c.get("category") == category]
    return matched or chunks


def known_categories() -> list[str]:
    return sorted({str(c.get("category", "")) for c in _load_chunks() if c.get("category")})


def search_knowledge_keyword(
    query: str, limit: int = 5, category: str | None = None
) -> list[dict[str, Any]]:
    chunks = filter_by_category(_load_chunks(), category)
    query_tokens = set(_tokenize(query))
    if not chunks or not query_tokens:
        return []

    keyword_index = _keyword_index(chunks)
    document_frequency = keyword_index["document_frequency"]
    lengths = keyword_index["lengths"]
    average_length = keyword_index["average_length"]
    scored: list[tuple[float, dict[str, Any]]] = []
    total_chunks = max(len(chunks), 1)

    for chunk in chunks:
        token_counts = keyword_index["counts"].get(chunk["id"])
        if not token_counts:
            continue

        # BM25 相对 TF-IDF 多两件事：
        # 词频饱和（出现 20 次不等于出现 1 次的 20 倍）
        # 长度归一化（长文档天然包含更多的每个词，要除掉这个优势）
        length_norm = 1 - BM25_B + BM25_B * lengths.get(chunk["id"], 0) / average_length
        score = 0.0
        max_idf = 0.0
        for token in query_tokens:
            count = token_counts.get(token, 0)
            if count == 0:
                continue
            idf = _bm25_idf(document_frequency.get(token, 1), total_chunks)
            max_idf = max(max_idf, idf)
            score += idf * (count * (BM25_K1 + 1)) / (count + BM25_K1 * length_norm)

        # 短语加权表达成「相当于多命中一个最强查询词」，而不是一个写死的大数。
        # 原来固定 +100，而 TF-IDF 中位分只有 10.7，等于排序完全由这张手写表决定，
        # 退化成规则匹配。现在它随语料规模自动缩放，也不会淹没 BM25 本身。
        if _phrase_boost(query, chunk["text"]) > 0:
            score += PHRASE_BOOST_WEIGHT * max_idf

        if score > 0:
            scored.append((score, chunk))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [chunk for _, chunk in scored[:limit]]


_index_cache: dict[str, tuple[float, Any]] = {}


def _build_vector_index(items: list[dict[str, Any]], chunk_ids: set[str]) -> Any:
    """把子块向量整理成可直接做矩阵运算的形式，并按 mtime 缓存。

    向量在这里就归一化好，检索时的余弦相似度退化成一次点积，
    省掉每次查询都重复算模长。
    """
    parents: list[str] = []
    rows: list[list[float]] = []
    for item in items:
        # 兼容旧格式：没有 parent_id 时，id 本身就是父块 id。
        parent_id = item.get("parent_id") or item.get("id")
        if not item.get("embedding") or parent_id not in chunk_ids:
            continue
        parents.append(parent_id)
        rows.append(item["embedding"])

    unique_parents = list(dict.fromkeys(parents))
    parent_position = {parent_id: i for i, parent_id in enumerate(unique_parents)}
    owner = [parent_position[parent_id] for parent_id in parents]

    if np is not None:
        matrix = np.asarray(rows, dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        matrix = matrix / norms
        owner_array = np.asarray(owner, dtype=np.int64)
    else:
        matrix = rows
        owner_array = owner

    return {"parents": unique_parents, "owner": owner_array, "matrix": matrix}


def _vector_index(items: list[dict[str, Any]], chunk_ids: set[str]) -> Any:
    mtime = EMBEDDINGS_PATH.stat().st_mtime if EMBEDDINGS_PATH.exists() else 0.0
    cached = _index_cache.get("vectors")
    if cached is not None and cached[0] == mtime:
        return cached[1]

    index = _build_vector_index(items, chunk_ids)
    _index_cache["vectors"] = (mtime, index)
    return index


def _rank_parents(index: Any, query_vector: list[float], limit: int) -> list[str]:
    """按父块取所属子块的最高分，返回前 limit 个父块 id。"""
    parents = index["parents"]

    if np is not None:
        query = np.asarray(query_vector, dtype=np.float32)
        norm = float(np.linalg.norm(query)) or 1.0
        scores = index["matrix"] @ (query / norm)
        best = np.full(len(parents), -np.inf, dtype=np.float32)
        np.maximum.at(best, index["owner"], scores)
        order = np.argsort(-best)[:limit]
        return [parents[i] for i in order]

    best_scores = [float("-inf")] * len(parents)
    for position, vector in zip(index["owner"], index["matrix"], strict=True):
        score = _cosine_similarity(query_vector, vector)
        if score > best_scores[position]:
            best_scores[position] = score
    ranked = sorted(range(len(parents)), key=lambda i: best_scores[i], reverse=True)
    return [parents[i] for i in ranked[:limit]]


# 混合检索：向量和关键词各自出一份候选，再按排名融合。
#
# 默认关闭。实测在当前语料上融合只会变差：纯向量已经 hit@5 = 1.00，
# 没有提升空间，关键词的排名反而会把正确结果挤出前列。
# 向量权重从 1 加到 8（关键词几乎不起作用）时指标才逐步回到纯向量水平，
# 说明关键词的每一分贡献都是负的，不是权重没调好。
#
# 代码和开关保留：语料规模变大、向量召回不再饱和之后，
# 用 RAG_HYBRID_ENABLED=true 打开重跑评测就能重新判断。
RRF_K = 60
FUSION_CANDIDATES = 20


def _hybrid_enabled() -> bool:
    return os.getenv("RAG_HYBRID_ENABLED", "false").lower() in {"1", "true", "yes", "on"}


def _hybrid_vector_weight() -> float:
    try:
        return float(os.getenv("RAG_VECTOR_WEIGHT", "1"))
    except ValueError:
        return 1.0


def _reciprocal_rank_fusion(
    ranked_lists: list[tuple[list[str], float]],
    limit: int,
) -> list[str]:
    """加权 RRF 融合：只看排名不看分数。

    向量的余弦相似度和关键词的 TF-IDF 分数量纲完全不同，
    直接加权求和需要先做归一化，而归一化系数又要调参。
    RRF 只用名次，天然免疫这个问题，也不受 _phrase_boost 那种硬加分影响。
    权重用来体现两个召回器可信度不同。
    """
    scores: dict[str, float] = {}
    for ranked, weight in ranked_lists:
        for rank, item_id in enumerate(ranked, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + weight / (RRF_K + rank)
    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [item_id for item_id, _ in ordered[:limit]]


async def search_knowledge(
    query: str, limit: int = 5, category: str | None = None
) -> list[dict[str, Any]]:
    """父子块 + 混合检索。

    子块小、向量不被稀释，负责精确匹配；父块大、上下文完整，负责投喂给模型。
    多个子块可能命中同一个父块，按父块取最高分归并，避免一个父块占掉多个名额。
    向量结果再和关键词结果做 RRF 融合。
    """
    chunks = _load_chunks()
    embeddings = _load_embeddings()
    items = embeddings.get("items", [])
    if not chunks or not items or not embedding_configured():
        return search_knowledge_keyword(query, limit, category)
    if embeddings.get("model") != get_embedding_model():
        return search_knowledge_keyword(query, limit, category)

    chunks_by_id = {chunk["id"]: chunk for chunk in chunks}
    # 索引必须建在**全部**块上。先按分类裁掉 chunks 的话，
    # 下面那条一致性检查会把它当成索引损坏，直接退回关键词检索。
    index = _vector_index(items, set(chunks_by_id))
    if len(index["parents"]) != len(chunks):
        return search_knowledge_keyword(query, limit, category)

    query_vector = (await embed_texts([query]))[0]

    def _take(parent_ids: list[str]) -> list[dict[str, Any]]:
        """按排名取结果，需要时先按分类筛。

        排序本来就是全量做的（argsort 之后才切片），所以要全排名不额外花钱。
        必须排完再筛：先筛再排会让分类外的块根本进不了打分环节，
        而「不确定属于哪类」时我们希望它仍然能被检索到。
        """
        picked = [chunks_by_id[pid] for pid in parent_ids if pid in chunks_by_id]
        return filter_by_category(picked, category)[:limit]

    if not _hybrid_enabled():
        pool = len(chunks) if category else limit
        return _take(_rank_parents(index, query_vector, pool))

    vector_ids = _rank_parents(index, query_vector, FUSION_CANDIDATES)
    keyword_ids = [
        chunk["id"] for chunk in search_knowledge_keyword(query, FUSION_CANDIDATES)
    ]
    fused = _reciprocal_rank_fusion(
        [(vector_ids, _hybrid_vector_weight()), (keyword_ids, 1.0)],
        len(chunks) if category else limit,
    )
    return _take(fused)


def format_page_label(chunk: dict[str, Any]) -> str:
    """页码标签。跨页合并之后一块可能横跨多页，这时显示区间。"""
    page = chunk.get("page", "?")
    page_end = chunk.get("page_end", page)
    if page_end != page:
        return f"p.{page}-{page_end}"
    return f"p.{page}"


def format_context(chunks: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        lines.append(
            f"[{index}] {chunk['source']} {format_page_label(chunk)}\n{chunk['text']}"
        )
    return "\n\n".join(lines)
