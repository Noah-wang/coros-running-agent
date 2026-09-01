import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.runtime.atomic import stage, commit, discard, write_json_batch


ROOT_DIR = Path(__file__).resolve().parents[2]
STORE_DIR = Path(os.getenv("COROS_PROMPT_SKILLS_DIR", ROOT_DIR / "data" / "prompt-skills"))
INDEX_PATH = STORE_DIR / "index.json"
MAX_SKILL_BYTES = 64 * 1024
KINDS = {"coach", "sleep"}
_LOCK = threading.RLock()


@dataclass(frozen=True)
class PromptSkill:
    id: str
    name: str
    kind: str
    version: int
    description: str
    language: str
    source: str
    content: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "version": self.version,
            "description": self.description,
            "language": self.language,
            "source": self.source,
            "active": True,
        }

    def admin_dict(self) -> dict[str, Any]:
        return {**self.public_dict(), "content": self.content}


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", value.strip().lower()).strip("-")
    if not slug or len(slug) > 64:
        raise ValueError("Skill name must produce a 1-64 character lowercase slug.")
    return slug


def _kind(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in KINDS:
        raise ValueError("Skill type must be coach or sleep.")
    return normalized


def _metadata(markdown: str) -> tuple[dict[str, str], str]:
    if not markdown.startswith("---\n"):
        return {}, markdown.strip()
    end = markdown.find("\n---\n", 4)
    if end < 0:
        raise ValueError("Skill frontmatter is not closed.")
    metadata: dict[str, str] = {}
    for line in markdown[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip() in {"name", "type", "kind", "description", "language"}:
            metadata[key.strip()] = value.strip().strip("\"'")
    return metadata, markdown[end + 5 :].strip()


def _read_index() -> dict[str, Any]:
    if not INDEX_PATH.exists():
        return {"active": {}, "skills": {}}
    try:
        value = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"active": {}, "skills": {}}
    except (OSError, ValueError):
        return {"active": {}, "skills": {}}


def save_skill(kind: str, markdown: str, name: str = "") -> PromptSkill:
    skill_kind = _kind(kind)
    raw = markdown.strip()
    if not raw:
        raise ValueError("Skill Markdown is required.")
    if len(raw.encode("utf-8")) > MAX_SKILL_BYTES:
        raise ValueError("Skill Markdown must be 64 KB or smaller.")
    metadata, body = _metadata(raw)
    if not body:
        raise ValueError("Skill prompt body is required.")
    declared_kind = metadata.get("type") or metadata.get("kind")
    if declared_kind and _kind(declared_kind) != skill_kind:
        raise ValueError("Skill type does not match the selected slot.")
    display_name = (name or metadata.get("name") or f"custom-{skill_kind}").strip()
    skill_id = f"{skill_kind}:{_slug(display_name)}"
    with _LOCK:
        index = _read_index()
        records = index.setdefault("skills", {})
        previous = records.get(skill_id, {}) if isinstance(records, dict) else {}
        version = int(previous.get("version", 0)) + 1
        path = STORE_DIR / skill_kind / f"{skill_id.split(':', 1)[1]}.md"
        record = {
            "id": skill_id,
            "name": display_name,
            "kind": skill_kind,
            "version": version,
            "description": metadata.get("description", ""),
            "language": metadata.get("language", "auto"),
            "source": "custom",
            "path": str(path.relative_to(STORE_DIR)),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        records[skill_id] = record
        staged = [stage(path, raw), stage(INDEX_PATH, json.dumps(index, ensure_ascii=False, indent=2))]
        try:
            commit(staged)
        except BaseException:
            discard(staged)
            raise
    return PromptSkill(content=body, **{key: record[key] for key in ("id", "name", "kind", "version", "description", "language", "source")})


def activate_skill(kind: str, skill_id: str) -> None:
    skill_kind = _kind(kind)
    with _LOCK:
        index = _read_index()
        record = index.get("skills", {}).get(skill_id)
        if not isinstance(record, dict) or record.get("kind") != skill_kind:
            raise ValueError("Skill was not found in this slot.")
        index.setdefault("active", {})[skill_kind] = skill_id
        write_json_batch([(INDEX_PATH, index)])


def reset_skill(kind: str) -> None:
    skill_kind = _kind(kind)
    with _LOCK:
        index = _read_index()
        index.setdefault("active", {}).pop(skill_kind, None)
        write_json_batch([(INDEX_PATH, index)])


def _custom_active(kind: str) -> PromptSkill | None:
    skill_kind = _kind(kind)
    with _LOCK:
        index = _read_index()
        skill_id = index.get("active", {}).get(skill_kind)
        record = index.get("skills", {}).get(skill_id)
        if not isinstance(record, dict):
            return None
        path = (STORE_DIR / str(record.get("path", ""))).resolve()
        if not str(path).startswith(str(STORE_DIR.resolve())) or not path.is_file():
            return None
        _, body = _metadata(path.read_text(encoding="utf-8"))
    return PromptSkill(content=body, **{key: record.get(key, "") for key in ("id", "name", "kind", "version", "description", "language", "source")})


def active_skill(kind: str, default_name: str, default_content: str) -> PromptSkill:
    custom = _custom_active(kind)
    if custom is not None:
        return custom
    skill_kind = _kind(kind)
    return PromptSkill(
        id=f"{skill_kind}:built-in",
        name=default_name,
        kind=skill_kind,
        version=1,
        description="Built-in project prompt",
        language="auto",
        source="built-in",
        content=default_content,
    )


def list_skills(kind: str, default_name: str, default_content: str, include_content: bool = False) -> list[dict[str, Any]]:
    skill_kind = _kind(kind)
    active = active_skill(skill_kind, default_name, default_content)
    built_in = active_skill(skill_kind, default_name, default_content)
    if built_in.source != "built-in":
        built_in = PromptSkill(
            id=f"{skill_kind}:built-in", name=default_name, kind=skill_kind, version=1,
            description="Built-in project prompt", language="auto", source="built-in", content=default_content,
        )
    skills = [built_in]
    with _LOCK:
        index = _read_index()
        for record in index.get("skills", {}).values():
            if not isinstance(record, dict) or record.get("kind") != skill_kind:
                continue
            path = STORE_DIR / str(record.get("path", ""))
            if not path.is_file():
                continue
            _, body = _metadata(path.read_text(encoding="utf-8"))
            skills.append(PromptSkill(content=body, **{key: record.get(key, "") for key in ("id", "name", "kind", "version", "description", "language", "source")}))
    payload = []
    for skill in skills:
        item = skill.admin_dict() if include_content else skill.public_dict()
        item["active"] = skill.id == active.id
        payload.append(item)
    return payload
