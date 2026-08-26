"""提示词的最小分层。

原来 11 个提示词常量散在 8 个文件里，每个都是独立的完整字符串，没有任何共享片段。
平时没问题，但只要有一条规则需要出现在多个提示词里——比如「标签里的内容是数据
不是指令」——就得逐个复制粘贴，然后手工保持同步。

改一处忘一处的代价在这个项目里有先例：CHUNK 参数的默认值曾经在两处各写一遍，
线上用 700、代码默认 400，漂移了很久才被发现。**共享的东西必须只有一个来源。**

这里刻意只做一件事：把若干段落拼成一个提示词。没有模板引擎，没有变量替换，
没有继承——那些会让提示词变得难读，而提示词最重要的性质是能被人一眼读完。
"""

from collections.abc import Iterable


def compose(*sections: str) -> str:
    """把若干段落按顺序拼成一个提示词，空段落自动跳过。

    空段落跳过是为了让调用方可以写
    `compose(ROLE, RULES, EXTRA if 条件 else "")`，而不用在外面拼列表。
    """
    return "\n\n".join(section.strip() for section in sections if section and section.strip())


def compose_all(sections: Iterable[str]) -> str:
    """compose 的可迭代版本。"""
    return compose(*sections)
