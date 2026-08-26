import importlib

from src.runtime.capability import Capability, CommandContext, TextCommand
from src.runtime.paths import ROOT_DIR
from src.runtime.tools import Tool


class CapabilityRegistry:
    def __init__(self, capabilities: list[Capability]) -> None:
        """初始化功能注册表。

        解析传入的 capabilities 列表，并为底下的文本指令建立映射，
        以便快速查找指令对应的 Command 实例与所属 Capability 实例。

        Args:
            capabilities: 已加载的功能（Capability）列表。
        """
        self._capabilities = capabilities
        self._commands: dict[str, TextCommand] = {}
        self._command_capabilities: dict[str, Capability] = {}
        for capability in capabilities:
            for command in capability.text_commands:
                for name in command.names:
                    self._commands[name] = command
                    self._command_capabilities[name] = capability

    def describe(self) -> str:
        """生成并返回所有已加载功能及其命令别名的说明文档文本。"""
        lines = ["当前已加载的 capabilities："]
        for capability in self._capabilities:
            lines.append(f"- {capability.name}: {capability.description}")
            for command in capability.text_commands:
                aliases = f" aliases: {', '.join(command.aliases)}" if command.aliases else ""
                lines.append(f"  !{command.name} - {command.description}{aliases}")
        return "\n".join(lines)

    def run_startup_handlers(self, client: object) -> None:
        """在系统启动时，依次执行每个功能模块注册的启动处理器。

        Args:
            client: Discord 客户端实例。
        """
        for capability in self._capabilities:
            for handler in capability.startup_handlers:
                handler(client)

    def channel_env_for_command(self, command_name: str) -> str | None:
        """获取特定指令所对应的专用 Discord 频道的环境变量配置名称。

        Args:
            command_name: 指令名称。
        """
        capability = self._command_capabilities.get(command_name)
        if capability is None:
            return None
        return capability.channel_env_name

    def read_tools(self) -> tuple[tuple[str | None, Tool], ...]:
        """所有能力交上来的只读工具，附带各自的频道环境变量名。

        返回 (channel_env_name, tool) 而不是直接返回工具，是因为频道隔离的
        判断在主 Agent 手里——注册表不知道当前是哪个频道，也不该知道。
        """
        pairs: list[tuple[str | None, Tool]] = []
        for capability in self._capabilities:
            for tool in capability.read_tools:
                pairs.append((capability.channel_env_name, tool))
        return tuple(pairs)

    def tool_commands(self) -> tuple[tuple[str | None, TextCommand], ...]:
        """愿意暴露给主 Agent 循环的命令，附带各自的频道环境变量名。

        和 read_tools 一样返回频道信息而不是直接过滤：注册表不知道当前是哪个频道。
        """
        pairs: list[tuple[str | None, TextCommand]] = []
        for capability in self._capabilities:
            for command in capability.text_commands:
                if command.expose_as_tool:
                    pairs.append((capability.channel_env_name, command))
        return tuple(pairs)

    def channel_env_names(self) -> tuple[str, ...]:
        """获取所有已加载能力中定义过的专属频道环境变量名列表（已去重）。"""
        names: list[str] = []
        for capability in self._capabilities:
            if capability.channel_env_name is not None:
                names.append(capability.channel_env_name)
        return tuple(dict.fromkeys(names))

    async def dispatch_command(
        self, context: CommandContext, command_name: str, argument: str = ""
    ) -> bool:
        """根据指令名称，将指令与参数分发给具体对应的命令处理器异步执行。

        Args:
            context: 执行指令所需的上下文（包含 client、channel 以及发送消息的方法）。
            command_name: 待执行的指令名称。
            argument: 指令的附加参数（默认为空）。
        """
        command = self._commands.get(command_name)
        if command is None:
            return False
        await command.handler(context, argument)
        return True

    async def dispatch_text(self, context: CommandContext, content: str) -> bool:
        """解析单条以 `!` 开头的指令文本，并自动分发执行。

        Args:
            context: 指令上下文。
            content: 原始指令文本（如 `!coros-list 最近 90 天`）。
        """
        stripped = content.strip()
        if not stripped.startswith("!"):
            return False

        raw_command = stripped[1:]
        command_name, _, argument = raw_command.partition(" ")
        return await self.dispatch_command(context, command_name, argument.strip())


_registry: CapabilityRegistry | None = None

AGENTS_DIR = ROOT_DIR / "agents"


def discover_capabilities() -> list[Capability]:
    """扫描 `agents/*/xxx_capability.py`，把每个 `build_*_capability()` 的结果收进来。

    原来这里是三行写死的 import。写死的代价是**「装了哪些能力」变成了代码事实**：
    想只跑跑步那一套，得改 registry；抽一个开源版出来，得改 registry；
    删掉一个能力目录会直接 ImportError。

    改成扫描之后，**目录在 = 能力在**，加减能力都不用碰这个文件。
    顺序按目录名排，保证 `describe()` 和评测里的工具表是稳定的。
    """
    capabilities: list[Capability] = []
    for module_path in sorted(AGENTS_DIR.glob("*/*_capability.py")):
        module_name = f"agents.{module_path.parent.name}.{module_path.stem}"
        module = importlib.import_module(module_name)
        builders = sorted(
            name
            for name in dir(module)
            if name.startswith("build_") and name.endswith("_capability")
        )
        for name in builders:
            capabilities.append(getattr(module, name)())
    return capabilities


def get_registry() -> CapabilityRegistry:
    """获取单例的功能注册表，能力由 `agents/` 下的目录自动发现。"""
    global _registry
    if _registry is None:
        _registry = CapabilityRegistry(discover_capabilities())
    return _registry
