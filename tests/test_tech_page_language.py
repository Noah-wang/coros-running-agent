"""技术页的正文要跟着语言走。

线上表现：语言切到中文，标签变成「系统架构」，**正文还是英文**。
原因是文档路径写死成了 `.en.md`——翻译表管得了标题，管不了
从文件里读出来的整篇正文。

而且两份不是互译：`.md` 是完整中文（架构 15K、RAG 36K），
`.en.md` 是英文精简节选（3.8K / 2.7K）。切中文能多看到几倍内容。
"""

import os
import tempfile

os.environ.setdefault("COROS_RUNTIME_SETTINGS_PATH", tempfile.mktemp())

from src.api.web_server import _doc_path, _tech_payload  # noqa: E402


def _first_body(lang: str, tab: int = 0) -> str:
    items = _tech_payload(lang)["tabs"][tab]["items"]
    assert items, f"lang={lang} 的第 {tab} 个标签没有内容"
    return items[0]["title"] + items[0]["body"]


def test_chinese_picks_the_chinese_file():
    assert _doc_path("ARCHITECTURE", "zh").name == "ARCHITECTURE.md"
    assert _doc_path("rag-pipeline", "zh").name == "rag-pipeline.md"


def test_english_picks_the_english_file():
    assert _doc_path("ARCHITECTURE", "en").name == "ARCHITECTURE.en.md"
    assert _doc_path("rag-pipeline", "en").name == "rag-pipeline.en.md"


def test_missing_chinese_file_falls_back_to_english():
    """中文版不存在时退回英文，而不是给一个空页面。"""
    assert _doc_path("does-not-exist", "zh").name == "does-not-exist.en.md"


def test_body_actually_differs_by_language():
    """**这条是重点。** 标题早就翻译了，坏的是正文。"""
    assert _first_body("zh") != _first_body("en")


def test_chinese_body_contains_chinese():
    body = _first_body("zh")
    assert any("一" <= ch <= "鿿" for ch in body), "中文页正文里一个汉字都没有"


def test_rag_tab_also_switches():
    """RAG 那个标签是另一条路径（_rag_items），别只修一半。"""
    assert _first_body("zh", tab=1) != _first_body("en", tab=1)
