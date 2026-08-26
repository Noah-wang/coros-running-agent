import math
from datetime import date, datetime
from typing import Any

from agents.coros_report.running_profile import apply_profile_patch, read_athlete_profile
from src.runtime.knowledge_health import inspect_knowledge_index
from src.runtime.tools import Tool, ToolRegistry


# Daniels/Gilbert VDOT 公式里的常数
_VO2_A = -4.60
_VO2_B = 0.182258
_VO2_C = 0.000104

RACE_DISTANCES_M = {
    "1500": 1500.0,
    "3k": 3000.0,
    "5k": 5000.0,
    "10k": 10000.0,
    "half": 21097.5,
    "marathon": 42195.0,
}

# 各训练强度占 VDOT 的百分比
INTENSITY = {
    "E": (0.65, 0.74),
    "M": (0.84, 0.84),
    "T": (0.88, 0.88),
    "I": (0.98, 0.98),
    "R": (1.05, 1.05),
}

INTENSITY_LABEL = {
    "E": "轻松跑",
    "M": "马拉松配速",
    "T": "阈值跑",
    "I": "间歇",
    "R": "重复跑",
}


def _parse_time_to_seconds(value: str) -> float:
    parts = [part.strip() for part in str(value).split(":")]
    if not parts or any(not part for part in parts):
        raise ValueError(f"无法解析成绩时间：{value}")

    numbers = [float(part) for part in parts]
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
    elif len(numbers) == 2:
        hours, minutes, seconds = 0.0, numbers[0], numbers[1]
    else:
        raise ValueError(f"无法解析成绩时间：{value}")
    return hours * 3600 + minutes * 60 + seconds


def _format_pace(seconds_per_km: float) -> str:
    total = int(round(seconds_per_km))
    return f"{total // 60}:{total % 60:02d}"


def _velocity_from_vo2(vo2: float) -> float:
    """由 VO2 反解速度（米/分钟）。"""
    discriminant = _VO2_B**2 - 4 * _VO2_C * (_VO2_A - vo2)
    return (-_VO2_B + math.sqrt(discriminant)) / (2 * _VO2_C)


def _vdot_from_result(distance_m: float, seconds: float) -> float:
    minutes = seconds / 60
    velocity = distance_m / minutes
    vo2 = _VO2_A + _VO2_B * velocity + _VO2_C * velocity**2
    percent_max = (
        0.8
        + 0.1894393 * math.exp(-0.012778 * minutes)
        + 0.2989558 * math.exp(-0.1932605 * minutes)
    )
    return vo2 / percent_max


def training_paces(distance: str, finish_time: str) -> dict[str, Any]:
    """根据一次比赛成绩推算 VDOT 和各强度训练配速。"""
    key = str(distance).strip().lower()
    if key not in RACE_DISTANCES_M:
        return {
            "error": f"不支持的距离：{distance}",
            "supported": sorted(RACE_DISTANCES_M),
        }

    distance_m = RACE_DISTANCES_M[key]
    seconds = _parse_time_to_seconds(finish_time)
    vdot = _vdot_from_result(distance_m, seconds)

    paces: dict[str, Any] = {}
    for zone, (low, high) in INTENSITY.items():
        slow = 60000 / _velocity_from_vo2(vdot * low)
        fast = 60000 / _velocity_from_vo2(vdot * high)
        entry: dict[str, Any] = {"名称": INTENSITY_LABEL[zone]}
        if abs(slow - fast) < 1:
            entry["每公里"] = f"{_format_pace(fast)}/km"
        else:
            entry["每公里"] = f"{_format_pace(fast)}~{_format_pace(slow)}/km"
        if zone in {"I", "R"}:
            entry["每400米"] = f"{_format_pace(fast * 0.4)}"
        paces[zone] = entry

    equivalents = {}
    for name, meters in RACE_DISTANCES_M.items():
        if name in {"1500", "3k"}:
            continue
        equivalents[name] = _equivalent_time(vdot, meters)

    return {
        "输入": {"距离": key, "成绩": finish_time},
        "vdot": round(vdot, 1),
        "训练配速": paces,
        "同等水平成绩预测": equivalents,
        "说明": "基于 Daniels/Gilbert 公式计算，与书中表格可能有几秒误差。",
    }


def _equivalent_time(vdot: float, distance_m: float) -> str:
    """给定 VDOT，二分求该距离的同等水平成绩。"""
    low, high = 60.0, 40000.0
    for _ in range(60):
        mid = (low + high) / 2
        if _vdot_from_result(distance_m, mid) > vdot:
            low = mid
        else:
            high = mid
    total = int(round((low + high) / 2))
    return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def race_countdown(target_date: str | None = None) -> dict[str, Any]:
    """返回今天的日期，以及距离目标比赛还有多久。

    不传 target_date 时，会去长期记忆里找已经记录的目标比赛日期。
    """
    today = date.today()
    result: dict[str, Any] = {
        "今天": today.isoformat(),
        "星期": "一二三四五六日"[today.weekday()],
    }

    resolved = target_date
    source = "调用方提供"
    if not resolved:
        profile = read_athlete_profile()
        for goal in profile.get("goals", []):
            if isinstance(goal, dict) and goal.get("target_date"):
                resolved = str(goal["target_date"])
                source = "长期记忆"
                break

    if not resolved:
        result["目标比赛"] = "未知，长期记忆里没有记录目标比赛日期"
        return result

    try:
        target = datetime.strptime(resolved.strip(), "%Y-%m-%d").date()
    except ValueError:
        result["目标比赛"] = f"日期格式无法解析：{resolved}"
        return result

    days = (target - today).days
    result["目标比赛日期"] = target.isoformat()
    result["日期来源"] = source
    result["剩余天数"] = days
    result["剩余周数"] = round(days / 7, 1)
    if days < 0:
        result["提示"] = "这个日期已经过去了"
    return result


def save_running_profile(
    body_metrics: dict[str, Any] | None = None,
    current_times: dict[str, Any] | None = None,
    training_context: dict[str, Any] | None = None,
    goals: list[Any] | None = None,
    race_notes: list[Any] | None = None,
    injury_notes: list[Any] | None = None,
    preferences: list[Any] | None = None,
) -> dict[str, Any]:
    """把用户明确说出的长期信息写入跑步档案。"""
    patch = {
        "body_metrics": body_metrics,
        "current_times": current_times,
        "training_context": training_context,
        "goals": goals,
        "race_notes": race_notes,
        "injury_notes": injury_notes,
        "preferences": preferences,
    }
    return {"result": apply_profile_patch(patch)}


TRAINING_PACES_TOOL = Tool(
    name="training_paces",
    description=(
        "根据用户的一次比赛成绩计算 VDOT 和 E/M/T/I/R 各强度的训练配速，"
        "并给出其他距离的同等水平成绩预测。"
        "只要需要给出具体配速数字，就必须调用这个工具，不要自己心算。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "distance": {
                "type": "string",
                "enum": sorted(RACE_DISTANCES_M),
                "description": "比赛距离",
            },
            "finish_time": {
                "type": "string",
                "description": "完赛成绩，格式 HH:MM:SS 或 MM:SS，例如 1:40:00",
            },
        },
        "required": ["distance", "finish_time"],
    },
    handler=training_paces,
)

RACE_COUNTDOWN_TOOL = Tool(
    name="race_countdown",
    description=(
        "获取今天的日期，以及距离目标比赛还有多少天和多少周。"
        "涉及今天几号、还剩几周、训练周期怎么分期时必须调用，不要凭空假设日期。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "target_date": {
                "type": "string",
                "description": "目标比赛日期 YYYY-MM-DD。不传则读取长期记忆里的目标。",
            },
        },
        "required": [],
    },
    handler=race_countdown,
)

SAVE_PROFILE_TOOL = Tool(
    name="save_running_profile",
    description=(
        "把用户明确说出的长期信息写入跑步档案，例如年龄、身高、体重、比赛成绩、"
        "目标成绩和日期、周跑量、每周训练天数、最长跑、比赛崩盘原因、伤病和偏好。"
        "只写用户真正说过的内容，不要推断或补全。用户只是提问时不要调用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "body_metrics": {
                "type": "object",
                "description": "age / height_cm / weight_kg",
                "properties": {
                    "age": {"type": "number"},
                    "height_cm": {"type": "number"},
                    "weight_kg": {"type": "number"},
                },
            },
            "current_times": {
                "type": "object",
                "description": "当前成绩，值统一为 HH:MM:SS",
                "properties": {
                    "five_k": {"type": "string"},
                    "ten_k": {"type": "string"},
                    "half_marathon": {"type": "string"},
                    "marathon": {"type": "string"},
                },
            },
            "training_context": {
                "type": "object",
                "properties": {
                    "training_days_per_week": {"type": "number"},
                    "weekly_mileage_km": {"type": "number"},
                    "recent_long_run_km": {"type": "number"},
                },
            },
            "goals": {
                "type": "array",
                "description": "目标比赛，含 distance / target_time / target_date",
                "items": {"type": "object"},
            },
            "race_notes": {
                "type": "array",
                "description": "比赛经历，含 distance / time / issue",
                "items": {"type": "object"},
            },
            "injury_notes": {"type": "array", "items": {"type": "string"}},
            "preferences": {"type": "array", "items": {"type": "string"}},
        },
        "required": [],
    },
    handler=save_running_profile,
)


INSPECT_INDEX_TOOL = Tool(
    name="inspect_knowledge_index",
    description=(
        "体检跑步知识库的分块质量，返回块数、来源分布、长度分布、"
        "空块和被切断的块数量、向量对齐情况。"
        "刚导入新资料后、或用户问知识库怎么样、检索为什么不准时调用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "只看某个来源，支持部分匹配，例如 BV1XN411G7AQ。不传则看全部。",
            },
        },
        "required": [],
    },
    handler=inspect_knowledge_index,
)


def build_running_registry(read_only: bool = False) -> ToolRegistry:
    """跑步教练的工具集。

    read_only 用于公开的 Web 入口：那里没有认证，任何人都能对话，
    所以不能把写长期档案的工具交出去——否则陌生人可以往档案里塞任意内容，
    而这些内容之后会被当成用户说过的事实用于生成训练建议。
    """
    tools = [TRAINING_PACES_TOOL, RACE_COUNTDOWN_TOOL, INSPECT_INDEX_TOOL]
    if not read_only:
        tools.append(SAVE_PROFILE_TOOL)
    return ToolRegistry(tuple(tools))


def build_ingest_registry() -> ToolRegistry:
    """导入资料后做质检用的最小工具集。"""
    return ToolRegistry((INSPECT_INDEX_TOOL,))
