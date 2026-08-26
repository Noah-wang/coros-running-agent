import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from src.bot.discord_bot import run_discord_bot


def main() -> None:
	load_dotenv(ROOT_DIR / ".env")
	run_discord_bot()


if __name__ == "__main__":
	main()
