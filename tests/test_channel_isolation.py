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

RUNNING = "1537316749622386718"
FORUM = "1544914627283124236"
STRANGER = 1111111111111111111

ENV = {
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


if __name__ == "__main__":
    unittest.main()
