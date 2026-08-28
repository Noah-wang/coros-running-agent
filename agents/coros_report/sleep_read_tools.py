"""睡眠与恢复的读工具。

**为什么要有这个文件。**

睡眠晨报原来只有一条出口：定时任务推到 Discord。主 Agent 循环的工具表里
一份睡眠数据都没有——四个读工具全是训练记录、长期档案和知识库。

结果是网页上问「我今天的睡眠怎么样」，模型能找到的最近的东西是 `feelings`
（主观感受记录），拿回一个空结果，然后如实说「我没有睡眠数据读取工具」。
同一个问题在 Discord 有一份详尽的晨报，在网页上答不出来——**看起来像两个
不同的产品，其实是同一份数据只接了一个出口。**

这里复用 `generate_sleep_report`，它本来就是纯的：取数 + 用晨报的系统提示词
生成，不发 Discord、不标记已发日期。副作用全在 `check_and_send_...` 里。
所以两条路径**用同一个提示词、同一份数据**，报告内容自然一致。
"""

from datetime import date
from typing import Any

from agents.coros_report.sleep_report import generate_sleep_report
from src.runtime.tools import Tool

# 并行拉五个 COROS 接口 + 让模型写一篇报告，正常就要一两分钟，
# 远超循环默认的 75 秒。不显式放宽的话它每次都超时，
# 而超时只会变成一条「拿不到数据」喂给模型——不报错，只是永远答不出来。
SLEEP_REPORT_TIMEOUT_SECONDS = 240.0


async def get_sleep_report(day: str = "", **_: Any) -> str:
    """生成指定日期的睡眠与恢复报告。day 留空表示最近一晚。"""
    target: date | None = None
    if day.strip():
        try:
            target = date.fromisoformat(day.strip())
        except ValueError:
            return f"日期格式不对：{day!r}。要 YYYY-MM-DD，或者留空表示最近一晚。"
    return await generate_sleep_report(day=target)


SLEEP_REPORT_TOOL = Tool(
    name="get_sleep_report",
    description=(
        "读取 COROS 的睡眠与恢复数据（睡眠分、深睡比例、清醒时长、入睡时间、"
        "HRV、静息心率、恢复状态、训练负荷），生成一份睡眠与恢复报告。"
        "用户问「我昨晚睡得怎么样」「最近睡眠质量如何」「恢复得怎么样」"
        "「今天适合上强度吗」这类问题时用它。"
        "**这是唯一的睡眠数据来源**——训练记录和主观感受里都没有睡眠。"
        "返回的内容已经是给用户看的格式，直接交出去，不要复述或改写。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "day": {
                "type": "string",
                "description": "要看哪一天的睡眠，格式 YYYY-MM-DD。留空表示最近一晚。",
            }
        },
        "required": [],
    },
    handler=get_sleep_report,
    timeout_seconds=SLEEP_REPORT_TIMEOUT_SECONDS,
    # 晨报已经是成品，原样透出。**这是为了两个入口格式一致**——
    # 定时任务推到 Discord 的和聊天里问出来的，必须是同一份东西。
    passthrough=True,
)

SLEEP_READ_TOOLS = (SLEEP_REPORT_TOOL,)
