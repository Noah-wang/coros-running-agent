from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from agents.coros_report.agent import fetch_coros_results, plan_coros_tools, render_coros_report
from src.runtime.llm import complete_json, complete_text


class CorosGraphState(TypedDict, total=False):
    user_request: str
    intent: str
    tool_calls: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    draft_report: str
    review_passed: bool
    review_notes: str
    revised_report: str
    final_report: str


async def route_request(state: CorosGraphState) -> dict[str, str]:
    return {"intent": "workout_report"}


async def plan_tools(state: CorosGraphState) -> dict[str, list[dict[str, Any]]]:
    tool_calls = await plan_coros_tools(state["user_request"])
    return {"tool_calls": tool_calls}


async def fetch_data(state: CorosGraphState) -> dict[str, list[dict[str, Any]]]:
    results = await fetch_coros_results(state.get("tool_calls", []))
    return {"tool_results": results}


async def generate_report(state: CorosGraphState) -> dict[str, str]:
    results = state.get("tool_results", [])
    if not results:
        return {
            "draft_report": (
                "我已经连上 COROS MCP，但还没有找到可以读取运动数据的工具。"
            )
        }

    report = await render_coros_report(state["user_request"], results)
    return {"draft_report": report}


async def critic_review(state: CorosGraphState) -> dict[str, object]:
    review = await complete_json(
        "你是运动报告质量审查器。只返回 JSON。",
        f"""
检查下面报告是否合格。

检查内容、安全和 ShadowRunner 复盘结构。
每天训练报告不能因为看起来例行就压成三五句话；普通训练也必须解释阶段、
瓶颈、适用域、边际收益和下一步最小实验。

要求：
- 不编造 COROS 没提供的数据
- 不展示内部 ID、坐标、FIT、设备 ID
- 不给医疗诊断、用药或康复处方
- 不承诺成绩，不给单点成绩预测
- 如果给了训练建议，这个建议必须低风险、可回滚、可验证
- 如果下了明确结论，必须能看出依据；证据不足时应该说明不确定
- 不应该有为了凑篇幅而写的空话
- 除非命中风险信号，否则应包含这些二级标题：当前判断、已知条件与缺失信息、阶段与瓶颈、适用域与边际收益、下一步最小实验、记录

返回：
{{
  "passed": true,
  "notes": "..."
}}

报告：
{state["draft_report"]}
""".strip(),
    )

    return {
        "review_passed": bool(review.get("passed")),
        "review_notes": str(review.get("notes", "")),
    }


async def revise_report(state: CorosGraphState) -> dict[str, str]:
    revised = await complete_text(
        "你是 COROS 运动报告修订器。根据审查意见修订报告，不添加新数据。",
        f"""
用户请求：
{state["user_request"]}

审查意见：
{state.get("review_notes", "")}

原报告：
{state["draft_report"]}
""".strip(),
    )
    return {"revised_report": revised}


async def final_report(state: CorosGraphState) -> dict[str, str]:
    return {"final_report": state.get("revised_report") or state["draft_report"]}


def after_critic(state: CorosGraphState) -> Literal["revise_report", "final_report"]:
    if state.get("review_passed"):
        return "final_report"
    return "revise_report"


builder = StateGraph(CorosGraphState)
builder.add_node("route_request", route_request)
builder.add_node("plan_tools", plan_tools)
builder.add_node("fetch_data", fetch_data)
builder.add_node("generate_report", generate_report)
builder.add_node("critic_review", critic_review)
builder.add_node("revise_report", revise_report)
builder.add_node("final_report", final_report)

builder.add_edge(START, "route_request")
builder.add_edge("route_request", "plan_tools")
builder.add_edge("plan_tools", "fetch_data")
builder.add_edge("fetch_data", "generate_report")
builder.add_edge("generate_report", "critic_review")
builder.add_conditional_edges(
    "critic_review",
    after_critic,
    {
        "revise_report": "revise_report",
        "final_report": "final_report",
    },
)
builder.add_edge("revise_report", "final_report")
builder.add_edge("final_report", END)

coros_graph = builder.compile()


async def generate_coros_graph_report(user_request: str) -> str:
    result = await coros_graph.ainvoke({"user_request": user_request})
    return result["final_report"]
