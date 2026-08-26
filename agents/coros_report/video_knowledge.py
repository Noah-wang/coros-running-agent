import asyncio
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from agents.coros_report.running_tools import build_ingest_registry
from src.integrations.bilibili_subtitle import fetch_bilibili_subtitle
from src.runtime.tools import run_tool_loop


INGEST_REVIEW_PROMPT = """
你是知识库质检员。刚有新资料导入跑步知识库，你要检查它切得好不好。

规则：
- 必须先调用 inspect_knowledge_index 拿到真实数据，不要凭空判断。
- 先查刚导入的这个来源，需要的话再查全部来源做对比。
- 用中文回答，不超过 150 字，不要罗列原始 JSON。
- 只说结论和值得注意的问题，没问题就直接说没问题。
- 重点关注：这份资料切出了几块、有没有空块或过短块、有没有从句子中间被切断、
  它在整个知识库里占比多少（占比太低意味着检索时很难被命中）。
""".strip()


from src.runtime.paths import ROOT_DIR  # noqa: E402
VIDEOS_DIR = ROOT_DIR / "data" / "knowledge" / "coros-report" / "videos"
INGEST_SCRIPT = ROOT_DIR / "scripts" / "ingest_books.py"


def _safe_slug(value: str) -> str:
    match = re.search(r"BV[a-zA-Z0-9]+", value)
    if match is not None:
        return match.group(0)

    slug = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", value).strip("-")
    return slug[:60] or "running-video"


def _source_path(video_input: str) -> Path:
    slug = _safe_slug(video_input)
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return VIDEOS_DIR / f"{timestamp}-{slug}.md"


def _document_text(video_input: str, subtitle: str) -> str:
    imported_at = datetime.now(UTC).isoformat(timespec="seconds")
    return f"""# Running Video Knowledge

Source: {video_input}
Imported at: {imported_at}
Content type: Bilibili subtitle transcript

## Transcript

{subtitle.strip()}
"""


async def _rebuild_knowledge_index() -> str:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(INGEST_SCRIPT),
        cwd=str(ROOT_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    output = stdout.decode("utf-8", errors="replace").strip()
    error = stderr.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        raise RuntimeError(error or output or "Knowledge ingest failed.")
    return output


async def import_running_video_knowledge(video_input: str) -> str:
    if not video_input.strip():
        return "请提供 B站 BV号或视频链接。"

    subtitle = await fetch_bilibili_subtitle(video_input.strip())
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    path = _source_path(video_input.strip())
    path.write_text(_document_text(video_input.strip(), subtitle), encoding="utf-8")

    ingest_output = await _rebuild_knowledge_index()
    review = await _review_ingest(path.name)
    return (
        "已把这个跑步视频导入知识库。\n"
        f"- 保存位置：{path}\n"
        f"- 字幕长度：{len(subtitle)} 字符\n"
        f"- 重建结果：\n{ingest_output}\n"
        f"\n## 质检\n{review}"
    )


async def _review_ingest(source_name: str) -> str:
    """导入后让 Agent 调用质检工具，把统计数据翻译成人能看懂的结论。

    质检本身是确定性的，但要不要提醒、提醒什么，交给模型判断。
    """
    try:
        return await run_tool_loop(
            INGEST_REVIEW_PROMPT,
            f"刚导入的来源文件名是 {source_name}，检查一下它切得怎么样。",
            build_ingest_registry(),
            max_rounds=3,
            log=lambda text: print(f"ingest {text}", flush=True),
        )
    except Exception as exc:
        print(f"ingest review failed: {exc}", flush=True)
        return f"（质检失败：{exc}）"
