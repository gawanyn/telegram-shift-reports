from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

# Україна: +380 та 9 цифр (наприклад +380681234567)
UA_PHONE_RE = re.compile(r"^\+380\d{9}$")

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"

REQUIRED = (
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_PHONE",
    "WORK_GROUP_STATIONARY_ID",
    "WORK_GROUP_MOBILE_ID",
)


def load_env() -> None:
    load_dotenv(ENV_PATH, override=True)


def normalize_phone(raw: str) -> str:
    phone = raw.strip().strip('"').strip("'")
    phone = phone.replace("\r", "").replace("\n", "")
    phone = phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if phone.startswith("00"):
        phone = "+" + phone[2:]
    elif phone.startswith("380"):
        phone = "+" + phone
    elif phone.startswith("0") and phone.isdigit() and len(phone) == 10:
        # 0686054603 → +380686054603
        phone = "+38" + phone
    elif phone.isdigit() and len(phone) == 9:
        phone = "+380" + phone
    return phone


def validate_phone(raw: str) -> str:
    phone = normalize_phone(raw)
    if not UA_PHONE_RE.match(phone):
        print(
            "Невірний TELEGRAM_PHONE. Потрібен формат міжнародний, без пробілів:\n"
            "  +380681234567\n"
            "або локальний:\n"
            "  0681234567\n"
            f"Зараз у .env (після нормалізації): {phone!r}",
            file=sys.stderr,
        )
        sys.exit(1)
    return phone


def telegram_phone() -> str:
    return validate_phone(os.environ["TELEGRAM_PHONE"])


def validate_env() -> None:
    load_env()
    missing = [key for key in REQUIRED if not os.environ.get(key, "").strip()]
    if not ENV_PATH.is_file():
        hint = (
            f"Файл .env не знайдено в {ROOT}\n"
            f"Створіть його з прикладу:\n"
            f'  copy "{ENV_EXAMPLE}" "{ENV_PATH}"\n'
            f"Потім заповніть TELEGRAM_API_ID, TELEGRAM_API_HASH та інші поля."
        )
        print(hint, file=sys.stderr)
        sys.exit(1)
    if missing:
        print(
            f"У .env не заповнено: {', '.join(missing)}\n"
            f"Відредагуйте {ENV_PATH}",
            file=sys.stderr,
        )
        sys.exit(1)
    validate_phone(os.environ["TELEGRAM_PHONE"])


def telegram_api_id() -> int:
    return int(os.environ["TELEGRAM_API_ID"])


def telegram_api_hash() -> str:
    return os.environ["TELEGRAM_API_HASH"]
