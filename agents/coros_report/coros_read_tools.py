"""跑步能力交给主 Agent 的只读工具。

主 Agent 回答开放式问题时需要跨来源取数：训练档案、PB、最近训练、
跑步知识库。这些数据原来只能通过各自的命令拿到，而命令的输出是
给人看的固定格式（带选择菜单、带引用块），塞回模型里既浪费上下文
又会诱导它照抄格式。

所以这里给的是**结构化摘要**，不是命令的那份输出。

全部只读。这些工具在没有认证的公开 Web 入口上也会被调用。
"""

from typing import Any

from agents.coros_report.activity_browser import query_activity_records, summarize_activity
from agents.coros_report.personal_bests import format_personal_bests
from agents.coros_report.running_profile import read_athlete_profile
from src.runtime.rag import format_context, known_categories, search_knowledge
from src.runtime.tools import Tool


def get_athlete_profile() -> dict[str, Any]:
    """用户的长期跑步档案：现有成绩、目标、比赛复盘、伤病备注。"""
    profile = read_athlete_profile()
    if not profile:
        return {"note": "还没有记录任何跑步档案。"}
    return profile


async def get_personal_bests() -> str:
    """COROS 自动统计的各距离个人最好成绩。"""
    return format_personal_bests()


async def list_recent_activities(range_text: str = "最近 30 天") -> dict[str, Any]:
    """最近的 COROS 训练记录摘要。"""
    records, arguments, label = await query_activity_records(range_text)
    if not records:
        return {"label": label, "count": 0, "activities": []}

    return {
        "label": label,
        "count": len(records),
        "shown": min(len(records), int(arguments.get("limit", 20) or 20)),
        "activities": [summarize_activity(record) for record in records],
    }


async def search_running_knowledge(
    query: str, limit: int = 3, category: str = ""
) -> str:
    """在跑步书籍和视频知识库里检索。

    category 由模型自己判断该查哪一类。这是 3.36 学到的模式：
    不要用规则去猜，把选择权交给已经看得见候选集的那一方。
    不确定就留空，全库查。
    """
    chunks = await search_knowledge(query, limit=limit, category=category or None)
    if not chunks:
        return f"知识库里没有找到和「{query}」相关的内容。"
    return format_context(chunks)


PROFILE_TOOL = Tool(
    name="get_athlete_profile",
    description=(
        "读取用户的长期跑步档案：现有的半马/全马成绩、目标成绩和目标日期、"
        "比赛复盘记录、伤病备注。回答涉及用户目标、水平、过往比赛问题时用它。"
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    handler=get_athlete_profile,
)

PERSONAL_BESTS_TOOL = Tool(
    name="get_personal_bests",
    description=(
        "读取 COROS 自动统计的个人最好成绩（1K/3K/5K/10K/半马/全马）。"
        "注意这是从 COROS 训练和比赛数据里自动算出来的。"
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    handler=get_personal_bests,
)

RECENT_ACTIVITIES_TOOL = Tool(
    name="list_recent_activities",
    description=(
        "读取最近的 COROS 训练记录摘要（日期、运动类型、距离、时长）。"
        "回答「最近练得怎么样」「这个月跑了多少公里」这类问题时用它。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "range_text": {
                "type": "string",
                "description": "时间范围，例如「最近 30 天」「最近 7 天」，默认最近 30 天",
            }
        },
        "required": [],
    },
    handler=list_recent_activities,
)

KNOWLEDGE_TOOL = Tool(
    name="search_running_knowledge",
    description=(
        "在用户导入的跑步书籍和视频知识库里检索。"
        "回答训练方法、生理学、配速安排、跑鞋装备这类需要资料依据的问题时用它。"
        "知识库按主题分类，用 category 收窄可以避免不相关的主题挤占结果。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索关键词"},
            "limit": {"type": "integer", "description": "返回几段，默认 3"},
            "category": {
                "type": "string",
                "description": (
                    "限定主题分类。training=训练方法和生理学，shoes=跑鞋装备。"
                    "**不确定或者问题跨主题时留空**，留空是全库检索。"
                ),
            },
        },
        "required": ["query"],
    },
    handler=search_running_knowledge,
    # 返回的是书籍原文和 B站字幕——第三方能控制的文本。
    # 一旦它进了上下文，本轮就不再允许写操作。
    returns_untrusted=True,
)


COROS_READ_TOOLS: tuple[Tool, ...] = (
    PROFILE_TOOL,
    PERSONAL_BESTS_TOOL,
    RECENT_ACTIVITIES_TOOL,
    KNOWLEDGE_TOOL,
)
