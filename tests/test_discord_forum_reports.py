import unittest
from datetime import date

from agents.coros_report.auto_report import _activity_title
from agents.coros_report.sleep_report import _sleep_duration_text, _sleep_title


class DiscordForumReportTests(unittest.TestCase):
    def test_activity_title_uses_date_and_sport(self) -> None:
        self.assertEqual(
            _activity_title(
                {"date": "2026-09-03", "sportName": "Indoor Run", "distanceKm": 10.0}
            ),
            "2026-09-03 跑步 10KM",
        )

    def test_activity_title_preserves_meaningful_distance_decimals(self) -> None:
        self.assertEqual(
            _activity_title(
                {"date": "2026-09-03", "sportName": "Run", "distanceKm": 8.01}
            ),
            "2026-09-03 跑步 8.01KM",
        )

    def test_sleep_title_uses_duration_from_coros_text(self) -> None:
        results = [
            {
                "result": {
                    "content": [
                        {"type": "text", "text": "Total Sleep: 7h 26min"}
                    ]
                }
            }
        ]
        self.assertEqual(_sleep_duration_text(results), "7小时26分")
        self.assertEqual(
            _sleep_title(date(2026, 9, 3), results, ""),
            "2026-09-03 睡眠 7小时26分",
        )

    def test_sleep_duration_can_fall_back_to_report(self) -> None:
        self.assertEqual(_sleep_duration_text([], "睡眠时长：6小时8分钟"), "6小时08分")
        self.assertEqual(
            _sleep_duration_text([], "- **睡眠总量**：**6h 57min**，睡眠评分 97"),
            "6小时57分",
        )


if __name__ == "__main__":
    unittest.main()
