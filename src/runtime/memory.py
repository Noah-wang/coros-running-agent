import json
from copy import deepcopy
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
MEMORY_PATH = ROOT_DIR / "data" / "memory.json"

DEFAULT_MEMORY: dict[str, Any] = {
    "global": {
        "name": "Runner",
        "language": "zh",
        "preferences": [],
    },
    "agents": {},
    "caches": {},
}

# 曾经被当成"记忆"存进 agents 里的临时缓存。
#
# last_activity_list 是「分析第 N 条」用的选择缓存，一份 20 条运动记录。
# 它跟长期记忆毫无关系，却因为躺在同一个字典里，被 format_memory_for_prompt
# 全量塞进每一次跑步问答的提示词——实测占了 10790 字符里的 5785 字符，
# 一半以上，每次提问白付约 2900 token。
#
# 缓存和记忆的区别不是大小，是**该不该进提示词**。所以这里给缓存单独一个命名空间。
LEGACY_CACHE_KEYS = (
    "last_activity_list",
    "last_activity_list_query",
    "last_activity_list_label",
    "last_activity_list_updated_at",
)


def load_memory() -> dict[str, Any]:
    if not MEMORY_PATH.exists():
        save_memory(deepcopy(DEFAULT_MEMORY))

    with MEMORY_PATH.open("r", encoding="utf-8") as file:
        memory = json.load(file)

    if _migrate_legacy_caches(memory):
        save_memory(memory)
    return memory


def _migrate_legacy_caches(memory: dict[str, Any]) -> bool:
    """把历史遗留在 agents 里的缓存搬进 caches。返回是否发生了改动。

    自愈式迁移：每次加载都检查一遍，搬完就不再命中。没有单独的迁移脚本，
    因为线上和本地的数据不同步，一次性脚本很容易漏跑其中一边。
    """
    agents = memory.get("agents")
    if not isinstance(agents, dict):
        return False

    caches = memory.setdefault("caches", {})
    if not isinstance(caches, dict):
        caches = {}
        memory["caches"] = caches

    changed = False
    for agent_name, agent_memory in agents.items():
        if not isinstance(agent_memory, dict):
            continue
        for key in LEGACY_CACHE_KEYS:
            if key in agent_memory:
                caches.setdefault(agent_name, {})[key] = agent_memory.pop(key)
                changed = True
    return changed


def save_memory(memory: dict[str, Any]) -> None:
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MEMORY_PATH.open("w", encoding="utf-8") as file:
        json.dump(memory, file, ensure_ascii=False, indent=2)
        file.write("\n")


def get_global_memory() -> dict[str, Any]:
    memory = load_memory()
    global_memory = memory.setdefault("global", {})
    if not isinstance(global_memory, dict):
        return {}
    return global_memory


def get_agent_memory(agent_name: str) -> dict[str, Any]:
    memory = load_memory()
    agents = memory.setdefault("agents", {})
    if not isinstance(agents, dict):
        return {}
    agent_memory = agents.setdefault(agent_name, {})
    if not isinstance(agent_memory, dict):
        return {}
    return agent_memory


def update_agent_memory(agent_name: str, patch: dict[str, Any]) -> dict[str, Any]:
    memory = load_memory()
    agents = memory.setdefault("agents", {})
    if not isinstance(agents, dict):
        agents = {}
        memory["agents"] = agents

    agent_memory = agents.setdefault(agent_name, {})
    if not isinstance(agent_memory, dict):
        agent_memory = {}
        agents[agent_name] = agent_memory

    agent_memory.update(patch)
    save_memory(memory)
    return agent_memory


def get_agent_cache(agent_name: str) -> dict[str, Any]:
    """读取某个能力的临时缓存。缓存不会进提示词。"""
    memory = load_memory()
    caches = memory.setdefault("caches", {})
    if not isinstance(caches, dict):
        return {}
    cache = caches.setdefault(agent_name, {})
    return cache if isinstance(cache, dict) else {}


def update_agent_cache(agent_name: str, patch: dict[str, Any]) -> dict[str, Any]:
    """写入某个能力的临时缓存。"""
    memory = load_memory()
    caches = memory.setdefault("caches", {})
    if not isinstance(caches, dict):
        caches = {}
        memory["caches"] = caches

    cache = caches.setdefault(agent_name, {})
    if not isinstance(cache, dict):
        cache = {}
        caches[agent_name] = cache

    cache.update(patch)
    save_memory(memory)
    return cache


def format_memory_for_prompt(agent_name: str) -> str:
    """只输出真正的长期记忆。caches 不在这里，它们不该占提示词的位置。"""
    memory = {
        "global": get_global_memory(),
        agent_name: get_agent_memory(agent_name),
    }
    return json.dumps(memory, ensure_ascii=False, indent=2)
