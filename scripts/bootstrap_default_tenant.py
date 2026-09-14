"""Create the backward-compatible owner tenant for an existing installation."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from dotenv import load_dotenv

from src.runtime.control_store import get_control_store


def main() -> None:
    load_dotenv(ROOT_DIR / ".env")
    tenant = get_control_store().ensure_default_tenant(os.getenv("AGENT_OWNER_NAME", "Owner"))
    print(f"Control plane ready: {tenant['id']} ({tenant['name']})")


if __name__ == "__main__":
    main()
