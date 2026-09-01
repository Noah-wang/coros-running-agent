import os
import threading
from pathlib import Path
from typing import Any

from src.runtime.atomic import write_json_batch


ROOT_DIR = Path(__file__).resolve().parents[2]
SETTINGS_PATH = Path(
    os.getenv("COROS_RUNTIME_SETTINGS_PATH", ROOT_DIR / "data" / "runtime-settings.json")
)
_LOCK = threading.RLock()
_AUTOMATIONS = {
    "auto_report": ("COROS_AUTO_REPORT_ENABLED", False),
    "sleep_report": ("COROS_SLEEP_REPORT_ENABLED", True),
}


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("Value must be true or false.")


def _read() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        import json

        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def automation_enabled(name: str) -> bool:
    if name not in _AUTOMATIONS:
        raise ValueError(f"Unknown automation: {name}")
    env_name, default = _AUTOMATIONS[name]
    with _LOCK:
        configured = _read().get("automations", {}).get(name)
    if isinstance(configured, bool):
        return configured
    try:
        return parse_bool(os.getenv(env_name, str(default)))
    except ValueError:
        return default


def set_automation_enabled(name: str, enabled: Any) -> bool:
    if name not in _AUTOMATIONS:
        raise ValueError(f"Unknown automation: {name}")
    parsed = parse_bool(enabled)
    with _LOCK:
        data = _read()
        automations = data.setdefault("automations", {})
        if not isinstance(automations, dict):
            automations = {}
            data["automations"] = automations
        automations[name] = parsed
        write_json_batch([(SETTINGS_PATH, data)])
    return parsed


def automation_payload() -> dict[str, bool]:
    return {name: automation_enabled(name) for name in _AUTOMATIONS}
