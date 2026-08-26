import asyncio
import collections
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from pypdf import PdfReader
from dotenv import load_dotenv

from src.runtime.chunking import (
    BOILERPLATE_MAX_CHARS,
    BOILERPLATE_MIN_CHARS,
    BOILERPLATE_MIN_REPEATS,
    BOILERPLATE_PAGE_RATIO,
    CHILD_CHUNK_OVERLAP,
    CHILD_CHUNK_SIZE,
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    MIN_CHUNK_CHARS,
    current_config,
)
from src.runtime.embeddings import embed_texts, embedding_configured, get_embedding_model


KNOWLEDGE_DIR = ROOT_DIR / "data" / "knowledge" / "coros-report"
BOOKS_DIR = KNOWLEDGE_DIR / "books"
VIDEOS_DIR = KNOWLEDGE_DIR / "videos"

# 语料分类。检索时按它过滤，避免不同主题互相挤占名额。
#
# 现在语料是 136 块书 + 9 块视频，书占 94%。往同一个索引里持续灌入
# 跑鞋测评这类新主题，训练类问题会被逐渐挤下去——3.26 否掉混合检索时
# 就见过这个现象：排序一被扰动，book_hit 从 1.00 掉到 0.91。
#
# 分类从**目录名**推导，不额外维护映射文件：放在 videos/shoes/ 下的就是
# shoes 类，直接放在 videos/ 根下的归入默认类。目录即声明，不会对不上。
DEFAULT_CATEGORY = "training"


def category_for(path: Path, base: Path) -> str:
    """按文件相对于 books/ 或 videos/ 的子目录名决定分类。"""
    try:
        relative = path.resolve().relative_to(base.resolve())
    except ValueError:
        return DEFAULT_CATEGORY
    parts = relative.parts
    return parts[0] if len(parts) > 1 else DEFAULT_CATEGORY
CHUNKS_PATH = KNOWLEDGE_DIR / "chunks.json"
INDEX_PATH = KNOWLEDGE_DIR / "index.json"
EMBEDDINGS_PATH = KNOWLEDGE_DIR / "embeddings.json"
BUILD_INFO_PATH = KNOWLEDGE_DIR / "build_info.json"

DEFAULT_EMBEDDING_BATCH_SIZE = 20

# 切块时优先在这些字符之后断开，避免把句子拦腰截断。
# 找不到句末标点时退而求其次找逗号顿号，再找不到才硬切。
SENTENCE_ENDINGS = "。！？；!?;\n"
CLAUSE_ENDINGS = "，、,）)”"
BREAK_SEARCH_WINDOW = 400


def normalize_text(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_pdf(path: Path) -> list[dict[str, Any]]:
    reader = PdfReader(path)
    pages: list[dict[str, Any]] = []
    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        text = normalize_text(text)
        if text:
            pages.append({"page": index, "text": text})
    return pages


def extract_text_document(path: Path) -> list[dict[str, Any]]:
    text = normalize_text(path.read_text(encoding="utf-8", errors="replace"))
    if not text:
        return []
    return [{"page": 1, "text": text}]


def find_boilerplate_lines(pages: list[dict[str, Any]]) -> set[str]:
    """找出反复出现在多个页面上的行，基本都是页眉页脚水印和表注。

    必须在跨页合并之前处理。合并之后它们会混进正常块的正文里，
    到那时再按块去重就晚了。
    """
    counts = collections.Counter(
        line.strip()
        for page in pages
        for line in page["text"].splitlines()
        if BOILERPLATE_MIN_CHARS <= len(line.strip()) <= BOILERPLATE_MAX_CHARS
    )
    threshold = max(BOILERPLATE_MIN_REPEATS, int(len(pages) * BOILERPLATE_PAGE_RATIO))
    return {line for line, count in counts.items() if count >= threshold}


def strip_boilerplate(
    pages: list[dict[str, Any]],
    boilerplate: set[str],
) -> tuple[list[dict[str, Any]], int]:
    cleaned: list[dict[str, Any]] = []
    removed = 0
    for page in pages:
        kept_lines = []
        for line in page["text"].splitlines():
            if line.strip() in boilerplate:
                removed += 1
                continue
            kept_lines.append(line)
        text = normalize_text("\n".join(kept_lines))
        if text:
            cleaned.append({"page": page["page"], "text": text})
    return cleaned, removed


def build_document(pages: list[dict[str, Any]]) -> tuple[str, list[tuple[int, int]]]:
    """把一个来源的所有页拼成整篇，并记录每页起始的字符偏移。

    跨页拼接是为了让 CHUNK_OVERLAP 真正生效。原来按页单独切块时，
    跨页的句子会被硬切，而 overlap 只在页内起作用。
    """
    parts: list[str] = []
    spans: list[tuple[int, int]] = []
    offset = 0
    for page in pages:
        spans.append((offset, page["page"]))
        parts.append(page["text"])
        offset += len(page["text"]) + 1
    return "\n".join(parts), spans


def page_at(spans: list[tuple[int, int]], offset: int) -> int:
    page = spans[0][1] if spans else 1
    for start, page_number in spans:
        if start > offset:
            break
        page = page_number
    return page


def find_break(text: str, start: int, end: int) -> int:
    """在 end 附近往回找断点。

    先找句末标点；书里有大量没有句号的表格，所以再退一步找逗号顿号；
    都找不到才硬切。
    """
    if end >= len(text):
        return len(text)

    lower_bound = max(start + 1, end - BREAK_SEARCH_WINDOW)
    for candidates in (SENTENCE_ENDINGS, CLAUSE_ENDINGS):
        for index in range(end - 1, lower_bound - 1, -1):
            if text[index] in candidates:
                return index + 1
    return end


def header_field(text: str, key: str) -> str:
    """取视频 md 头部的某个字段。"""
    match = re.search(rf"^{key}:\s*(.+)$", text[:600], re.M)
    return match.group(1).strip() if match else ""


def video_context(text: str, source: str) -> str:
    """从视频 md 的头部抽出文档级前缀。

    原来这段头部只落在第 1 块里，第 2 块之后完全不知道自己是哪个视频。

    优先用标题而不是 BV 号：前缀是要参与嵌入的，而「BV1XouM6BER3」
    没有任何语义，「美津浓WaveSky9实战报告」才能让「美津浓怎么样」
    这类查询命中这条视频的每一块。
    """
    title = header_field(text, "Title")
    uploader = header_field(text, "Uploader")
    if title:
        # UP 主也放进前缀。同一个博主的用词和评测风格是连贯的，
        # 「东哥怎么评价这双鞋」这类问题需要它才能命中。
        prefix = f"B站跑步视频字幕《{title}》"
        return f"{prefix} UP主：{uploader}" if uploader else prefix
    bv = header_field(text, "Source")
    return f"B站跑步视频字幕 {bv or source}"


def chunk_document(
    source: str,
    kind: str,
    text: str,
    spans: list[tuple[int, int]],
    context: str,
    category: str = DEFAULT_CATEGORY,
    uploader: str = "",
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    start = 0
    while start < len(text):
        hard_end = min(start + CHUNK_SIZE, len(text))
        end = find_break(text, start, hard_end)
        chunk_text = text[start:end].strip()
        if chunk_text:
            page = page_at(spans, start)
            chunks.append(
                {
                    "id": f"{source}:p{page}:c{len(chunks) + 1}",
                    "source": source,
                    "kind": kind,
                    "category": category,
                    "uploader": uploader,
                    "page": page,
                    "page_end": page_at(spans, max(end - 1, start)),
                    # text 是原文，用于展示引用；context 只参与嵌入，不展示
                    "text": chunk_text,
                    "context": f"{context} 第{page}页" if kind == "book" else context,
                }
            )
        if end >= len(text):
            break
        # 重叠起点也要对齐到句子边界。直接用 end - CHUNK_OVERLAP 会退回到
        # 上一块的句子中间，导致几乎每一块都从半句话开始。
        overlap_start = max(start + 1, end - CHUNK_OVERLAP)
        start = max(start + 1, find_break(text, start, overlap_start))
    return chunks


def filter_chunks(chunks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """丢掉空块和过短块。页眉页脚已经在合并前按行去掉了。"""
    kept: list[dict[str, Any]] = []
    dropped = {"empty": 0, "too_short": 0}
    for chunk in chunks:
        text = chunk["text"].strip()
        if not text:
            dropped["empty"] += 1
            continue
        if len(text) < MIN_CHUNK_CHARS:
            dropped["too_short"] += 1
            continue
        kept.append(chunk)
    return kept, dropped


def tokenize(text: str) -> list[str]:
    lowered = text.lower()
    english = re.findall(r"[a-z0-9]+", lowered)
    chinese = []
    for sequence in re.findall(r"[\u4e00-\u9fff]+", lowered):
        for size in (2, 3, 4):
            chinese.extend(
                sequence[index : index + size]
                for index in range(0, max(len(sequence) - size + 1, 0))
            )
    return english + chinese


def embedding_text(context: str, text: str) -> str:
    """嵌入用的文本 = 文档级前缀 + 原文。

    前缀让每一块都带上来源身份，缓解块内指代缺失；原文一个字不改，
    引用展示时仍然是页面上真实存在的文字。
    """
    return f"{context}\n{text}" if context else text


def split_children(parent: dict[str, Any]) -> list[dict[str, Any]]:
    """把一个父块切成若干子块。子块只用于向量匹配，不展示给用户。"""
    text = parent["text"]
    if CHILD_CHUNK_SIZE <= 0 or len(text) <= CHILD_CHUNK_SIZE:
        return [{"id": f"{parent['id']}:s1", "parent_id": parent["id"], "text": text}]

    children: list[dict[str, Any]] = []
    start = 0
    while start < len(text):
        hard_end = min(start + CHILD_CHUNK_SIZE, len(text))
        end = find_break(text, start, hard_end)
        child_text = text[start:end].strip()
        if child_text:
            children.append(
                {
                    "id": f"{parent['id']}:s{len(children) + 1}",
                    "parent_id": parent["id"],
                    "text": child_text,
                }
            )
        if end >= len(text):
            break
        overlap_start = max(start + 1, end - CHILD_CHUNK_OVERLAP)
        start = max(start + 1, find_break(text, start, overlap_start))
    return children or [{"id": f"{parent['id']}:s1", "parent_id": parent["id"], "text": text}]


def embedding_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_cached_vectors() -> dict[str, list[float]]:
    """按内容哈希索引已有向量。

    用内容而不是块 ID 作键：块 ID 里带页码，页码会随分块策略变，
    而内容没变就没必要重算。换了 embedding 模型则整份缓存作废。
    """
    if not EMBEDDINGS_PATH.exists():
        return {}
    try:
        payload = json.loads(EMBEDDINGS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if payload.get("model") != get_embedding_model():
        return {}
    return {
        item["hash"]: item["embedding"]
        for item in payload.get("items", [])
        if item.get("hash") and item.get("embedding")
    }


async def build_embedding_payload(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """只对内容变化的子块调用 embedding API，返回待写盘的 payload。

    这里不直接写盘：四个索引文件要一起原子生效，写盘统一交给 main() 处理。

    切块本身是纯本地计算，每次全量重来无所谓，还顺带保证了删除传播：
    源文件没了就不会出现在 children 里，它的向量自然不会被写出去。
    真正花钱的是嵌入，所以增量只做在这一层。
    """
    contexts = {chunk["id"]: chunk.get("context", "") for chunk in chunks}
    children = [child for chunk in chunks for child in split_children(chunk)]
    for child in children:
        child["embed_text"] = embedding_text(contexts[child["parent_id"]], child["text"])
        child["hash"] = embedding_hash(child["embed_text"])

    cached = load_cached_vectors()
    vectors: dict[str, list[float]] = {}
    pending_hashes: list[str] = []
    pending_texts: list[str] = []
    queued: set[str] = set()

    for child in children:
        digest = child["hash"]
        if digest in vectors or digest in queued:
            continue
        if digest in cached:
            vectors[digest] = cached[digest]
        else:
            pending_hashes.append(digest)
            pending_texts.append(child["embed_text"])
            queued.add(digest)

    reused = sum(1 for child in children if child["hash"] in cached)
    print(f"Reusing {reused}/{len(children)} child vectors from cache")

    batch_size = int(os.getenv("EMBEDDING_BATCH_SIZE", DEFAULT_EMBEDDING_BATCH_SIZE))
    done = 0
    for start in range(0, len(pending_hashes), batch_size):
        batch_hashes = pending_hashes[start : start + batch_size]
        batch_texts = pending_texts[start : start + batch_size]
        computed = await embed_texts(batch_texts)
        for digest, vector in zip(batch_hashes, computed, strict=True):
            vectors[digest] = vector
        done += len(batch_hashes)
        print(f"Embedded {done}/{len(pending_hashes)} new child chunks")

    if not pending_hashes:
        print("Nothing changed, no embedding API calls made")

    return {
        "model": get_embedding_model(),
        "chunk_count": len(chunks),
        "child_count": len(children),
        "items": [
            {
                "id": child["id"],
                "parent_id": child["parent_id"],
                "hash": child["hash"],
                "embedding": vectors[child["hash"]],
            }
            for child in children
        ],
    }


def stage(path: Path, text: str) -> tuple[Path, Path]:
    """把内容写进同目录的临时文件，返回 (临时文件, 目标路径)。

    此时还没有任何人能看到新内容，崩溃也只是留下一个 .tmp。
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    return tmp, path


def commit(staged: list[tuple[Path, Path]]) -> None:
    """把所有临时文件一次性改名就位。

    同一文件系统内的 rename 是原子的：读的人要么看到完整的旧文件，
    要么看到完整的新文件，不存在半截状态。
    四个文件仍然是四次 rename，理论上还有个几微秒的窗口，
    但相比"写 embeddings 要等几十秒 API"那个窗口已经不是一个量级。
    """
    for tmp, final in staged:
        tmp.replace(final)


def discard(staged: list[tuple[Path, Path]]) -> None:
    for tmp, _ in staged:
        tmp.unlink(missing_ok=True)


def build_chunks() -> tuple[list[dict[str, Any]], dict[str, int]]:
    raw: list[dict[str, Any]] = []
    boilerplate_lines = 0

    # rglob 而不是 glob：分类靠子目录表达，所以必须往下走一层
    for pdf_path in sorted(BOOKS_DIR.rglob("*.pdf")):
        pages = extract_pdf(pdf_path)
        if not pages:
            continue
        pages, removed = strip_boilerplate(pages, find_boilerplate_lines(pages))
        boilerplate_lines += removed
        text, spans = build_document(pages)
        raw.extend(
            chunk_document(
                pdf_path.name,
                "book",
                text,
                spans,
                f"《{pdf_path.stem}》",
                category_for(pdf_path, BOOKS_DIR),
            )
        )

    for text_path in sorted([*VIDEOS_DIR.rglob("*.md"), *VIDEOS_DIR.rglob("*.txt")]):
        pages = extract_text_document(text_path)
        if not pages:
            continue
        text, spans = build_document(pages)
        raw.extend(
            chunk_document(
                text_path.name,
                "video",
                text,
                spans,
                video_context(text, text_path.name),
                category_for(text_path, VIDEOS_DIR),
                header_field(text, "Uploader"),
            )
        )

    kept, dropped = filter_chunks(raw)
    dropped["boilerplate_lines"] = boilerplate_lines
    return kept, dropped


def main() -> None:
    load_dotenv(ROOT_DIR / ".env")
    BOOKS_DIR.mkdir(parents=True, exist_ok=True)
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)

    chunks, dropped = build_chunks()

    index = {
        "chunk_count": len(chunks),
        "sources": sorted({chunk["source"] for chunk in chunks}),
        "tokens": {chunk["id"]: tokenize(chunk["text"]) for chunk in chunks},
    }

    build_info = {
        # 把产生这份索引的配置一并写盘。没有它就没法发现
        # "索引是用 700 建的，但代码默认值是 400" 这种漂移。
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "chunk_count": len(chunks),
        "sources": index["sources"],
        "config": current_config(),
    }

    # 四个索引文件必须一起生效。先全部写成 .tmp，最后一次性改名就位。
    # 原来是顺序直接写，而 embeddings 要等几十秒的 API 调用才写得完，
    # 这期间 chunks.json 已经是新的、embeddings.json 还是旧的——
    # 中途崩溃（报错、Ctrl+C、断网）就会留下撕裂索引，
    # 检索会静默降级到关键词兜底，回答变差但没有任何提示。
    staged: list[tuple[Path, Path]] = []
    try:
        staged.append(
            stage(CHUNKS_PATH, json.dumps(chunks, ensure_ascii=False, indent=2) + "\n")
        )
        staged.append(
            stage(INDEX_PATH, json.dumps(index, ensure_ascii=False, indent=2) + "\n")
        )
        staged.append(
            stage(
                BUILD_INFO_PATH,
                json.dumps(build_info, ensure_ascii=False, indent=2) + "\n",
            )
        )

        print(f"Prepared {len(chunks)} chunks")
        print(f"Build config: {current_config()}")
        print(
            "Dropped: "
            f"empty={dropped['empty']} "
            f"too_short={dropped['too_short']} "
            f"boilerplate_lines={dropped['boilerplate_lines']}"
        )
        for source in index["sources"]:
            print(f"- {source}")

        if embedding_configured():
            payload = asyncio.run(build_embedding_payload(chunks))
            staged.append(
                stage(EMBEDDINGS_PATH, json.dumps(payload, ensure_ascii=False) + "\n")
            )
        else:
            print("Skipped embeddings because EMBEDDING_API_KEY is not set.")

        commit(staged)
        print(f"Committed {len(staged)} index files to {KNOWLEDGE_DIR}")
    except BaseException:
        # BaseException 而不是 Exception：Ctrl+C 是最现实的中断方式之一，
        # 它抛的 KeyboardInterrupt 不属于 Exception。
        discard(staged)
        print("Ingest failed, index left untouched")
        raise


if __name__ == "__main__":
    main()
