import asyncio
import logging
import sys

from .app import ShiftReportBot
from .env_setup import validate_env


def main() -> None:
    validate_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    bot = ShiftReportBot()
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
