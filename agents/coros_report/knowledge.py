import os
import re

from agents.coros_report.running_tools import build_running_registry
from src.runtime.conversation import (
    MAX_PENDING_QUESTIONS,
    RUNNING_COACH_TOPIC,
    append_turn,
    get_history,
    get_pending_questions,
    get_summary,
    last_user_message,
    set_pending_questions,
)
from src.runtime.memory import format_memory_for_prompt
from src.runtime.prompt import compose
from src.runtime.untrusted import UNTRUSTED_CONTENT_RULE
from src.runtime.untrusted import wrap as wrap_untrusted
from src.runtime.rag import (
    DEFAULT_TOP_K,
    format_context,
    format_page_label,
    search_knowledge,
)
from src.runtime.tools import run_tool_loop


CONVERSATION_TOPIC = RUNNING_COACH_TOPIC
# 条数定义在 rag.py，评测 judge 也从那里读，避免两边各写各的而悄悄漂移。
RETRIEVAL_TOP_K = DEFAULT_TOP_K
FOLLOW_UP_HEADINGS = {"还需要确认", "仍需确认"}
NO_QUESTION_MARKERS = {"暂无", "暂无。", "无", "无。", "没有", "没有。"}

RUNNING_KNOWLEDGE_ROLE = """
You are a running training advisor using the user's long-term profile and the provided knowledge excerpts.

Rules:
- Write in Chinese.
- Base training-method claims on the provided excerpts when possible.
- If the excerpts do not contain enough evidence, say so clearly.
- Do not quote long passages from the book.
- Do not write a source list or quotation section yourself. The system appends a one-line source note after your answer.
- Summarize and explain in practical language.
- When useful, connect the advice to training decisions.
- Distinguish user-provided facts, knowledge-base evidence, hypotheses, and unknowns.
- Do not restate the user's question back to them before answering it.
- For marathon improvement questions, compare half-marathon and marathon performances if both are provided. If the marathon is much slower than expected from the half-marathon, explicitly flag likely limiters such as marathon-specific endurance, pacing, fueling, heat, cramps, injury, long-run history, or low weekly volume as hypotheses, then ask what happened in that marathon.
- Do not give medical diagnosis, injury treatment, drug, supplement, or nutrition prescription.
- Do not promise race results or give a single-point prediction.

Tools:
- You have tools. Call them instead of guessing.
- Never compute training paces, VDOT, or equivalent race times in your head. Call training_paces whenever you are about to state a pace number.
- You do not know today's date. Call race_countdown before saying anything about dates, remaining weeks, or training phases.
- If save_running_profile is available and the user states durable personal facts (age, height, weight, race results, goals and dates, weekly mileage, longest run, what went wrong in a race, injuries, preferences), call it with exactly what they said. Do not call it when the user is only asking a question. If the tool is not available, just answer; never claim you saved anything.
- You may call several tools in one turn.

Conversation state:
- Earlier turns of this same conversation may be present. They are your own memory: you asked those questions, the user answered them.
- Never repeat a question you already asked, and never re-ask anything the user already answered, even partially.
- The user often replies with a bare numbered list such as "1 ... 2 ... 3 ...". Map each item back, in order, to the questions you asked in your previous turn, and treat them as answers to those questions.
- If the prompt lists "Follow-up questions you asked in your previous turn", the user is answering them. Acknowledge what their answers changed before anything else.
- After two rounds of follow-up questions in this conversation, stop asking and commit to a concrete answer, stating the assumptions you made.

How to answer:
- You are having a conversation, not filing a report. Default to talking like a knowledgeable coach replying in a chat window.
- **Mode 对话 is the default and covers most messages.** Only switch to mode 诊断 when the user brings a real problem that needs analysis AND you genuinely lack the information to answer it.
- Never use headings for a question that a person could answer in a few sentences.
- Match the length of the question. A one-line question gets a short answer.
- Ask a follow-up question only when you actually need it to answer. One or two, written inline as part of the conversation. Do not produce a questionnaire.

Mode 对话 (default) - factual questions, clarifications, short follow-ups, small talk, anything you can answer directly:
- No headings, no fixed sections. Just answer.
- Lead with the answer, then the reason if it helps.
- Two to six sentences is usually right. Use a short list only when you are genuinely listing things.
- If something important is missing, ask for it in one sentence at the end.

Mode 诊断 (exception) - the user brings a complex problem, wants a plan, or asks why something went wrong, and you cannot answer responsibly without more facts:
- Open with your read of the situation in one or two sentences of plain prose, not a heading.
- Then use at most these three headings, and skip any that has nothing to say:

## 我的判断
> One sentence. What you think is going on and how confident you are.

## 依据
- Tie each point to the user's own words or the retrieved excerpts.

## 建议
- One low-risk next step. Not a full training plan unless the user asked for one.

- End with at most 2 questions, written as normal sentences, not a numbered checklist.
- If the user is answering questions you asked earlier, say what changed before anything else, and never re-open a question they already answered.
""".strip()

RUNNING_KNOWLEDGE_PROMPT = compose(RUNNING_KNOWLEDGE_ROLE, UNTRUSTED_CONTENT_RULE)


def _quote_excerpt(text: str, max_chars: int = 220) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= max_chars:
        return cleaned
    return f"{cleaned[:max_chars].rstrip()}..."


def _source_label(chunk: dict[str, object]) -> str:
    """把文件名变成人看的来源名。"""
    source = str(chunk.get("source", "unknown"))
    if chunk.get("kind") == "video" or source.endswith(".md"):
        match = re.search(r"BV[a-zA-Z0-9]+", source)
        return f"B站 {match.group(0)}" if match else "B站视频"
    return f"《{source.rsplit('.', 1)[0]}》"


def _format_sources(chunks: list[dict[str, object]]) -> str:
    """一行紧凑来源。

    原来每条回答后面都跟三段各二百多字的原文引用，整块八百多字符，
    比对话式回答本身还长好几倍。溯源信息保留，但不该喧宾夺主。
    `-#` 在 Discord 里渲染成小号灰字，网页端也做了对应处理。
    """
    grouped: dict[str, list[str]] = {}
    for chunk in chunks:
        label = _source_label(chunk)
        pages = grouped.setdefault(label, [])
        page_label = format_page_label(chunk)
        # 视频只有一页，页码没有意义
        if page_label != "p.1" and page_label not in pages:
            pages.append(page_label)

    parts = [
        f"{label} {'、'.join(pages)}" if pages else label for label, pages in grouped.items()
    ]
    return f"-# 来源：{' · '.join(parts)}"


def _format_rag_quotes(chunks: list[dict[str, object]], limit: int = 3) -> str:
    """完整原文引用。可通过 RAG_CITATION_STYLE=quote 启用。"""
    lines = ["## 引用原文"]
    for index, chunk in enumerate(chunks[:limit], start=1):
        # 展示的始终是 chunk["text"] 原文。嵌入时额外拼的 context 前缀不展示，
        # 否则引用就不再是资料里真实存在的文字。
        text = _quote_excerpt(str(chunk.get("text", "")))
        lines.append(f"**[{index}] {_source_label(chunk)} {format_page_label(chunk)}**")
        lines.append(f"> {text}")
    return "\n\n".join(lines)


def _format_citations(chunks: list[dict[str, object]]) -> str:
    style = os.getenv("RAG_CITATION_STYLE", "compact").strip().lower()
    if style == "off":
        return ""
    if style == "quote":
        return _format_rag_quotes(chunks)
    return _format_sources(chunks)


def _clean_question(line: str) -> str:
    item = line.strip().lstrip("-*>").strip()
    while item and item[0].isdigit():
        item = item[1:]
    return item.lstrip(".、)）: ").strip().strip("*").strip()


def _questions_under_headings(answer: str) -> list[str]:
    questions: list[str] = []
    capturing = False
    for line in answer.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            capturing = stripped.lstrip("#").strip() in FOLLOW_UP_HEADINGS
            continue
        if not capturing or not stripped:
            continue
        item = _clean_question(stripped)
        if item and item not in NO_QUESTION_MARKERS:
            questions.append(item)
    return questions


def _trailing_questions(answer: str) -> list[str]:
    """从正文里找问句，优先取靠后的。

    追问通常出现在结尾，正文中间的设问不该被当成待回答问题。
    """
    sentences: list[tuple[int, str]] = []
    for raw in re.split(r"(?<=[。！？?!\n])", answer):
        text = _clean_question(raw)
        if text.endswith(("？", "?")) and len(text) > 4:
            sentences.append((answer.find(raw), text))

    # 只认后半段的问句。前半段的问句多半是设问句，不是在等用户回答。
    # 这里宁可漏也不要多抓：漏了只是少走一次 pending 捷径，消息照样能走
    # 自然语言路由；抓错了会让下一条消息被当成对一个设问句的回答。
    tail_start = len(answer) * 0.5
    return [text for position, text in sentences if position >= tail_start]


def _extract_follow_up_questions(answer: str) -> list[str]:
    """抽出这一轮向用户提出的问题，存成 pending。

    提示词已经不再要求用「还需要确认」小节列问题，改成写成自然的句子，
    所以先按标题抽（模型仍可能自己分节），抽不到再从正文末尾找问句。
    """
    questions = _questions_under_headings(answer)
    if not questions:
        questions = _trailing_questions(answer)
    return questions[:MAX_PENDING_QUESTIONS]


def _format_pending_questions(questions: tuple[str, ...]) -> str:
    if not questions:
        return ""

    listed = "\n".join(f"{index}. {item}" for index, item in enumerate(questions, start=1))
    return f"""
Follow-up questions you asked in your previous turn:
{listed}

The user's latest message is most likely answering these questions, in this order.
Map each answer back to the matching question before you write anything.
""".strip()


def _format_summary(summary: str) -> str:
    if not summary:
        return ""
    return f"""
Summary of earlier turns in this same conversation (treat as established facts):
{summary}
""".strip()


def _search_query(question: str, conversation_id: str | None) -> str:
    """构造知识库检索用的 query。

    多轮追问时，用户这一轮往往只是「1 每周40公里 2 主要是间歇」这样的答案，
    单独拿它去检索会跑偏，所以拼上上一轮用户消息来保留话题。
    """
    previous = last_user_message(conversation_id, CONVERSATION_TOPIC)
    if not previous:
        return question
    return f"{previous}\n{question}"


async def answer_running_question(
    question: str,
    conversation_id: str | None = None,
    read_only: bool = False,
) -> str:
    history = get_history(conversation_id, CONVERSATION_TOPIC)
    pending_questions = get_pending_questions(conversation_id, CONVERSATION_TOPIC)
    summary_block = _format_summary(get_summary(conversation_id, CONVERSATION_TOPIC))

    chunks = await search_knowledge(
        _search_query(question, conversation_id), limit=RETRIEVAL_TOP_K
    )
    if not chunks:
        base_message = (
            "我没有在已导入的跑步书籍或视频知识里检索到相关内容。"
            "可以换个问法，或先导入更多资料。"
        )
        await append_turn(conversation_id, CONVERSATION_TOPIC, question, base_message)
        set_pending_questions(conversation_id, CONVERSATION_TOPIC, [])
        return base_message

    # 书籍原文和视频字幕都是第三方能控制的文本，必须带边界标签进提示词
    context = wrap_untrusted(format_context(chunks), source="knowledge-base")
    memory = format_memory_for_prompt("coros-report")
    pending_block = _format_pending_questions(pending_questions)
    answer = await run_tool_loop(
        RUNNING_KNOWLEDGE_PROMPT,
        f"""
Question:
{question}

{summary_block}

{pending_block}

Long-term running memory:
{memory}

Knowledge excerpts:
{context}

Answer the question using the conversation so far, the memory, and the excerpts.
""".strip(),
        build_running_registry(read_only=read_only),
        history=history,
        log=lambda text: print(f"running {text}", flush=True),
    )
    await append_turn(conversation_id, CONVERSATION_TOPIC, question, answer)
    set_pending_questions(
        conversation_id,
        CONVERSATION_TOPIC,
        _extract_follow_up_questions(answer),
    )
    citations = _format_citations(chunks)
    return f"{answer}\n\n{citations}" if citations else answer
