"""多轮会话历史。

历史落在磁盘上的 JSONL 日志里，**只追加，不改写**。内存里的会话是这份日志的
一个视图：进程重启后按日志重建，压缩只缩小视图而不删记录。

原来的实现是个纯内存 dict，两个后果：进程一重启所有正在进行的对话就断了；
压缩把老消息换成摘要之后，原文永久消失——用户问「你刚才说的那个配速是多少」
就答不上来了。

只追加带来的另一个好处是写入天然安全：崩溃最多丢掉最后一行，
前面的记录不可能被破坏，所以这里不需要 atomic.py 那套写入-改名。

会话边界（闲置超时后重新开始）不写标记，而是重放时按**相邻记录的时间间隔**
推断。这样重建出来的边界和线上实时跑出来的完全一致，不需要额外维护状态。
"""

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from src.runtime.llm import complete_text


RUNNING_COACH_TOPIC = "running-coach"
PHOTO_MEMORY_TOPIC = "photo-memory"

MAX_TURNS = 6
MAX_MESSAGE_CHARS = 1200
MAX_PENDING_QUESTIONS = 6
DEFAULT_IDLE_MINUTES = 60

# 窗口溢出时，一次折叠多少轮进摘要。批量折叠是为了避免每轮都触发一次压缩调用。
COMPRESS_BATCH_TURNS = 3
SUMMARY_MAX_CHARS = 700

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_JOURNAL_DIR = ROOT_DIR / "data" / "conversations"

SUMMARY_PROMPT = """
你在压缩一段跑步教练和用户的对话历史，供教练在后续对话中继续参考。

只输出压缩后的摘要正文，不要任何解释、标题或前后缀。

必须保留：
- 用户说过的所有具体事实：成绩、周跑量、配速、训练安排、休息日、伤病、比赛经历和当时发生了什么、目标和日期。
- 教练已经给出的结论和建议方向。
- 教练问过但用户还没有回答的问题。

要求：
- 数字、配速、日期一律原样保留，不要改写成约数或范围。
- 不要新增任何原文里没有的信息，不要推断。
- 如果同一件事前后有更新，以最新的说法为准。
- 用中文，不超过 700 字。
""".strip()

_lock = threading.Lock()
_sessions: dict[tuple[str, str], dict[str, Any]] = {}


def _idle_timeout_seconds() -> float:
    value = os.getenv("CONVERSATION_IDLE_MINUTES", str(DEFAULT_IDLE_MINUTES))
    try:
        minutes = float(value)
    except ValueError:
        minutes = DEFAULT_IDLE_MINUTES
    return max(minutes, 1.0) * 60


def _compression_enabled() -> bool:
    value = os.getenv("CONVERSATION_COMPRESSION_ENABLED", "true")
    return value.lower() not in {"0", "false", "no", "off"}


def _persist_enabled() -> bool:
    value = os.getenv("CONVERSATION_PERSIST_ENABLED", "true")
    return value.lower() not in {"0", "false", "no", "off"}


def _journal_dir() -> Path:
    value = os.getenv("CONVERSATION_DIR")
    return Path(value) if value else DEFAULT_JOURNAL_DIR


def _journal_path(key: tuple[str, str]) -> Path:
    """会话日志的路径。

    conversation_id 可能是 Discord 频道号，也可能是网页前端生成的任意字符串，
    不能直接当文件名。取一个可读的前缀加上哈希后缀：前缀方便人翻，
    哈希保证不同 id 不会因为字符被替换而撞到同一个文件。
    """
    conversation_id, topic = key
    slug = re.sub(r"[^A-Za-z0-9_-]", "_", conversation_id)[:48]
    digest = hashlib.sha1(conversation_id.encode("utf-8")).hexdigest()[:8]
    topic_slug = re.sub(r"[^A-Za-z0-9_-]", "_", topic)[:32] or "default"
    return _journal_dir() / f"{slug}-{digest}" / f"{topic_slug}.jsonl"


def _new_session(now: float) -> dict[str, Any]:
    return {
        "messages": [],
        "summary": "",
        "pending_questions": [],
        "context": {},
        "updated_at": now,
        "last_mid": 0,
    }


def _append(
    session: dict[str, Any],
    key: tuple[str, str],
    entry: dict[str, Any],
) -> None:
    """把一条记录追加到日志。必须在持锁状态下调用。

    写失败只打日志不抛：日志断了会丢历史，但不该让用户的这一轮对话失败。
    """
    entry.setdefault("ts", time.time())
    if not _persist_enabled():
        return

    path = _journal_path(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"conversation journal append failed: {exc}", flush=True)


def _replay(
    key: tuple[str, str],
    now: float,
    apply_compaction: bool = True,
) -> dict[str, Any] | None:
    """按日志重建会话状态。

    apply_compaction=False 时忽略压缩标记，重建出**完整**的对话——
    这正是「压缩只缩小视图、不删记录」这句话的兑现方式。
    """
    path = _journal_path(key)
    if not path.exists():
        return None

    timeout = _idle_timeout_seconds()
    session = _new_session(now)
    previous_ts: float | None = None

    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    # 崩溃时可能留下半行。跳过它，前面的记录仍然有效。
                    continue
                if not isinstance(entry, dict):
                    continue

                timestamp = entry.get("ts")
                timestamp = float(timestamp) if isinstance(timestamp, int | float) else 0.0

                # 相邻两条记录间隔超过闲置阈值，说明中间那段时间会话已经过期，
                # 后面的记录属于一段新对话。
                if previous_ts is not None and timestamp - previous_ts > timeout:
                    last_mid = session["last_mid"]
                    session = _new_session(now)
                    session["last_mid"] = last_mid
                previous_ts = timestamp

                _apply_entry(session, entry, apply_compaction)
    except OSError as exc:
        print(f"conversation journal read failed: {exc}", flush=True)
        return None

    if previous_ts is None:
        return None
    # 最后一条记录距今太久，这段会话已经过期，只把消息 id 计数带过来。
    if now - previous_ts > timeout:
        last_mid = session["last_mid"]
        session = _new_session(now)
        session["last_mid"] = last_mid
        return session

    session["updated_at"] = previous_ts
    return session


def _apply_entry(
    session: dict[str, Any],
    entry: dict[str, Any],
    apply_compaction: bool,
) -> None:
    kind = entry.get("type")

    if kind == "message":
        mid = entry.get("mid")
        mid = int(mid) if isinstance(mid, int) else session["last_mid"] + 1
        session["last_mid"] = max(session["last_mid"], mid)
        session["messages"].append(
            {
                "mid": mid,
                "role": str(entry.get("role", "user")),
                "content": str(entry.get("content", "")),
            }
        )
    elif kind == "compaction":
        if not apply_compaction:
            return
        session["summary"] = str(entry.get("summary", ""))
        through = entry.get("through_mid")
        through = int(through) if isinstance(through, int) else 0
        session["messages"] = [
            message for message in session["messages"] if message["mid"] > through
        ]
    elif kind == "pending":
        questions = entry.get("questions")
        session["pending_questions"] = (
            [str(item) for item in questions][:MAX_PENDING_QUESTIONS]
            if isinstance(questions, list)
            else []
        )
    elif kind == "context":
        key_name = entry.get("key")
        if isinstance(key_name, str) and key_name:
            session["context"][key_name] = entry.get("value")
    elif kind == "reset":
        last_mid = session["last_mid"]
        session.clear()
        session.update(_new_session(time.time()))
        session["last_mid"] = last_mid


def _live_session(key: tuple[str, str], now: float) -> dict[str, Any] | None:
    """内存里未过期的会话。必须在持锁状态下调用。"""
    session = _sessions.get(key)
    if session is None:
        return None
    if now - session["updated_at"] > _idle_timeout_seconds():
        del _sessions[key]
        return None
    return session


def _resolve_session(key: tuple[str, str], now: float) -> dict[str, Any] | None:
    """读会话：内存里没有就按日志重建。必须在持锁状态下调用。

    读取路径也要能重建，否则重启之后 get_history 会一直返回空——
    只在写入时重建就等于没有持久化。
    """
    session = _live_session(key, now)
    if session is not None:
        return session

    session = _replay(key, now)
    if session is not None:
        _sessions[key] = session
    return session


def _touch_session(key: tuple[str, str], now: float) -> dict[str, Any]:
    """读会话，不存在就新建。必须在持锁状态下调用。"""
    session = _resolve_session(key, now)
    if session is None:
        session = _new_session(now)
        _sessions[key] = session
    session["updated_at"] = now
    return session


def _truncate(text: str) -> str:
    cleaned = text.strip()
    if len(cleaned) <= MAX_MESSAGE_CHARS:
        return cleaned
    return f"{cleaned[:MAX_MESSAGE_CHARS].rstrip()}..."


def _visible(messages: list[dict[str, Any]]) -> tuple[dict[str, str], ...]:
    """去掉内部的 mid，只留模型需要的 role 和 content。"""
    return tuple(
        {"role": message["role"], "content": message["content"]} for message in messages
    )


def get_history(conversation_id: str | None, topic: str) -> tuple[dict[str, str], ...]:
    if not conversation_id:
        return ()

    with _lock:
        session = _resolve_session((conversation_id, topic), time.time())
        if session is None:
            return ()
        return _visible(session["messages"])


def read_full_history(
    conversation_id: str | None,
    topic: str,
) -> tuple[dict[str, str], ...]:
    """当前这段会话的完整历史，包含已经被压缩进摘要的部分。

    内存窗口是日志的视图，压缩只是缩小视图。原文一直在磁盘上，
    需要时可以完整取回。
    """
    if not conversation_id:
        return ()

    with _lock:
        session = _replay((conversation_id, topic), time.time(), apply_compaction=False)
        if session is None:
            return ()
        return _visible(session["messages"])


def get_summary(conversation_id: str | None, topic: str) -> str:
    """获取更早之前对话被压缩成的摘要，没有则返回空字符串。"""
    if not conversation_id:
        return ""

    with _lock:
        session = _resolve_session((conversation_id, topic), time.time())
        if session is None:
            return ""
        return session["summary"]


def _format_turns(messages: list[dict[str, Any]]) -> str:
    lines = []
    for message in messages:
        speaker = "用户" if message["role"] == "user" else "教练"
        lines.append(f"{speaker}：{message['content']}")
    return "\n\n".join(lines)


async def _compress(previous_summary: str, messages: list[dict[str, Any]]) -> str:
    existing = f"已有摘要：\n{previous_summary}\n\n" if previous_summary else ""
    summary = await complete_text(
        SUMMARY_PROMPT,
        f"{existing}需要并入摘要的对话：\n{_format_turns(messages)}",
    )
    cleaned = summary.strip()
    if len(cleaned) > SUMMARY_MAX_CHARS:
        cleaned = f"{cleaned[:SUMMARY_MAX_CHARS].rstrip()}..."
    return cleaned


def _record_message(
    session: dict[str, Any],
    key: tuple[str, str],
    role: str,
    content: str,
) -> None:
    """必须在持锁状态下调用。"""
    session["last_mid"] += 1
    mid = session["last_mid"]
    session["messages"].append({"mid": mid, "role": role, "content": content})
    _append(session, key, {"type": "message", "mid": mid, "role": role, "content": content})


async def append_turn(
    conversation_id: str | None,
    topic: str,
    user_text: str,
    assistant_text: str,
) -> None:
    """写入一轮对话。窗口溢出时把最老的几轮压缩成摘要。

    压缩只把消息移出内存窗口，日志里那几行原样保留——
    再配上一条压缩标记，说明「到这条为止已经被这段摘要覆盖」。
    """
    if not conversation_id or not user_text.strip() or not assistant_text.strip():
        return

    key = (conversation_id, topic)
    with _lock:
        session = _touch_session(key, time.time())
        _record_message(session, key, "user", _truncate(user_text))
        _record_message(session, key, "assistant", _truncate(assistant_text))

        messages = session["messages"]
        if len(messages) <= MAX_TURNS * 2:
            return

        # 取出最老的一批，先从窗口里摘掉再去压缩，避免持锁跨越网络调用。
        batch_size = min(COMPRESS_BATCH_TURNS * 2, len(messages) - 2)
        folding = messages[:batch_size]
        through_mid = folding[-1]["mid"]
        del messages[:batch_size]
        previous_summary = session["summary"]

    if not _compression_enabled():
        return

    try:
        summary = await _compress(previous_summary, folding)
    except Exception as exc:
        # 压缩失败时不写标记。这批对话暂时离开了内存窗口，但日志里还在，
        # 下次重建会把它们带回来——比原来直接丢弃安全。
        print(f"conversation compression failed: {exc}", flush=True)
        return

    with _lock:
        session = _sessions.get(key)
        if session is not None:
            session["summary"] = summary
            _append(
                session,
                key,
                {"type": "compaction", "through_mid": through_mid, "summary": summary},
            )


def last_user_message(conversation_id: str | None, topic: str) -> str:
    for message in reversed(get_history(conversation_id, topic)):
        if message["role"] == "user":
            return message["content"]
    return ""


def set_pending_questions(
    conversation_id: str | None,
    topic: str,
    questions: list[str],
) -> None:
    """记下 Agent 这一轮追问了什么。下一条消息会被当成对这些问题的回答。

    传入空列表即表示这一轮没有追问，等价于清除 pending。
    """
    if not conversation_id:
        return

    cleaned = [question.strip() for question in questions if question.strip()]
    cleaned = cleaned[:MAX_PENDING_QUESTIONS]
    key = (conversation_id, topic)
    with _lock:
        session = _touch_session(key, time.time())
        session["pending_questions"] = cleaned
        _append(session, key, {"type": "pending", "questions": cleaned})


def get_pending_questions(conversation_id: str | None, topic: str) -> tuple[str, ...]:
    if not conversation_id:
        return ()

    with _lock:
        session = _resolve_session((conversation_id, topic), time.time())
        if session is None:
            return ()
        return tuple(session["pending_questions"])


def set_context_value(
    conversation_id: str | None,
    topic: str,
    key: str,
    value: object,
) -> None:
    if not conversation_id or not key:
        return

    session_key = (conversation_id, topic)
    with _lock:
        session = _touch_session(session_key, time.time())
        session["context"][key] = value
        _append(session, session_key, {"type": "context", "key": key, "value": value})


def get_context_value(
    conversation_id: str | None,
    topic: str,
    key: str,
) -> object | None:
    if not conversation_id or not key:
        return None

    with _lock:
        session = _resolve_session((conversation_id, topic), time.time())
        if session is None:
            return None
        return session["context"].get(key)


def clear_history(conversation_id: str | None, topic: str) -> None:
    """开一段新对话。

    日志不删——写一条重置标记，重建时从这里开始算新会话。
    """
    if not conversation_id:
        return

    key = (conversation_id, topic)
    with _lock:
        session = _sessions.get(key)
        if session is not None:
            _append(session, key, {"type": "reset"})
            _sessions.pop(key, None)
            return

        # 内存里没有，但日志可能有：也要留下重置标记，否则重启后旧对话会复活。
        if _journal_path(key).exists():
            _append(_new_session(time.time()), key, {"type": "reset"})
