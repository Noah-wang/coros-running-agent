"""命令行查看知识库分块质量。

实际检查逻辑在 src/runtime/knowledge_health.py，那里同时被 Agent 的
inspect_knowledge_index 工具复用，保证人看到的和 Agent 看到的是同一份数据。

用法：uv run python scripts/inspect_chunks.py [来源关键字]
"""

import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.runtime.knowledge_health import format_report, inspect_knowledge_index


def main() -> None:
    source = sys.argv[1] if len(sys.argv) > 1 else None
    report = inspect_knowledge_index(source)
    print(format_report(report))
    if "error" in report:
        sys.exit(1)


if __name__ == "__main__":
    main()
