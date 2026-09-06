"""Discord 入口的频道闸门。

真实事故：bot 在一个跟跑步无关的频道里回答了跑步问题。原因不是路由判错，
而是 `_dispatch_text_inner` 末尾的主 Agent 循环**没有任何频道判断**——
前面每个分支都各自查了频道，最后那个兜底分支忘了查，于是它接管一切。

所以这里测的不是「某条路径挡住了」，而是「陌生频道一个字都不说」，
并且**在主 Agent 循环开着的情况下测**（那是线上默认值）。
"""

import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("COROS_RUNTIME_SETTINGS_PATH", tempfile.mktemp())

from src.orchestrator import MainAgentOrchestrator  # noqa: E402
from src.registry import CapabilityRegistry  # noqa: E402
from src.runtime.capability import Capability  # noqa: E402
from src.runtime.tools import Tool  # noqa: E402

PLACEHOLDER_TOOL = Tool(
    name="list_recent_activities",
    description="占位只读工具",
    parameters={"type": "object", "properties": {}},
    handler=lambda: None,
)

# 用一个占位能力，绑到跑步频道——空注册表里没有任何频道，
# 「报告帖能不能用能力」这条根本测不出来。
RUNNING_CAPABILITY = Capability(
    name="coros-report",
    description="占位",
    channel_env_name="DISCORD_RUNNING_CHANNEL_ID",
    read_tools=(PLACEHOLDER_TOOL,),
)

RUNNING = "1537316749622386718"
FORUM = "1544914627283124236"
STRANGER = 1111111111111111111

ENV = {
    "DISCORD_RUNNING_CHANNEL_ID": RUNNING,
    "DISCORD_AGENT_CHANNEL_ID": RUNNING,
    "DISCORD_REPORT_FORUM_CHANNEL_ID": FORUM,
    "MAIN_AGENT_LOOP_ENABLED": "true",
}


class _Channel:
    def __init__(self, channel_id: int, parent_id: int | None = None) -> None:
        self.id = channel_id
        self.parent_id = parent_id
        self.messages: list[str] = []

    async def send(self, content: str) -> None:
        self.messages.append(content)


class ChannelGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.orchestrator = MainAgentOrchestrator(CapabilityRegistry([]))

    def test_stranger_channel_is_not_allowed(self) -> None:
        with patch.dict(os.environ, ENV, clear=False):
            self.assertTrue(self.orchestrator.is_discord_channel_allowed(int(RUNNING)))
            self.assertFalse(self.orchestrator.is_discord_channel_allowed(STRANGER))

    def test_forum_is_allowed_even_though_no_capability_owns_it(self) -> None:
        """论坛不在 channel_env_names() 里——它是发帖目标，不是能力入口。"""
        with patch.dict(os.environ, ENV, clear=False):
            self.assertTrue(self.orchestrator.is_discord_channel_allowed(int(FORUM)))

    def test_forum_thread_is_allowed_via_parent(self) -> None:
        """报告帖的 channel.id 是帖子的 id，不是论坛的 id。只看 id 会把追问挡掉。"""
        with patch.dict(os.environ, ENV, clear=False):
            self.assertTrue(
                self.orchestrator.is_discord_channel_allowed(999, parent_id=int(FORUM))
            )
            self.assertFalse(
                self.orchestrator.is_discord_channel_allowed(999, parent_id=STRANGER)
            )

    async def test_main_agent_loop_does_not_answer_in_stranger_channel(self) -> None:
        """这条是真正的回归测试：闸门去掉后，兜底的主 Agent 循环会答这句话。"""
        channel = _Channel(STRANGER)
        with patch.dict(os.environ, ENV, clear=False):
            handled = await self.orchestrator.dispatch_text(object(), channel, "今天跑了多远")
        self.assertFalse(handled)
        self.assertEqual(channel.messages, [])

    async def test_bang_command_in_stranger_channel_is_silent(self) -> None:
        channel = _Channel(STRANGER)
        with patch.dict(os.environ, ENV, clear=False):
            handled = await self.orchestrator.dispatch_text(
                object(), channel, "!capabilities"
            )
        self.assertFalse(handled)
        self.assertEqual(channel.messages, [])


class ForumThreadPermissionTests(unittest.IsolatedAsyncioTestCase):
    """报告帖里必须真的能用能力，不能只是「放进来了但没工具」。

    只补 is_discord_channel_allowed 的话，消息进得来、权限表查不到，
    bot 会回一句「这个入口没有可用的能力」——比直接沉默更像坏了。
    """

    def setUp(self) -> None:
        self.orchestrator = MainAgentOrchestrator(
            CapabilityRegistry([RUNNING_CAPABILITY])
        )

    def test_forum_thread_resolves_to_main_channel(self) -> None:
        with patch.dict(os.environ, ENV, clear=False):
            thread = _Channel(999, parent_id=int(FORUM))
            self.assertEqual(
                self.orchestrator.permission_channel_id_for(thread), int(RUNNING)
            )

    def test_ordinary_channel_keeps_its_own_id(self) -> None:
        with patch.dict(os.environ, ENV, clear=False):
            plain = _Channel(int(RUNNING))
            self.assertEqual(
                self.orchestrator.permission_channel_id_for(plain), int(RUNNING)
            )
            stranger = _Channel(STRANGER, parent_id=STRANGER)
            self.assertEqual(
                self.orchestrator.permission_channel_id_for(stranger), STRANGER
            )

    def test_forum_thread_gets_the_main_channel_capabilities(self) -> None:
        """真正要保证的东西：报告帖里 coros 命令是可用的。"""
        with patch.dict(os.environ, ENV, clear=False):
            thread = _Channel(999, parent_id=int(FORUM))
            allow_id = self.orchestrator.permission_channel_id_for(thread)
            self.assertTrue(self.orchestrator.is_capabilities_channel(allow_id))

    def test_forum_thread_tool_table_is_not_empty(self) -> None:
        """线上真实症状：工具表查空 → bot 回「这个入口没有可用的能力」。"""
        with patch.dict(os.environ, ENV, clear=False):
            thread = _Channel(999, parent_id=int(FORUM))
            self.assertTrue(self.orchestrator._loop_tools(object(), thread))

    def test_stranger_thread_tool_table_stays_empty(self) -> None:
        with patch.dict(os.environ, ENV, clear=False):
            stranger = _Channel(999, parent_id=STRANGER)
            self.assertFalse(self.orchestrator._loop_tools(object(), stranger))


if __name__ == "__main__":
    unittest.main()
