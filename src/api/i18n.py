"""后端返回文案的中文对照。

**为什么是「出口翻译」而不是「逐处改造」。**

数据页和快捷提问的文案散在 117 个字面量里。挨个改成 `t("key")` 要动 117 处，
每一处都是一次出错机会，而且以后加一个分区就要记得再打一个 key。

这里换个位置：payload 组装完之后，在 API 出口整体走一遍，
按「值」查中文表。改动收在一个函数里，新增文案不翻译也不会坏——
查不到就原样保留英文，最坏情况是这一条没翻，不是整页崩掉。

代价是**同一句英文在不同语境下只能有一个中文**。目前没有这种冲突；
真出现了，再给那一处换成显式 key。
"""

from typing import Any

# 只翻这些键的值。像 "key"、"id"、"image" 这种是程序用的，翻了会出事。
LOCALIZED_KEYS = frozenset(
    {"title", "description", "meta", "prompt", "label", "detail"}
)

ZH: dict[str, str] = {
    # 数据页分区
    "Running data": "运动数据",
    "COROS data": "COROS 数据",
    "Running profile": "运动画像",
    "Personal bests": "个人 PB",
    "Photo memory": "照片记忆",
    "RAG knowledge base": "RAG 知识库",
    "System architecture": "系统架构",
    "RAG pipeline": "RAG 全流程",
    "COROS MCP, PBs, FIT archives, and route maps.": "COROS MCP、PB、FIT 归档与路线图。",
    "Activities, raw FIT files, and route map assets.": "运动记录、FIT 原始文件与路线图素材。",
    "Stable long-term context for training plans and reports.": "长期稳定信息，给训练计划和报告提供背景。",
    "Read-only permanent memory; better results overwrite older PBs automatically.":
        "只读永久记忆；检测到更好成绩时自动覆盖。",
    "Race photos, dates, results, and notes. Writes are only enabled in Discord.":
        "比赛照片、日期、成绩和说明。写入只在 Discord 开放。",
    "Running books, video subtitles, chunks, and embeddings.": "跑步书籍、视频字幕、分块与向量。",

    # 概览指标
    "Data modules": "数据模块",
    "Knowledge chunks": "知识块",
    "FIT files": "FIT 文件",
    "Read-only personal data": "个人只读资料库",
    "Books and video RAG": "书籍与视频 RAG",
    "Raw COROS archive": "COROS 原始运动归档",

    # 运动画像
    "Current fitness profile": "当前运动画像",
    "Running profile is incomplete": "运动画像待补充",
    "No stable profile yet": "暂无稳定画像",
    "Long-term memory · user-confirmed stable facts": "长期记忆 · 用户确认过的稳定信息",
    "Used for training plans, performance bottleneck analysis, and follow-up questions.":
        "用于训练计划、成绩瓶颈分析和后续追问。",
    "Add age, height, weight, recent weekly mileage, and target races through Discord or chat; "
    "they will become long-term memory.":
        "在 Discord 或网页对话里补充年龄、身高体重、近期周跑量、目标比赛后，会进入长期记忆。",
    "Goal data affects training cycle length, long-run planning, and intensity distribution.":
        "目标数据会影响训练周期长度、长距离安排和强度分配。",
    "PBs can only be updated automatically from COROS activity details. "
    "They cannot be manually edited from web or chat.":
        "PB 只能由 COROS 运动详情自动更新，网页和聊天都不能手动改。",
    "Not detected yet": "尚未检测到",

    # FIT / 路线图
    "Raw COROS FIT files": "COROS FIT 原始文件",
    "Raw FIT archive": "FIT 原始归档",
    "Route map assets": "路线图素材",
    "Latest file": "最新文件",
    "Raw COROS activity files are synced locally and can be used later for routes, splits, and maps.":
        "COROS 原始运动文件已同步到本地，之后可以用来做路线、分段和地图。",
    "Generated automatically for outdoor runs with GPS data.": "带 GPS 数据的户外跑会自动生成。",
    "Auto-reporting uses this record to decide whether the latest activity has already been sent.":
        "自动报告用这条记录判断最新运动是否已经推送过。",

    # RAG
    "RAG chunk index": "RAG 分块索引",
    "Embedding vector store": "向量库",
    "Stores chunks from books and videos for evidence retrieval.": "存放书籍和视频的分块，用于检索证据。",
    "Finds similar knowledge chunks with vectors before the LLM writes an answer with citations.":
        "先用向量找出相近的知识块，再让模型带引用作答。",
    "child vectors": "子块向量",
    "chunk_size": "分块大小",
    "overlap": "重叠",
    "model": "模型",

    # 字段标签
    "Date": "日期",
    "Time": "时间",
    "Result": "成绩",
    "Race date": "比赛日期",

    # 快捷提问（标题）
    "List my last 90 days": "查最近 90 天运动记录",
    "Review my latest workout": "查最近一次训练报告",
    "Show my PBs": "查个人 PB",
    "Show my personal bests": "查个人最好成绩",
    "Choose race shoes": "按我的水平选跑鞋",
    "Search shoe reviews": "查跑鞋测评",
    "Find my bottleneck": "查成绩瓶颈",
    "Generate a report from my latest activity": "根据最近一次运动生成报告",
    "Help me build my running profile": "帮我补全运动画像",
    "Show this outdoor running route": "看看这次户外跑的路线",

    # 快捷提问（发给 Agent 的原文）
    "List my COROS activities from the last 90 days": "列出我最近 90 天的运动记录",
    "How was my latest workout, and what should I do next?": "我今天这次训练怎么样？下一次应该怎么练？",
    "Based on my fitness and goal, what shoes should I wear for my next marathon?":
        "根据我的实际水平和目标，我下一场马拉松该穿什么鞋？",
    "What shoe reviews are in my knowledge base? Pick a few that fit my profile":
        "知识库里有哪些跑鞋测评？挑几双适合我的说说",
    "My half marathon is 1:40 and my marathon is 4:30. What should I improve?":
        "我现在半马 1:40，全马 4:30，想提高全马成绩，应该加强哪部分训练？",
    "Explain what is inside my RAG knowledge base": "讲讲我的 RAG 知识库里都有什么",
    "How does my RAG pipeline retrieve answers?": "我的 RAG 是怎么检索出答案的？",
    "Use my current fitness to build a training plan": "根据我当前的水平制定训练计划",
    "Use my goal to plan the next training block": "根据我的目标安排下一个训练周期",
    "Generate a detailed report for my latest COROS workout. "
    "Use the Shadowrunner workout review style.":
        "为我最近一次 COROS 运动生成详细报告，用 Shadowrunner 的复盘风格。",
}


def localize(value: Any, lang: str) -> Any:
    """把 payload 里 LOCALIZED_KEYS 下的文案换成目标语言。

    只处理中文；英文是原文，不用查表。
    """
    if lang != "zh":
        return value
    if isinstance(value, dict):
        return {
            key: (
                ZH.get(item, item)
                if key in LOCALIZED_KEYS and isinstance(item, str)
                else localize(item, lang)
            )
            for key, item in value.items()
        }
    # tuple 也要处理：SAMPLE_ACTIONS 这类常量是元组，
    # 只判断 list 的话它会原样穿过去，表现成「界面切了但快捷提问没切」。
    if isinstance(value, (list, tuple)):
        return [localize(item, lang) for item in value]
    return value
