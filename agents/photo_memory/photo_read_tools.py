"""照片能力交给主 Agent 的只读工具。

「我一共跑过几场比赛」的答案在这里，不在 COROS 训练记录里——
COROS 记的是每天的训练，比赛是用户自己上传照片时标注的。
主 Agent 原来没有任何途径读到这份数据，只能把问题误判成查训练流水。

这里只暴露摘要，不暴露文件路径：主 Agent 用它回答问题，
真要看图还是走 photo 能力的检索，那条路才会发图。
"""

from typing import Any

from agents.photo_memory.photo_store import list_recent_groups
from src.runtime.tools import Tool


def list_races(limit: int = 50) -> dict[str, Any]:
    """列出所有已记录的比赛（照片分组）。"""
    groups = list_recent_groups(limit=limit)
    races = [
        {
            "event": group.get("event", ""),
            "race_date": group.get("race_date", "") or "未记录",
            "result": group.get("result", "") or "未记录",
            # 叫 photo_count 而不是 photos，是为了不让它看起来像图片本身
            "photo_count": group.get("photo_count", 0),
        }
        for group in groups
    ]
    # 「未命名照片」是用户传了图但还没说是哪场比赛的分组，不能算作一场比赛，
    # 但也不能默默丢掉——否则用户数了 3 组照片、agent 说 2 场，对不上。
    named = [race for race in races if race["event"] and race["event"] != "未命名照片"]
    unnamed = len(races) - len(named)

    result: dict[str, Any] = {"race_count": len(named), "races": named}
    # **张数不是图片。** 模型拿到 photo_count 之后会以为已经答完了，
    # 于是「给我看照片」这类问题停在这里，一张图都不显示，
    # 还反过来告诉用户「实际图片要你自己去照片库看」。
    #
    # 提示词管不住这件事——工具返回是更强的信号，所以写在数据里。
    if any(race["photo_count"] for race in named):
        result["images_included"] = False
        result["next_step"] = (
            "这里只有张数，没有图片本身。用户要看照片时，"
            "必须再调用 photo 工具，它才会把原图显示出来。"
        )
    # 只在真有未标注分组时才提。无条件写进 note 的话，模型会把这句说明
    # 当成事实，回答里凭空多出一组根本不存在的照片。
    if unnamed:
        result["unnamed_group_count"] = unnamed
        result["note"] = (
            f"另有 {unnamed} 组照片还没标注是哪场比赛，未计入 race_count。"
        )
    return result


LIST_RACES_TOOL = Tool(
    name="list_races",
    description=(
        "列出用户记录过的所有比赛，含赛事名、比赛日期、完赛成绩和照片张数。"
        "回答「跑过几场比赛」「参加过哪些马拉松」「上一场比赛成绩多少」"
        "这类问题时用它。注意：这是用户上传比赛照片时标注的比赛记录，"
        "和 COROS 的日常训练记录是两回事。"
        "**它只返回张数，不返回图片本身**——用户要看照片时改用 photo 工具。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "最多返回多少组，默认 50",
            }
        },
        "required": [],
    },
    handler=list_races,
)


PHOTO_READ_TOOLS: tuple[Tool, ...] = (LIST_RACES_TOOL,)
