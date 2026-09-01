import importlib
import os
import tempfile
import unittest
from pathlib import Path


class PromptSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        os.environ["COROS_PROMPT_SKILLS_DIR"] = str(Path(self.temp.name) / "skills")
        import src.runtime.prompt_skills as module
        self.skills = importlib.reload(module)

    def tearDown(self) -> None:
        os.environ.pop("COROS_PROMPT_SKILLS_DIR", None)
        self.temp.cleanup()

    def test_save_activate_and_reset(self) -> None:
        skill = self.skills.save_skill(
            "coach",
            "---\nname: Steady Coach\ntype: coach\n---\nUse concise evidence.",
        )
        self.skills.activate_skill("coach", skill.id)
        active = self.skills.active_skill("coach", "Default", "Default prompt")
        self.assertEqual(active.name, "Steady Coach")
        self.assertEqual(active.content, "Use concise evidence.")
        self.skills.reset_skill("coach")
        self.assertEqual(
            self.skills.active_skill("coach", "Default", "Default prompt").source,
            "built-in",
        )

    def test_rejects_slot_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            self.skills.save_skill("sleep", "---\ntype: coach\n---\nPrompt")


if __name__ == "__main__":
    unittest.main()
