"""Вхід у Telegram (QR за замовчуванням). Після успіху — python -m src.main"""

import argparse
import asyncio
import sys

# Дружнє завершення при таймауті QR (без traceback)

from telethon.errors import PhoneNumberInvalidError

from .env_setup import validate_env
from .telegram_auth import delete_session, login_interactive


def main() -> None:
    parser = argparse.ArgumentParser(description="Вхід у Telegram для userbot")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Видалити стару сесію (після невдалих спроб)",
    )
    args = parser.parse_args()
    validate_env()
    try:
        asyncio.run(login_interactive(reset=args.reset))
    except PhoneNumberInvalidError:
        print("Невірний TELEGRAM_PHONE у .env", file=sys.stderr)
        sys.exit(1)
    except asyncio.TimeoutError:
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nСкасовано.")
        sys.exit(0)


if __name__ == "__main__":
    main()
