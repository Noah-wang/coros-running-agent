import importlib
import os
import tempfile
import unittest
from pathlib import Path


class RuntimeSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        os.environ["COROS_RUNTIME_SETTINGS_PATH"] = str(Path(self.temp.name) / "settings.json")
        import src.runtime.runtime_settings as module
        self.settings = importlib.reload(module)

    def tearDown(self) -> None:
        os.environ.pop("COROS_RUNTIME_SETTINGS_PATH", None)
        self.temp.cleanup()

    def test_persisted_override_wins_over_environment(self) -> None:
        os.environ["COROS_AUTO_REPORT_ENABLED"] = "false"
        self.settings.set_automation_enabled("auto_report", "true")
        self.assertTrue(self.settings.automation_enabled("auto_report"))

    def test_rejects_ambiguous_boolean(self) -> None:
        with self.assertRaises(ValueError):
            self.settings.set_automation_enabled("sleep_report", "maybe")


if __name__ == "__main__":
    unittest.main()
