import argparse
import asyncio
import hmac
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlparse

from dotenv import load_dotenv

from agents.coros_report.activity_browser import summarize_activity
from agents.coros_report.auto_report import activity_key, recent_coros_activities
from agents.coros_report.shadowrunner_prompt import REPORT_SYSTEM_PROMPT
from agents.coros_report.sleep_report_prompt import SLEEP_REPORT_SYSTEM_PROMPT
from src.api.i18n import localize
from src.runtime import ratelimit
from src.runtime.flow_map import module_payload
from src.runtime.trace import log_event
from src.runtime.prompt_skills import (
    activate_skill,
    list_skills,
    reset_skill,
    save_skill,
)
from src.runtime.runtime_settings import automation_payload, set_automation_enabled


ROOT_DIR = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT_DIR / "web"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
# 公开入口只读。running-video、feel、coros-fit-sync 是写操作，不在白名单里。
#
# **这份名单是网页那一路的真正来源**，它作为实参传给 dispatch_web_text，
# 会覆盖那边的默认值。两处都写一遍白名单很容易只改一处——
# photo 就是这么漏的：改了 dispatch_web_text 的默认值，
# 网页却依然拿不到，因为实参在这里。
WEB_COMMANDS = (
    "coros",
    "coros-tools",
    "coros-list",
    "coros-activity",
    "coros-pb",
    "running",
    "feelings",
    # photo 是 read_only_safe：能力层自己按 read_only 只走检索分支，
    # 保存和改标注在这条路上不会执行。
    "photo",
)


@dataclass(frozen=True)
class DemoResponse:
    message: str
    capability: str
    confidence: float
    tools: tuple[str, ...]
    graph_steps: tuple[str, ...]
    citations: tuple[str, ...]
    memory: tuple[str, ...]


SAMPLE_PROMPTS = {
    "Running Q&A": "My half marathon is 1:40 and my marathon is 4:30. What should I improve?",
    "Workout report": "How was my latest workout, and what should I do next?",
    "Activity list": "List my COROS activities from the last 90 days",
}

SAMPLE_ACTIONS = (
    {
        "title": "List my last 90 days",
        "description": "",
        "prompt": "List my COROS activities from the last 90 days",
        "mode": "web",
    },
    {
        "title": "Review my latest workout",
        "description": "",
        "prompt": "How was my latest workout, and what should I do next?",
        "mode": "web",
    },
    {
        "title": "Show my PBs",
        "description": "",
        "prompt": "Show my personal bests",
        "mode": "web",
    },
    {
        "title": "Choose race shoes",
        "description": "",
        "prompt": "Based on my fitness and goal, what shoes should I wear for my next marathon?",
        "mode": "web",
    },
    {
        "title": "Search shoe reviews",
        "description": "",
        "prompt": "What shoe reviews are in my knowledge base? Pick a few that fit my profile",
        "mode": "web",
    },
    {
        "title": "Find my bottleneck",
        "description": "",
        "prompt": "My half marathon is 1:40 and my marathon is 4:30. What should I improve?",
        "mode": "web",
    },
)


def _route_prompt(prompt: str) -> str:
    text = prompt.lower()
    if any(term in text for term in ("pb", "personal best", "personal bests")) or any(
        term in prompt for term in ("个人最好", "最好成绩", "最好记录")
    ):
        return "pb"
    if any(term in prompt for term in ("运动记录", "历史运动", "记录列表")) or any(
        term in text for term in ("activity", "activities", "workouts", "last 90 days")
    ):
        return "activity-list"
    return "running"


def _demo_response(prompt: str) -> DemoResponse:
    route = _route_prompt(prompt)
    if route == "activity-list":
        return DemoResponse(
            message=(
                "Found 3 COROS activities from the last 90 days.\n\n"
                "```text\n"
                "1. 2026-08-20 | Indoor Run | 10.00 km | 39:22\n"
                "2. 2026-08-12 | Indoor Run | 8.01 km | 39:22\n"
                "3. 2026-07-28 | Outdoor Run | 6.20 km | 34:10\n"
                "```\n\n"
                "You can continue with: `Analyze activity 1`. In real mode, the agent reads the selected activity details "
                "and generates a workout report."
            ),
            capability="Activity browser",
            confidence=0.92,
            tools=("COROS MCP", "Activity summary cache"),
            graph_steps=("Query activity summaries", "Render list", "Wait for selection", "Fetch selected details"),
            citations=(),
            memory=("last_activity_list: short-lived selection cache",),
        )
    if route == "pb":
        return DemoResponse(
            message=(
                "Your COROS automatic PBs:\n\n"
                "| Distance | Time | Date | Source |\n"
                "|---|---:|---|---|\n"
                "| 1K | - | - | - |\n"
                "| 3K | - | - | - |\n"
                "| 5K | - | - | - |\n"
                "| 10K | 39:22 | 2026-08-12 | Auto-detected from COROS |\n"
                "| Half marathon | - | - | - |\n"
                "| Marathon | - | - | - |\n\n"
                "PBs are only updated automatically from COROS activity details and cannot be edited manually in chat."
            ),
            capability="COROS automatic PBs",
            confidence=0.92,
            tools=("COROS MCP", "Long-term memory"),
            graph_steps=("Read PB memory", "Return read-only table"),
            citations=(),
            memory=("personal_bests: only updated from COROS activity details",),
        )
    return DemoResponse(
        message=(
            "## Initial read\n"
            "> Your half-marathon ability is clearly stronger than your current marathon result. The limiter is more likely marathon-specific endurance than raw fitness.\n\n"
            "## Why\n"
            "- A 1:40 half marathon usually predicts a much faster marathon than 4:30. That gap often points to endurance, pacing, fueling, or late-race durability.\n\n"
            "## What I still need\n"
            "1. What has your weekly mileage been over the last 1-2 months?\n"
            "2. What was your longest long run before the marathon?\n"
            "3. What happened in the second half of your last marathon?\n\n"
            "## What to do for now\n"
            "- Do not rush into more volume. Put most runs back at conversational easy pace, and keep one consistent long run each week."
        ),
        capability="Running coach",
        confidence=0.94,
        tools=("COROS MCP", "LangGraph", "RAG retrieval", "Coach skill", "Long-term memory"),
        graph_steps=("Route request", "Plan tools", "Fetch context", "Retrieve knowledge", "Generate answer", "Quality check"),
        citations=(
            "Daniels' Running Formula p.143: marathon plans should be based on realistic current ability.",
            "Imported long-form running videos: long-run and fueling content used as supporting context.",
        ),
        memory=("Current result: half marathon 1:40:00", "Current result: marathon 4:30:00"),
    )


def _demo_trace_modules(prompt: str) -> tuple[str, ...]:
    route = _route_prompt(prompt)
    if route == "activity-list":
        return ("entry", "router", "capability", "coros", "answer")
    if route == "pb":
        return ("entry", "router", "capability", "profile", "answer")
    return ("entry", "router", "capability", "profile", "knowledge", "llm", "answer")


ARCHITECTURE_DOC_PATH = ROOT_DIR / "docs" / "ARCHITECTURE.en.md"
RAG_DOC_PATH = ROOT_DIR / "docs" / "rag-pipeline.en.md"


def _split_markdown(text: str) -> list[dict[str, Any]]:
    """把 Markdown 按 ## / ### 两级拆开。"""
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    subsection: dict[str, Any] | None = None
    in_code_block = False

    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_code_block = not in_code_block

        if in_code_block:
            # 代码块里的 ## 是示例内容，不是章节标题。
            # 文档里引用过 prompt 模板中的「## 引用原文」，不排除就会把章节劈开。
            if current is not None:
                target = subsection["body"] if subsection is not None else current["body"]
                target.append(line)
            continue

        if line.startswith("## "):
            current = {"title": line[3:].strip(), "body": [], "subs": []}
            subsection = None
            sections.append(current)
        elif line.startswith("### ") and current is not None:
            subsection = {"title": line[4:].strip(), "body": []}
            current["subs"].append(subsection)
        elif current is not None:
            target = subsection["body"] if subsection is not None else current["body"]
            target.append(line)

    return sections


def _tech_payload() -> dict[str, Any]:
    """把开源版通用技术文档整理成网页 tab。"""
    return {
        "tabs": [
            {"key": "architecture", "title": "System architecture", "items": _doc_items(ARCHITECTURE_DOC_PATH)},
            {"key": "rag", "title": "RAG pipeline", "items": _rag_items()},
        ]
    }


def _doc_items(path: Path) -> list[dict[str, str]]:
    """把通用 Markdown 文档按 ## 一级标题拆成条目。"""
    if not path.exists():
        return []

    sections = _split_markdown(path.read_text(encoding="utf-8"))
    items: list[dict[str, str]] = []
    for section in sections:
        body = _section_body(section)
        if body:
            items.append({"title": section["title"], "body": body})
    return items


def _section_body(section: dict[str, Any]) -> str:
    parts = ["\n".join(section["body"]).strip()]
    for subsection in section["subs"]:
        body = "\n".join(subsection["body"]).strip()
        if body:
            parts.append(f"### {subsection['title']}\n{body}")
    return "\n\n".join(part for part in parts if part).strip()


def _rag_items() -> list[dict[str, str]]:
    """RAG 全流程文档按 ## 一级标题拆成条目。

    这份文档只有一层标题，每个标题就是流水线里的一个环节，
    所以直接用顶级 section 当条目，不像迭代报告那样取子节。
    """
    return _doc_items(RAG_DOC_PATH)


def _json_response(payload: Any, status: HTTPStatus = HTTPStatus.OK) -> tuple[int, bytes, str]:
    return status.value, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8"


def _settings_payload(include_content: bool) -> dict[str, Any]:
    return {
        "automations": automation_payload(),
        "skills": {
            "coach": list_skills(
                "coach", "ShadowRunner", REPORT_SYSTEM_PROMPT, include_content=include_content
            ),
            "sleep": list_skills(
                "sleep",
                "Morning Recovery Coach",
                SLEEP_REPORT_SYSTEM_PROMPT,
                include_content=include_content,
            ),
        },
    }


class WebHandler(BaseHTTPRequestHandler):
    server_version = "CorosRunningAgentWeb/0.1"

    def do_GET(self) -> None:
        self._handle_get(include_body=True)

    def do_HEAD(self) -> None:
        self._handle_get(include_body=False)

    def _lang(self, parsed: Any) -> str:
        """从查询串取语言。默认英文——这是个开源项目，陌生访客占多数。"""
        values = parse_qs(parsed.query).get("lang", [])
        return "zh" if values and values[0] == "zh" else "en"

    def _handle_get(self, include_body: bool) -> None:
        parsed = urlparse(self.path)
        lang = self._lang(parsed)
        if parsed.path == "/api/health":
            self._send(
                *_json_response(
                    {"ok": True, "domain": os.getenv("WEB_PUBLIC_DOMAIN", "localhost")}
                ),
                include_body=include_body,
            )
            return
        if parsed.path == "/api/capabilities":
            # 只暴露给前端做空状态提示的示例问题，不再对外列出内部能力名和命令。
            payload = {
                "project": "COROS Running Agent",
                "sample_prompts": SAMPLE_PROMPTS,
                "sample_actions": SAMPLE_ACTIONS,
            }
            self._send(*_json_response(localize(payload, lang)), include_body=include_body)
            return
        if parsed.path == "/api/tech":
            self._send(*_json_response(localize(_tech_payload(), lang)), include_body=include_body)
            return
        if parsed.path == "/api/showcase":
            self._send(*_json_response(localize(_showcase_payload(), lang)), include_body=include_body)
            return
        if parsed.path == "/api/data":
            self._send(*_json_response(localize(_data_payload(), lang)), include_body=include_body)
            return
        if parsed.path == "/api/auto-report/latest":
            self._send(
                *_json_response(localize(asyncio.run(_auto_report_notice_payload()), lang)),
                include_body=include_body,
            )
            return
        if parsed.path == "/api/settings":
            if not self._settings_authorized():
                self._send(
                    *_json_response({"error": "Administrator token required"}, HTTPStatus.UNAUTHORIZED),
                    include_body=include_body,
                )
                return
            self._send(*_json_response(_settings_payload(include_content=True)), include_body=include_body)
            return
        if parsed.path == "/data":
            self._serve_static("/data.html", include_body=include_body)
            return
        if parsed.path == "/tech":
            self._serve_static("/tech.html", include_body=include_body)
            return
        if parsed.path == "/settings":
            self._serve_static("/settings.html", include_body=include_body)
            return
        if parsed.path.startswith("/media/photo-memory/"):
            self._serve_photo_media(parsed.path, include_body=include_body)
            return
        if parsed.path.startswith("/media/coros-route-maps/"):
            self._serve_route_map_media(parsed.path, include_body=include_body)
            return
        self._serve_static(parsed.path, include_body=include_body)

    def _client_ip(self) -> str:
        """真实客户端 IP。

        服务跑在 127.0.0.1，前面是 Caddy，所以 client_address 永远是本机。
        Caddy 会把真实 IP **追加**到 X-Forwarded-For 末尾，因此取最后一段——
        客户端自己伪造的前缀会被排在前面，取最后一段就伪造不了。
        """
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()
        return self.client_address[0]

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/settings":
            self._handle_settings_post()
            return
        if parsed.path not in {"/api/chat", "/api/chat/stream"}:
            self._send(*_json_response({"error": "Endpoint not found"}, HTTPStatus.NOT_FOUND))
            return

        source = self._client_ip()
        allowed, retry_after = ratelimit.check(source)
        if not allowed:
            log_event("rate_limited", source=source, retry_after=retry_after)
            status, body, content_type = _json_response(
                {"error": f"Too many requests. Try again in {retry_after} seconds."},
                HTTPStatus.TOO_MANY_REQUESTS,
            )
            self._send(
                status,
                body,
                content_type,
                extra_headers={"Retry-After": str(retry_after)},
            )
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8")) if raw else {}
            prompt = str(data.get("message", "")).strip()
            if not prompt:
                self._send(*_json_response({"error": "Message is required"}, HTTPStatus.BAD_REQUEST))
                return

            conversation_id = self._conversation_id(data)
            body_lang = "zh" if str(data.get("lang", "")) == "zh" else "en"

            if parsed.path == "/api/chat/stream":
                self._stream_chat(prompt, conversation_id, body_lang)
                return

            if _web_agent_mode() == "real":
                result = asyncio.run(_collect_real_chat(prompt, conversation_id, body_lang))
                self._send(*_json_response(result))
                return

            response = _demo_response(prompt)
            self._send(*_json_response(asdict(response)))
        except Exception as exc:
            self._send(
                *_json_response(
                    {"error": str(exc) or exc.__class__.__name__},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            )

    def _settings_authorized(self) -> bool:
        expected = os.getenv("WEB_SETTINGS_TOKEN", "").strip()
        if not expected:
            return False
        header = self.headers.get("Authorization", "")
        provided = header[7:].strip() if header.startswith("Bearer ") else ""
        return bool(provided and hmac.compare_digest(provided, expected))

    def _handle_settings_post(self) -> None:
        if not self._settings_authorized():
            self._send(
                *_json_response({"error": "Administrator token required"}, HTTPStatus.UNAUTHORIZED)
            )
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 80 * 1024:
                raise ValueError("Request body must be between 1 byte and 80 KB.")
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            action = str(data.get("action", ""))
            if action == "set_automation":
                set_automation_enabled(str(data.get("name", "")), data.get("enabled"))
            elif action == "save_skill":
                kind = str(data.get("kind", ""))
                skill = save_skill(kind, str(data.get("content", "")), str(data.get("name", "")))
                if bool(data.get("activate", True)):
                    activate_skill(kind, skill.id)
            elif action == "activate_skill":
                kind = str(data.get("kind", ""))
                skill_id = str(data.get("skill_id", ""))
                if skill_id == f"{kind}:built-in":
                    reset_skill(kind)
                else:
                    activate_skill(kind, skill_id)
            elif action == "reset_skill":
                reset_skill(str(data.get("kind", "")))
            else:
                raise ValueError("Unsupported settings action.")
            self._send(*_json_response(_settings_payload(include_content=True)))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._send(*_json_response({"error": str(exc)}, HTTPStatus.BAD_REQUEST))

    def _conversation_id(self, data: dict[str, Any]) -> str:
        raw = str(data.get("session_id", "")).strip()[:64]
        safe = "".join(char for char in raw if char.isalnum() or char in "-_")
        if safe:
            return f"web:{safe}"
        return f"web:{self.client_address[0]}"

    def _stream_chat(self, prompt: str, conversation_id: str, lang: str = "en") -> None:
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        # 必须让连接在流结束后关闭。keep-alive 会让 close_connection=False，
        # 前端的 reader 永远等不到 EOF，一次对话之后就再也发不出消息。
        self.send_header("Connection", "close")
        self.end_headers()

        def emit(payload: dict[str, Any]) -> None:
            body = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
            self.wfile.write(body)
            self.wfile.flush()

        try:
            if _web_agent_mode() == "real":
                asyncio.run(_stream_real_chat(prompt, emit, conversation_id, lang))
            else:
                response = _demo_response(prompt)
                for module in _demo_trace_modules(prompt):
                    emit(module_payload(module, f"demo · {module}"))
                emit({"type": "message", "message": response.message})
            emit({"type": "done"})
        except BrokenPipeError:
            return
        except Exception as exc:
            try:
                emit({"type": "error", "error": str(exc) or exc.__class__.__name__})
            except BrokenPipeError:
                return

    def _serve_static(self, path: str, include_body: bool = True) -> None:
        if path in {"", "/"}:
            file_path = WEB_DIR / "index.html"
        elif path == "/architecture.svg":
            file_path = ROOT_DIR / "docs" / "architecture.svg"
        else:
            safe_path = path.lstrip("/")
            file_path = (WEB_DIR / safe_path).resolve()
            if not str(file_path).startswith(str(WEB_DIR.resolve())):
                self._send(
                    *_json_response({"error": "Forbidden"}, HTTPStatus.FORBIDDEN),
                    include_body=include_body,
                )
                return

        if not file_path.exists() or not file_path.is_file():
            self._send(
                *_json_response({"error": "File not found"}, HTTPStatus.NOT_FOUND),
                include_body=include_body,
            )
            return

        content_type = _content_type(file_path)
        self._send(
            HTTPStatus.OK.value,
            file_path.read_bytes(),
            content_type,
            include_body=include_body,
        )

    def _serve_photo_media(self, path: str, include_body: bool = True) -> None:
        media_root = (ROOT_DIR / "data" / "media" / "photo-memory").resolve()
        safe_path = unquote(path.removeprefix("/media/photo-memory/").lstrip("/"))
        file_path = (media_root / safe_path).resolve()
        # 先 resolve 再比前缀：反过来写的话 "../" 会在解析时逃出目录
        if not str(file_path).startswith(str(media_root)):
            self._send(
                *_json_response({"error": "Forbidden"}, HTTPStatus.FORBIDDEN),
                include_body=include_body,
            )
            return
        if not file_path.exists() or not file_path.is_file():
            self._send(
                *_json_response({"error": "File not found"}, HTTPStatus.NOT_FOUND),
                include_body=include_body,
            )
            return
        self._send(
            HTTPStatus.OK.value,
            file_path.read_bytes(),
            _content_type(file_path),
            include_body=include_body,
        )

    def _serve_route_map_media(self, path: str, include_body: bool = True) -> None:
        media_root = (ROOT_DIR / "data" / "coros-report" / "route-maps").resolve()
        safe_path = unquote(path.removeprefix("/media/coros-route-maps/").lstrip("/"))
        file_path = (media_root / safe_path).resolve()
        if not str(file_path).startswith(str(media_root)):
            self._send(
                *_json_response({"error": "Forbidden"}, HTTPStatus.FORBIDDEN),
                include_body=include_body,
            )
            return
        if not file_path.exists() or not file_path.is_file():
            self._send(
                *_json_response({"error": "File not found"}, HTTPStatus.NOT_FOUND),
                include_body=include_body,
            )
            return
        self._send(
            HTTPStatus.OK.value,
            file_path.read_bytes(),
            _content_type(file_path),
            include_body=include_body,
        )

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        include_body: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        # 静态资源文件名没有版本号，长缓存会让部署后的一段时间内用户拿到旧前端，
        # 所以统一要求每次回源校验。
        self.send_header(
            "Cache-Control",
            "no-store" if content_type.startswith("application/json") else "no-cache",
        )
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print(f"web {self.address_string()} {format % args}", flush=True)


def _content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".ico": "image/x-icon",
    }.get(suffix, "application/octet-stream")


def _read_json(path: Path, fallback: object) -> object:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def _showcase_payload() -> dict[str, Any]:
    personal_bests = _showcase_personal_bests()
    photos = _showcase_photos()
    fit_files = list((ROOT_DIR / "data" / "coros-report" / "fit-files").glob("**/*.fit"))
    route_maps = list((ROOT_DIR / "data" / "coros-report" / "route-maps").glob("*.png"))

    return {
        "summary": {
            "fit_files": len(fit_files),
            "route_maps": len(route_maps),
            "personal_bests": len(personal_bests),
            "photo_groups": len(photos),
        },
        "sections": [
            {
                "key": "running",
                "title": "Running data",
                "description": "COROS MCP, PBs, FIT archives, and route maps.",
                "items": [
                    {
                        "title": "Raw COROS FIT files",
                        "meta": f"{len(fit_files)} FIT files archived",
                        "prompt": "List my COROS activities from the last 90 days",
                    },
                    {
                        "title": "Route map assets",
                        "meta": f"{len(route_maps)} route maps generated",
                        "prompt": "Show my personal bests",
                    },
                    *personal_bests,
                ],
            },
        ],
    }


def _showcase_photos() -> list[dict[str, str]]:
    path = ROOT_DIR / "data" / "photo-memory" / "photos.json"
    records = _read_json(path, [])
    if not isinstance(records, list):
        return []

    items: list[dict[str, str]] = []
    media_root = ROOT_DIR / "data" / "media" / "photo-memory"
    for record in records:
        if not isinstance(record, dict):
            continue
        files = record.get("files")
        image_url = ""
        if isinstance(files, list) and files:
            first = files[0]
            if isinstance(first, dict) and first.get("path"):
                file_path = ROOT_DIR / str(first["path"])
                try:
                    image_url = "/media/photo-memory/" + quote(
                        file_path.relative_to(media_root).as_posix(),
                        safe="/",
                    )
                except ValueError:
                    image_url = ""
        event = str(record.get("event") or "Untitled photos")
        race_date = str(record.get("race_date") or "Date missing")
        result = str(record.get("result") or "Result missing")
        photo_count = len(files) if isinstance(files, list) else 0
        items.append(
            {
                "title": event,
                "meta": f"{race_date} · {result} · {photo_count} photos",
                "prompt": f"Show photos for {event}",
                "image": image_url,
            }
        )
    return items


def _showcase_personal_bests() -> list[dict[str, str]]:
    memory = _read_json(ROOT_DIR / "data" / "memory.json", {})
    if not isinstance(memory, dict):
        return []
    agents = memory.get("agents")
    if not isinstance(agents, dict):
        return []
    coros = agents.get("coros-report")
    if not isinstance(coros, dict):
        return []
    personal_bests = coros.get("personal_bests")
    if not isinstance(personal_bests, dict):
        return []

    labels = {
        "1k": "1K PB",
        "3k": "3K PB",
        "5k": "5K PB",
        "10k": "10K PB",
        "half_marathon": "Half marathon PB",
        "marathon": "Marathon PB",
    }
    items = []
    for key, label in labels.items():
        record = personal_bests.get(key)
        if not isinstance(record, dict):
            continue
        items.append(
            {
                "title": label,
                "meta": f"{record.get('time', '-')} · {record.get('date') or 'Unknown date'}",
                "prompt": "Show my personal bests",
            }
        )
    return items


def _data_payload() -> dict[str, Any]:
    memory = _read_json(ROOT_DIR / "data" / "memory.json", {})
    coros_memory: dict[str, Any] = {}
    if isinstance(memory, dict):
        agents = memory.get("agents")
        if isinstance(agents, dict) and isinstance(agents.get("coros-report"), dict):
            coros_memory = agents["coros-report"]

    sections = [
        _data_profile_section(coros_memory),
        _data_personal_bests_section(coros_memory),
        _data_photos_section(),
        _data_rag_section(),
        _data_coros_archive_section(coros_memory),
    ]
    counts = {
        "sections": len(sections),
        "photos": sum(len(item.get("images", [])) for item in sections[2]["items"]),
        "knowledge_chunks": _json_count(ROOT_DIR / "data" / "knowledge" / "coros-report" / "chunks.json"),
        "fit_files": len(list((ROOT_DIR / "data" / "coros-report" / "fit-files").glob("**/*.fit"))),
    }
    return {
        "summary": [
            {"label": "Data modules", "value": str(counts["sections"]), "detail": "Read-only personal data"},
            {"label": "Knowledge chunks", "value": str(counts["knowledge_chunks"]), "detail": "Books and video RAG"},
            {"label": "FIT files", "value": str(counts["fit_files"]), "detail": "Raw COROS archive"},
        ],
        "sections": sections,
    }


def _data_profile_section(coros_memory: dict[str, Any]) -> dict[str, Any]:
    profile = coros_memory.get("athlete_profile")
    items: list[dict[str, Any]] = []
    if isinstance(profile, dict):
        current_times = profile.get("current_times")
        if isinstance(current_times, dict) and current_times:
            facts = [
                {"label": _race_label(key), "value": str(value)}
                for key, value in current_times.items()
            ]
            items.append(
                {
                    "title": "Current fitness profile",
                    "meta": "Long-term memory · user-confirmed stable facts",
                    "description": "Used for training plans, performance bottleneck analysis, and follow-up questions.",
                    "facts": facts,
                    "prompt": "Use my current fitness to build a training plan",
                }
            )
        goals = profile.get("goals")
        if isinstance(goals, list) and goals:
            seen_goals: set[tuple[str, str, str]] = set()
            for goal in goals:
                if not isinstance(goal, dict):
                    continue
                goal_key = (
                    str(goal.get("distance") or "race"),
                    str(goal.get("target_time") or ""),
                    str(goal.get("target_date") or ""),
                )
                if goal_key in seen_goals:
                    continue
                seen_goals.add(goal_key)
                target = goal.get("target_time") or "Target time missing"
                target_date = goal.get("target_date") or "Target date missing"
                items.append(
                    {
                        "title": f"{_race_label(str(goal.get('distance') or 'race'))} goal",
                        "meta": f"{target} · {target_date}",
                        "description": "Goal data affects training cycle length, long-run planning, and intensity distribution.",
                        "prompt": "Use my goal to plan the next training block",
                    }
                )

    if not items:
        items.append(
            {
                "title": "Running profile is incomplete",
                "meta": "No stable profile yet",
                "description": "Add age, height, weight, recent weekly mileage, and target races through Discord or chat; they will become long-term memory.",
                "prompt": "Help me build my running profile",
            }
        )

    return {
        "key": "profile",
        "title": "Running profile",
        "description": "Stable long-term context for training plans and reports.",
        "items": items,
    }


def _data_personal_bests_section(coros_memory: dict[str, Any]) -> dict[str, Any]:
    labels = {
        "1k": "1K",
        "3k": "3K",
        "5k": "5K",
        "10k": "10K",
        "half_marathon": "Half marathon",
        "marathon": "Marathon",
    }
    personal_bests = coros_memory.get("personal_bests")
    if not isinstance(personal_bests, dict):
        personal_bests = {}

    items = []
    for key, label in labels.items():
        record = personal_bests.get(key)
        if isinstance(record, dict):
            value = str(record.get("time") or "-")
            date = str(record.get("date") or "Unknown date")
            source = str(record.get("source") or "Auto-detected from COROS")
            items.append(
                {
                    "title": label,
                    "meta": f"{value} · {date}",
                    "description": source,
                    "state": "ready",
                    "facts": [
                        {"label": "Time", "value": value},
                        {"label": "Date", "value": date},
                    ],
                    "prompt": "Show my personal bests",
                }
            )
        else:
            items.append(
                {
                    "title": label,
                    "meta": "Not detected yet",
                    "description": "PBs can only be updated automatically from COROS activity details. They cannot be manually edited from web or chat.",
                    "state": "empty",
                    "prompt": "Show my personal bests",
                }
            )

    return {
        "key": "personal-bests",
        "title": "Personal bests",
        "description": "Read-only permanent memory; better results overwrite older PBs automatically.",
        "items": items,
    }


def _knowledge_category(path: Path, base: Path) -> str:
    """分类取自子目录名。直接放在 base 下的算默认类。"""
    try:
        parts = path.resolve().relative_to(base.resolve()).parts
    except ValueError:
        return "training"
    return parts[0] if len(parts) > 1 else "training"


def _video_header(path: Path) -> dict[str, str]:
    """读视频 md 头部的元数据。"""
    try:
        head = path.read_text(encoding="utf-8")[:600]
    except OSError:
        return {}
    fields: dict[str, str] = {}
    for key in ("Source", "Title", "Uploader", "UploaderId", "Imported at"):
        match = re.search(rf"^{re.escape(key)}:\s*(.+)$", head, re.M)
        if match:
            fields[key] = match.group(1).strip()
    return fields


CATEGORY_LABELS = {"shoes": "Shoe reviews", "training": "Training theory"}


def _subscription_progress() -> dict[str, dict[str, Any]]:
    """每个订阅源的回填进度：已导入多少 / 一共多少。

    订阅了但一条都没导入的 UP 主也要出现在页面上，否则刚订阅完看不到任何反馈，
    像是没生效。总数取自同步脚本的列表缓存，不额外请求 B 站。
    """
    from src.runtime.knowledge_sources import load_sources

    base = ROOT_DIR / "data" / "knowledge" / "coros-report"
    cache_dir = base / ".video-index"

    imported: dict[int, int] = {}
    for path in (base / "videos").rglob("*.md"):
        header = _video_header(path)
        try:
            uid = int(header.get("UploaderId", 0) or 0)
        except ValueError:
            continue
        imported[uid] = imported.get(uid, 0) + 1

    progress: dict[str, dict[str, Any]] = {}
    for source in load_sources():
        uid = int(source.get("uid", 0) or 0)
        total = 0
        cache_path = cache_dir / f"{uid}.json"
        if cache_path.exists():
            try:
                total = len(json.loads(cache_path.read_text(encoding="utf-8")).get("videos", []))
            except (OSError, json.JSONDecodeError):
                total = 0
        progress[str(uid)] = {
            "uid": uid,
            "name": source.get("name") or f"UID {uid}",
            "category": source.get("category", "training"),
            "imported": imported.get(uid, 0),
            "total": total,
        }
    return progress


def _knowledge_tree() -> list[dict[str, Any]]:
    """按 内容方向 → UP主/来源 → 单条资料 组织知识库。

    原来是一个平铺的卡片列表，二十多条视频堆在一起看不出结构。
    分类和 UP 主本来就是数据里已有的字段，只是没被用来组织展示。
    """
    base = ROOT_DIR / "data" / "knowledge" / "coros-report"
    buckets: dict[str, dict[str, list[dict[str, Any]]]] = {}
    progress = _subscription_progress()
    by_name = {p["name"]: p for p in progress.values()}

    for file_path in sorted((base / "books").rglob("*.pdf")):
        category = _knowledge_category(file_path, base / "books")
        buckets.setdefault(category, {}).setdefault("Books", []).append(
            {
                "title": file_path.stem,
                "meta": f"PDF · {_file_size(file_path)}",
                "prompt": f"Use {file_path.stem} to answer my training question",
            }
        )

    for file_path in sorted((base / "videos").rglob("*.md")):
        category = _knowledge_category(file_path, base / "videos")
        header = _video_header(file_path)
        uploader = header.get("Uploader") or "Unlabeled source"
        buckets.setdefault(category, {}).setdefault(uploader, []).append(
            {
                "title": header.get("Title") or file_path.stem,
                "meta": f"{header.get('Source', '')} · {_file_size(file_path)}",
                "imported_at": header.get("Imported at", ""),
                "prompt": f"Use {header.get('Title') or file_path.stem} to answer my question",
            }
        )

    # 订阅了但一条还没导入的来源也要占个位，显示「待同步」。
    for item in progress.values():
        buckets.setdefault(item["category"], {}).setdefault(item["name"], [])

    tree: list[dict[str, Any]] = []
    for category in sorted(buckets, key=lambda c: (c != "training", c)):
        groups = []
        for uploader in sorted(buckets[category]):
            items = sorted(
                buckets[category][uploader],
                key=lambda i: i.get("imported_at", ""),
                reverse=True,
            )
            info = by_name.get(uploader, {})
            total = int(info.get("total", 0) or 0)
            groups.append(
                {
                    "name": uploader,
                    "count": len(items),
                    "uid": info.get("uid", 0),
                    # 回填进度。总数是 0 说明还没抓过列表，这时不显示分母。
                    "progress": f"{len(items)}/{total}" if total else str(len(items)),
                    "pending": max(total - len(items), 0),
                    "items": items,
                }
            )
        # 有内容的排前面，待同步的排后面
        groups.sort(key=lambda g: (g["count"] == 0, g["name"]))
        tree.append(
            {
                "key": category,
                "label": CATEGORY_LABELS.get(category, category),
                "count": sum(g["count"] for g in groups),
                "groups": groups,
            }
        )
    return tree


def _video_title(path: Path) -> str:
    """优先用文件头里的 Title，退回文件名。

    文件名是 `BV14jbv6nE6d-【李宁京东200档跑鞋新手优选】` 这种，
    带着 BV 前缀不好看；头部的 Title 才是干净的标题。
    """
    try:
        head = path.read_text(encoding="utf-8")[:400]
    except OSError:
        return path.stem
    match = re.search(r"^Title:\s*(.+)$", head, re.M)
    if match and match.group(1).strip():
        return match.group(1).strip()
    return path.stem


def _data_photos_section() -> dict[str, Any]:
    records = _read_json(ROOT_DIR / "data" / "photo-memory" / "photos.json", [])
    media_root = ROOT_DIR / "data" / "media" / "photo-memory"
    items: list[dict[str, Any]] = []
    if isinstance(records, list):
        for record in records:
            if not isinstance(record, dict):
                continue
            files = record.get("files")
            image_urls = []
            if isinstance(files, list):
                for file_record in files:
                    if not isinstance(file_record, dict) or not file_record.get("path"):
                        continue
                    file_path = ROOT_DIR / str(file_record["path"])
                    try:
                        image_urls.append(
                            "/media/photo-memory/"
                            + quote(file_path.relative_to(media_root).as_posix(), safe="/")
                        )
                    except ValueError:
                        continue
            event = str(record.get("event") or "Untitled photos")
            race_date = str(record.get("race_date") or "Date missing")
            result = str(record.get("result") or "Result missing")
            items.append(
                {
                    "title": event,
                    "meta": f"{race_date} · {result} · {len(image_urls)} photos",
                    "description": str(record.get("notes") or "Photo memory uploaded and archived from Discord."),
                    "images": image_urls,
                    "facts": [
                        {"label": "Race date", "value": race_date},
                        {"label": "Result", "value": result},
                    ],
                    "prompt": f"Show photos for {event}",
                }
            )

    return {
        "key": "photos",
        "title": "Photo memory",
        "description": "Race photos, dates, results, and notes. Writes are only enabled in Discord.",
        "items": items,
    }


def _data_rag_section() -> dict[str, Any]:
    base = ROOT_DIR / "data" / "knowledge" / "coros-report"
    build_info = _read_json(base / "build_info.json", {})
    embeddings = _read_json(base / "embeddings.json", {})
    chunks_count = _json_count(base / "chunks.json")
    items: list[dict[str, Any]] = []

    # 资料条目改由 tree 承载（内容方向 → UP主 → 单条），
    # items 只保留索引和向量库这类整体信息。
    # 二十多条视频平铺成卡片是看不出结构的，而分类和 UP 主本来就在数据里。

    if isinstance(build_info, dict):
        config = build_info.get("config")
        chunk_size = "-"
        overlap = "-"
        if isinstance(config, dict):
            chunk_size = str(config.get("chunk_size") or "-")
            overlap = str(config.get("chunk_overlap") or "-")
        items.append(
            {
                "title": "RAG chunk index",
                "meta": f"{chunks_count} chunks · {build_info.get('built_at', 'Unknown build time')}",
                "description": "Stores chunks from books and videos for evidence retrieval.",
                "facts": [
                    {"label": "chunk_size", "value": chunk_size},
                    {"label": "overlap", "value": overlap},
                ],
                "prompt": "Explain what is inside my RAG knowledge base",
            }
        )

    if isinstance(embeddings, dict):
        items.append(
            {
                "title": "Embedding vector store",
                "meta": f"{embeddings.get('model', 'Unknown model')} · {embeddings.get('chunk_count', chunks_count)} parent chunks",
                "description": "Finds similar knowledge chunks with vectors before the LLM writes an answer with citations.",
                "facts": [
                    {"label": "child vectors", "value": str(embeddings.get("child_count") or "-")},
                    {"label": "model", "value": str(embeddings.get("model") or "-")},
                ],
                "prompt": "How does my RAG pipeline retrieve answers?",
            }
        )

    return {
        "key": "rag",
        "title": "RAG knowledge base",
        "description": "Running books, video subtitles, chunks, and embeddings.",
        "tree": _knowledge_tree(),
        "items": items,
    }


def _data_coros_archive_section(coros_memory: dict[str, Any]) -> dict[str, Any]:
    fit_files = sorted((ROOT_DIR / "data" / "coros-report" / "fit-files").glob("**/*.fit"))
    route_root = ROOT_DIR / "data" / "coros-report" / "route-maps"
    route_maps = sorted(route_root.glob("*.png"))
    items: list[dict[str, Any]] = []

    latest = coros_memory.get("latest_reported_activity")
    if isinstance(latest, dict):
        title = str(latest.get("name") or latest.get("sportType") or "Latest reported activity")
        distance = latest.get("distance")
        distance_text = f"{float(distance) / 1000:.2f} km" if isinstance(distance, (int, float)) else "-"
        items.append(
            {
                "title": title,
                "meta": f"{latest.get('startTime') or latest.get('date') or 'Unknown date'} · {distance_text}",
                "description": "Auto-reporting uses this record to decide whether the latest activity has already been sent.",
                "prompt": "Generate a report from my latest activity",
            }
        )

    items.append(
        {
            "title": "Raw FIT archive",
            "meta": f"{len(fit_files)} files · {_total_size(fit_files)}",
            "description": "Raw COROS activity files are synced locally and can be used later for routes, splits, and maps.",
            "facts": [
                {"label": "Latest file", "value": fit_files[-1].name if fit_files else "None yet"},
            ],
            "prompt": "List my COROS activities from the last 90 days",
        }
    )

    for file_path in route_maps:
        items.append(
            {
                "title": file_path.stem,
                "meta": f"Route map · {_file_size(file_path)}",
                "description": "Generated automatically for outdoor runs with GPS data.",
                "images": [
                    "/media/coros-route-maps/"
                    + quote(file_path.relative_to(route_root).as_posix(), safe="/")
                ],
                "prompt": "Show this outdoor running route",
            }
        )

    return {
        "key": "coros",
        "title": "COROS data",
        "description": "Activities, raw FIT files, and route map assets.",
        "items": items,
    }


def _web_auto_report_enabled() -> bool:
    return os.getenv("WEB_AUTO_REPORT_NOTICE_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _web_auto_report_demo_enabled() -> bool:
    return os.getenv("WEB_AUTO_REPORT_NOTICE_DEMO", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _auto_report_timeout_seconds() -> int:
    raw_value = os.getenv("WEB_AUTO_REPORT_NOTICE_TIMEOUT_SECONDS", "20")
    try:
        return max(int(raw_value), 5)
    except ValueError:
        return 20


def _demo_auto_report_notice() -> dict[str, Any]:
    return {
        "enabled": True,
        "pending": True,
        "activity": {
            "key": "demo-activity-2026-08-20",
            "title": "Completed 10.00 km · Indoor Run",
            "meta": "2026-08-20 · 1:18 · ready for AI review",
            "sport": "Indoor Run",
            "distance": "10.00 km",
            "duration": "1:18",
            "date": "2026-08-20",
            "prompt": "Generate a detailed report for my latest COROS workout. Use the Shadowrunner workout review style.",
        },
    }


async def _auto_report_notice_payload() -> dict[str, Any]:
    if not _web_auto_report_enabled():
        return {"enabled": False, "pending": False}

    if _web_agent_mode() != "real":
        if _web_auto_report_demo_enabled():
            return _demo_auto_report_notice()
        return {"enabled": True, "pending": False, "mode": "demo"}

    try:
        records = await asyncio.wait_for(
            recent_coros_activities(),
            timeout=_auto_report_timeout_seconds(),
        )
    except Exception as exc:
        return {
            "enabled": True,
            "pending": False,
            "error": str(exc) or exc.__class__.__name__,
        }

    if not records:
        return {"enabled": True, "pending": False}

    activity = records[0]
    summary = summarize_activity(activity)
    distance = summary.get("distance") or "distance unknown"
    sport = summary.get("type") or "Workout"
    date_text = summary.get("date") or "unknown date"
    duration = summary.get("duration") or "duration unknown"
    key = activity_key(activity)
    return {
        "enabled": True,
        "pending": True,
        "activity": {
            "key": key,
            "title": f"Completed {distance} · {sport}",
            "meta": f"{date_text} · {duration} · ready for AI review",
            "sport": sport,
            "distance": distance,
            "duration": duration,
            "date": date_text,
            "prompt": (
                "Generate a detailed report for my latest COROS workout. "
                "Use the Shadowrunner workout review style."
            ),
        },
    }


def _json_count(path: Path) -> int:
    data = _read_json(path, [])
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        if isinstance(data.get("items"), list):
            return len(data["items"])
        value = data.get("chunk_count")
        if isinstance(value, int):
            return value
    return 0


def _race_label(key: str) -> str:
    return {
        "1k": "1K",
        "3k": "3K",
        "5k": "5K",
        "10k": "10K",
        "half_marathon": "Half marathon",
        "marathon": "Marathon",
        "race": "Race",
    }.get(key, key)


def _file_size(path: Path) -> str:
    try:
        size = path.stat().st_size
    except OSError:
        return "Unknown size"
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def _total_size(paths: list[Path]) -> str:
    total = 0
    for path in paths:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    if total >= 1024 * 1024:
        return f"{total / 1024 / 1024:.1f} MB"
    if total >= 1024:
        return f"{total / 1024:.1f} KB"
    return f"{total} B"


class WebChannel:
    def __init__(
        self,
        emit: Callable[[dict[str, Any]], None],
        conversation_id: str = "web:default",
    ) -> None:
        self.id = -1
        self.conversation_id = conversation_id
        self._emit = emit
        self.messages: list[str] = []

    # 网页有独立的 status 事件承载进度，显示完就消失、不进对话记录，
    # 所以可以把工具级的进度也推过来。Discord 只能发真实消息，那边默认关。
    verbose_progress = True

    async def send(self, content: str) -> None:
        self.messages.append(content)
        await self._emit_message_stream(content)

    async def notify(self, content: str) -> None:
        """进度提示。走 status 事件，前端显示在「思考中」那一行，不落进对话。"""
        self._emit({"type": "status", "message": content})

    def show_images(self, urls: list[str], caption: str = "") -> None:
        """图片直发。不经过模型，所以不会被复述成一句「已经加载出来了」。"""
        self._emit({"type": "images", "urls": list(urls), "caption": caption})

    def trace_step(self, payload: dict[str, Any]) -> None:
        """把一次工具调用映射成的架构模块推给前端，用来在架构图上高亮。"""
        self._emit(payload)

    async def _emit_message_stream(self, content: str) -> None:
        self._emit({"type": "message_start"})
        for chunk in _text_stream_chunks(content):
            self._emit({"type": "message_delta", "delta": chunk})
            await asyncio.sleep(0.012)
        self._emit({"type": "message_end", "message": content})


def _text_stream_chunks(text: str, size: int = 72) -> list[str]:
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    current = ""
    for token in re.split(r"(\s+)", text):
        if len(current) + len(token) > size and current:
            chunks.append(current)
            current = token
        else:
            current += token
    if current:
        chunks.append(current)
    return chunks


def _ensure_agent_paths() -> None:
    paths = (
        ROOT_DIR,
        # 包化之后 agents 是正规包，只要仓库根在 sys.path 上就够了
    )
    for path in paths:
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)


def _web_agent_mode() -> str:
    return os.getenv("WEB_AGENT_MODE", "demo").strip().lower()


def _route_result_payload(route: object | None) -> dict[str, Any]:
    if route is None:
        return {
            "message": "",
            "capability": "Not routed",
            "confidence": 0.0,
            "tools": (),
            "graph_steps": (),
            "citations": (),
            "memory": (),
        }

    command_name = getattr(route, "command_name", "unknown")
    return {
        "message": "",
        "capability": _capability_label(str(command_name)),
        "confidence": float(getattr(route, "confidence", 1.0)),
        "tools": _tools_for_command(str(command_name)),
        "graph_steps": _steps_for_command(str(command_name)),
        "citations": (),
        "memory": (),
    }


def _capability_label(command_name: str) -> str:
    if command_name in {
        "coros",
        "coros-tools",
        "coros-list",
        "coros-activity",
        "coros-pb",
        "running",
        "running-video",
        "feel",
        "feelings",
    }:
        return "Running coach"
    return command_name


def _tools_for_command(command_name: str) -> tuple[str, ...]:
    if command_name == "running":
        return ("RAG retrieval", "Embedding", "Long-term memory", "DeepSeek")
    if command_name == "running-video":
        return ("Bilibili subtitle fetch", "Knowledge chunking", "Embedding")
    if command_name in {"coros-tools", "coros-list", "coros-activity", "coros-pb"}:
        return ("COROS MCP",)
    return ("COROS MCP", "LangGraph", "RAG retrieval", "Long-term memory")


def _steps_for_command(command_name: str) -> tuple[str, ...]:
    if command_name == "running":
        return ("Natural-language routing", "Update running profile", "Retrieve knowledge", "Generate training advice")
    if command_name == "running-video":
        return ("Natural-language routing", "Fetch subtitles", "Chunk knowledge", "Write knowledge base")
    if command_name == "coros-tools":
        return ("Natural-language routing", "Connect COROS MCP", "Read tool list")
    if command_name == "coros-list":
        return ("Natural-language routing", "Query activity summaries", "Cache selectable list")
    if command_name == "coros-activity":
        return ("Natural-language routing", "Read selected record", "Fetch details", "Generate report")
    if command_name == "coros-pb":
        return ("Natural-language routing", "Read PB memory", "Return read-only table")
    return ("Natural-language routing", "LangGraph workflow", "Read COROS data", "Generate report")


async def _stream_real_chat(
    prompt: str,
    emit: Callable[[dict[str, Any]], None],
    conversation_id: str = "web:default",
    lang: str = "en",
) -> None:
    _ensure_agent_paths()
    from src.orchestrator import get_orchestrator

    channel = WebChannel(emit, conversation_id)
    emit({"type": "status", "message": "Calling the real agent..."})
    task = asyncio.create_task(
        get_orchestrator().dispatch_web_text(
            None,
            channel,
            prompt,
            WEB_COMMANDS,
            lang,
        )
    )
    messages = (
        "Reading the required data...",
        "Waiting for tool results...",
        "Organizing context...",
        "Generating the answer...",
    )
    index = 0
    while not task.done():
        await asyncio.sleep(2.5)
        if not task.done():
            emit({"type": "status", "message": messages[index % len(messages)]})
            index += 1
    route = await task
    emit({"type": "trace", "result": _route_result_payload(route)})


async def _collect_real_chat(
    prompt: str,
    conversation_id: str = "web:default",
    lang: str = "en",
) -> dict[str, Any]:
    messages: list[str] = []
    saw_delta = False

    def emit(payload: dict[str, Any]) -> None:
        nonlocal saw_delta
        if payload.get("type") == "message":
            message = payload.get("message")
            if isinstance(message, str):
                messages.append(message)
        elif payload.get("type") == "message_delta":
            delta = payload.get("delta")
            if isinstance(delta, str):
                saw_delta = True
                messages.append(delta)

    _ensure_agent_paths()
    from src.orchestrator import get_orchestrator

    channel = WebChannel(emit, conversation_id)
    route = await get_orchestrator().dispatch_web_text(
        None,
        channel,
        prompt,
        WEB_COMMANDS,
        lang,
    )
    result = _route_result_payload(route)
    result["message"] = "".join(messages) if saw_delta else "\n\n".join(messages)
    return result


def run(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    load_dotenv(ROOT_DIR / ".env")
    _ensure_agent_paths()
    httpd = ThreadingHTTPServer((host, port), WebHandler)
    print(f"COROS Running Agent web console running at http://{host}:{port}", flush=True)
    httpd.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the COROS Running Agent web console.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    run(args.host, args.port)


if __name__ == "__main__":
    main()
