"""出站文本的最后一道检查。

模型的输出会直接发给用户——Discord 消息、网页回答。在此之前没有任何检查。

这一层刻意只做**零误报**的事：把确实不该出现的字面量删掉，而不是猜测
「这句话像不像泄露」。基于猜测的过滤会误伤正常回答，而一个会误伤的
安全层最终会被关掉。

两类目标：

- **密钥**：环境变量里的真实值。注入攻击的一个典型目标就是诱导模型
  把它知道的配置说出来。这里做精确匹配，不匹配就一定不误伤。
- **内部标记**：`<untrusted-data>` 这类给模型看的边界标签。它们泄露到
  用户可见文本里不构成危险，但是个明确的信号——说明模型在照抄上下文，
  值得记一条事件。
"""

import os

from src.runtime.trace import log_event
from src.runtime.untrusted import CLOSE_TAG, OPEN_TAG

# 值得从输出里抹掉的环境变量。只列真正的凭据，
# 不列 URL、模型名这类本来就可以公开的配置。
SECRET_ENV_NAMES = (
    "LLM_API_KEY",
    "DEEPSEEK_API_KEY",
    "EMBEDDING_API_KEY",
    "DISCORD_BOT_TOKEN",
    "MAPBOX_ACCESS_TOKEN",
    "BILIBILI_SESSDATA",
)

# 太短的值不做匹配。一个 6 位的配置值可能正好是用户成绩里的数字，
# 抹掉它就是误伤——**宁可漏掉一个不像密钥的密钥，也不能改坏正常回答**。
MIN_SECRET_LENGTH = 12

REDACTED = "[已隐去]"


def _secrets() -> list[tuple[str, str]]:
    found = []
    for name in SECRET_ENV_NAMES:
        value = os.getenv(name, "")
        if value and len(value) >= MIN_SECRET_LENGTH:
            found.append((name, value))
    return found


def sanitize(text: str) -> str:
    """检查一段要发给用户的文本，必要时改写。"""
    if not text:
        return text

    cleaned = text
    leaked: list[str] = []

    for name, value in _secrets():
        if value in cleaned:
            cleaned = cleaned.replace(value, REDACTED)
            leaked.append(name)

    tags_stripped = False
    if OPEN_TAG in cleaned or CLOSE_TAG in cleaned:
        # 只删标签本身，保留里面的正文——那部分往往是用户真正想看的检索内容。
        for tag in (CLOSE_TAG,):
            cleaned = cleaned.replace(tag, "")
        cleaned = _strip_open_tags(cleaned)
        tags_stripped = True

    if leaked:
        log_event("output_secret_redacted", names=",".join(leaked))
    if tags_stripped:
        log_event("output_tag_stripped", chars=len(text))

    return cleaned


def _strip_open_tags(text: str) -> str:
    """删掉 `<untrusted-data ...>` 形式的开标签。"""
    result = []
    rest = text
    while True:
        start = rest.find(OPEN_TAG)
        if start == -1:
            result.append(rest)
            break
        end = rest.find(">", start)
        if end == -1:
            result.append(rest)
            break
        result.append(rest[:start])
        rest = rest[end + 1 :]
    return "".join(result)
