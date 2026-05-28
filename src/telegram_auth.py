from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path

import qrcode
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from .env_setup import ROOT, telegram_api_hash, telegram_api_id, telegram_phone

SESSION_PATH = ROOT / "shift_reports.session"
SESSION_NAME = str(ROOT / "shift_reports")
QR_IMAGE_PATH = ROOT / "login_qr.png"


def session_files() -> list[Path]:
    return [
        SESSION_PATH,
        Path(f"{SESSION_NAME}.session"),
        ROOT / "shift_reports.session-journal",
        QR_IMAGE_PATH,
    ]


def delete_session() -> None:
    for path in session_files():
        if path.is_file():
            path.unlink()
            print(f"Видалено: {path}")


def create_client() -> TelegramClient:
    return TelegramClient(SESSION_NAME, telegram_api_id(), telegram_api_hash())


def _render_qr(url: str) -> None:
    """QR у терміналі + PNG (відкриється у переглядачі на Windows)."""
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)

    try:
        img = qrcode.make(url)
        img.save(QR_IMAGE_PATH)
        print(f"\nЗображення QR: {QR_IMAGE_PATH}")
        if sys.platform == "win32":
            try:
                os.startfile(QR_IMAGE_PATH)
                print("(Відкрито у переглядачі — відскануйте з телефону.)")
            except OSError:
                print("Відкрийте файл login_qr.png вручну.")
    except (ModuleNotFoundError, ImportError):
        print(
            "\nPNG не створено (немає Pillow). Скануйте QR з терміналу вище\n"
            "або: pip install Pillow\n"
        )
    print()


def _seconds_until(expires: datetime) -> float:
    now = datetime.now(timezone.utc)
    exp = expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc)
    return max((exp - now).total_seconds() - 3, 15)


async def require_authorized(client: TelegramClient) -> None:
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Telegram: {me.first_name or me.username or me.id}")
        return
    print(
        "\nСесія Telegram відсутня.\n"
        "Увійдіть:  python -m src.login --reset\n",
        file=sys.stderr,
    )
    sys.exit(1)


async def login_interactive(*, reset: bool = False) -> None:
    if reset:
        delete_session()

    client = create_client()
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Вже увійшли як {me.first_name or me.username or me.id}")
        await client.disconnect()
        return

    mode = _login_mode()
    try:
        if mode == "qr":
            await _login_via_qr(client)
        else:
            await _login_via_app_code(client)
    finally:
        await client.disconnect()


def _login_mode() -> str:
    raw = os.getenv("TELEGRAM_LOGIN_MODE", "qr").strip().lower()
    if raw in ("phone", "code", "app"):
        return "phone"
    return "qr"


async def _login_via_qr(client: TelegramClient) -> None:
    print()
    print("=" * 60)
    print("  ВХІД ЧЕРЕЗ QR")
    print("=" * 60)
    print("  Телефон: Telegram → Налаштування → Пристрої →")
    print("           → Підключити пристрій → сканувати QR")
    print()
    print("  Не закривайте цей термінал, поки скануєте!")
    print("  Посилання tg:// у Windows НЕ працює — лише скан QR.")
    print("=" * 60)
    print()

    qr = await client.qr_login()
    attempt = 0
    max_attempts = 5

    while attempt < max_attempts:
        attempt += 1
        secs = _seconds_until(qr.expires)
        print(f"--- QR #{attempt} (дійсний ~{int(secs)} сек) ---\n")
        _render_qr(qr.url)
        print("Скануйте ЗАРАЗ. Термінал чекає...\n")

        try:
            await qr.wait(timeout=secs)
            break
        except asyncio.TimeoutError:
            print("QR прострочено.\n")
            if attempt >= max_attempts:
                print(
                    "Не вдалося увійти через QR.\n"
                    "Спробуйте:  set TELEGRAM_LOGIN_MODE=phone\n"
                    "           python -m src.login --reset\n",
                    file=sys.stderr,
                )
                raise
            print("Генерую новий QR...\n")
            await qr.recreate()
        except SessionPasswordNeededError:
            password = getpass("2FA: пароль Telegram: ")
            await client.sign_in(password=password)
            break
    else:
        raise RuntimeError("QR login failed")

    me = await client.get_me()
    print(f"\nГотово: {me.first_name or ''} (@{me.username or '—'})")
    print(f"Сесія: {SESSION_PATH}\n")
    if QR_IMAGE_PATH.is_file():
        QR_IMAGE_PATH.unlink(missing_ok=True)


async def _login_via_app_code(client: TelegramClient) -> None:
    phone = telegram_phone()
    print()
    print("=" * 60)
    print("  ВХІД КОДОМ (у додатку Telegram, не SMS)")
    print("=" * 60)
    print(f"  Номер: {phone}")
    print("  1. Відкрийте Telegram на телефоні з цим акаунтом")
    print("  2. Чат «Telegram» (вгорі списку) — код входу")
    print("  3. Або сповіщення «Новий вхід» / «Login code»")
    print("=" * 60)
    print()

    try:
        result = await client.send_code_request(phone, force_sms=False)
    except FloodWaitError as exc:
        print(f"Зачекайте {exc.seconds} сек. (захист Telegram).", file=sys.stderr)
        raise

    delivery = type(result.type).__name__
    print(f"Код надіслано способом: {delivery}")
    if "App" in delivery:
        print("→ Шукайте код у додатку Telegram, не в SMS.\n")
    else:
        print(f"→ Тип доставки: {delivery}\n")

    while True:
        code = input("Введіть код (Enter = скасувати): ").strip()
        if not code:
            print("Скасовано.")
            return
        code = code.replace(" ", "").replace("-", "")
        try:
            await client.sign_in(phone=phone, code=code)
            break
        except PhoneCodeInvalidError:
            print("Невірний код. Перевірте нове повідомлення в Telegram.")
        except PhoneCodeExpiredError:
            print("Код прострочено. Надсилаю новий...")
            result = await client.send_code_request(phone, force_sms=False)
            print(f"Новий запит: {type(result.type).__name__}")
        except SessionPasswordNeededError:
            password = getpass("2FA пароль: ")
            await client.sign_in(password=password)
            break

    me = await client.get_me()
    print(f"\nГотово: {me.first_name or ''} (@{me.username or '—'})")
    print(f"Сесія: {SESSION_PATH}\n")
