from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.runtime.tools import Tool


SendText = Callable[[str], Awaitable[None]]
CommandHandler = Callable[["CommandContext", str], Awaitable[None]]
StartupHandler = Callable[[Any], None]
AttachmentSaver = Callable[[Path], Awaitable[None]]


IMAGE_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"}
)


@dataclass(frozen=True)
class RuntimeAttachment:
    filename: str
    content_type: str | None
    url: str
    size: int
    save: AttachmentSaver

    @property
    def is_image(self) -> bool:
        """是不是图片附件。"""
        if self.content_type and self.content_type.startswith("image/"):
            return True
        return Path(self.filename).suffix.lower() in IMAGE_SUFFIXES


@dataclass(frozen=True)
class CommandContext:
    client: Any
    channel: Any
    send: SendText
    send_chunks: SendText
    message: Any | None = None
    conversation_id: str = "default"
    # 公开的 Web 入口没有认证，只允许读。写操作（改长期记忆、记录感受、
    # 导入知识库）必须在能力层再拒一次，不能只依赖命令白名单。
    read_only: bool = False
    attachments: tuple[RuntimeAttachment, ...] = ()

    # 进度提示的出口。和 send 分开，是因为主 Agent 循环把命令包装成工具之后，
    # send 的内容会被收进缓冲区当作工具返回值交给模型——「正在读取 COROS 数据…」
    # 这类话本来是说给人听的，被吞掉之后用户会对着几十秒的沉默干等。
    #
    # None 表示这个入口没有独立的进度通道，退回 send。
    notify: SendText | None = None
    # 是否把工具级的进度（模型为什么调这个工具）也推给用户。
    # 网页有独立的 status 事件可以承载，显示完就消失；
    # Discord 只能发真实消息，一次问答冒出三四条会很吵，所以默认关。
    verbose_progress: bool = False

    # 图片的直发通道。和 send 分开的理由比 notify 更强：
    # 主 Agent 把命令包装成工具后，send 的内容是**喂给模型的**。
    # 17 张图的 markdown 走这条路，等于让模型逐个复述 URL——
    # 又费 token 又会漏，实测模型直接改成一句「已经全部加载出来了」，
    # 用户一张图都看不到。
    #
    # **图片是给人看的数据，不该经过模型。** None 表示这个入口不支持，
    # 能力层照常退回把图链接写进文本。
    show_images: Any | None = None

    async def progress(self, text: str) -> None:
        """发一条进度提示。它不会进入工具返回值。"""
        if not text:
            return
        await (self.notify or self.send)(text)

    def images(self, urls: list[str], caption: str = "") -> bool:
        """把图片直接推给用户，绕过模型。不支持时返回 False。"""
        if not urls or not callable(self.show_images):
            return False
        self.show_images(urls, caption)
        return True


@dataclass(frozen=True)
class TextCommand:
    name: str
    description: str
    handler: CommandHandler
    aliases: tuple[str, ...] = ()

    # 这条命令会不会改变状态。主 Agent 的循环按它决定在只读入口暴露哪些工具——
    # 权限挂在命令自己身上，而不是散落在调用方的 if 里。
    writes: bool = False
    # 这条命令的输出里含有第三方能控制的文本（书籍原文、视频字幕）。
    # 包装成工具后，它一旦执行过，本轮就不再允许写操作。
    returns_untrusted: bool = False
    # writes=True 但这条命令自己会在只读入口拒掉写操作，所以仍然可以暴露。
    read_only_safe: bool = False
    # 只读入口下改用这段描述。
    #
    # read_only_safe 的命令天生要在一段话里同时讲清读和写，结果是**读被写淹没**：
    # photo 的描述原来以「保存、追加、补充信息」开头、末尾带一句
    # 「这些只在 Discord 生效」，只读入口下模型把那句限制读成了整个工具的限制，
    # 于是明知道该调 photo 也不调，改口说「我没有显示图片的能力」。
    # 留空则两种入口共用 description。
    read_only_description: str = ""
    # 参数格式说明，给模型看。留空就只用 description。
    argument_hint: str = ""
    # 有些命令是运维用的手动触发，不该让模型自己去调。
    expose_as_tool: bool = True

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


@dataclass(frozen=True)
class Capability:
    name: str
    description: str
    channel_env_name: str | None = None
    text_commands: tuple[TextCommand, ...] = ()
    startup_handlers: tuple[StartupHandler, ...] = field(default_factory=tuple)
    # 能力主动交给主 Agent 的只读工具，用于开放式提问。
    #
    # 主 Agent 需要跨来源回答「我最近训练状态怎么样」这种问题，但它不应该
    # 直接 import 能力内部的存储——那样 src/ 就反向依赖 agents/ 了。
    # 所以反过来：能力自己决定愿意暴露什么，主 Agent 只负责收集和调用。
    #
    # 只读是硬约束。这些工具在没有认证的公开 Web 入口上也会被调用，
    # 任何写操作都必须留在各自的命令里，不能出现在这里。
    read_tools: tuple[Tool, ...] = ()
