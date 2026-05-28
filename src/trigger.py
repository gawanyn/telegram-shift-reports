"""
Ручний запуск задач (для test_mode). Бот python -m src.main має бути зупинений.

  python -m src.trigger reminder   — нагадування в групи
  python -m src.trigger missing    — теги о 19:00-логіці
  python -m src.trigger dm           — особисті повідомлення
  python -m src.trigger all          — reminder → missing → dm
  python -m src.trigger reset-day    — скинути стан сьогодні (повторний тест)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .app import ShiftReportBot
from .env_setup import validate_env
from .schedule_utils import today_local

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


async def run_command(bot: ShiftReportBot, command: str) -> None:
    await bot.client.connect()
    if not await bot.client.is_user_authorized():
        print("Спочатку: python -m src.login", file=sys.stderr)
        sys.exit(1)

    await bot._resolve_groups()
    await bot._resolve_people_ids()

    day = today_local(bot.cfg)

    if command == "reset-day":
        bot.state.reset_day(day)
        print(f"Стан за {day.isoformat()} скинуто.")
        return

    if command in ("reminder", "all"):
        await bot.job_send_reminder()
    if command in ("missing", "all"):
        await bot.job_tag_missing()
    if command in ("dm", "all"):
        await bot.job_send_dm()

    print(f"Готово: {command}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ручний запуск задач userbot")
    parser.add_argument(
        "command",
        choices=["reminder", "missing", "dm", "all", "reset-day"],
        help="Що виконати",
    )
    args = parser.parse_args()
    validate_env()

    bot = ShiftReportBot()
    if bot.cfg.schedule_enabled:
        print("Увага: зупиніть src.main перед trigger (одна сесія).", file=sys.stderr)

    async def _run() -> None:
        try:
            await run_command(bot, args.command)
        finally:
            if bot.client.is_connected():
                await bot.client.disconnect()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
