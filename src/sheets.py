from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

from .parser import ParsedReport

ROOT = Path(__file__).resolve().parent.parent
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
HEADERS = [
    "Дата",
    "Час",
    "Група",
    "Співробітник",
    "Telegram ID",
    "Код відділення",
    "Назва відділення",
    "Виплачена пенсія",
    "Торгівля (грн)",
    "Передплата (шт)",
    "Текст звіту",
]


class SheetsWriter:
    def __init__(self) -> None:
        spreadsheet_id = os.environ.get("GOOGLE_SPREADSHEET_ID", "").strip()
        sheet_name = os.environ.get("GOOGLE_SHEET_NAME", "Звіти").strip()
        creds_path = os.environ.get(
            "GOOGLE_SERVICE_ACCOUNT_FILE",
            str(ROOT / "credentials" / "service_account.json"),
        )
        if not spreadsheet_id:
            raise RuntimeError("GOOGLE_SPREADSHEET_ID не задано в .env")
        if not Path(creds_path).is_file():
            raise RuntimeError(f"Файл service account не знайдено: {creds_path}")

        credentials = Credentials.from_service_account_file(creds_path, scopes=SCOPES)
        client = gspread.authorize(credentials)
        self._sheet = client.open_by_key(spreadsheet_id).worksheet(sheet_name)
        self._ensure_headers()

    def _ensure_headers(self) -> None:
        first = self._sheet.row_values(1)
        if not first:
            self._sheet.append_row(HEADERS, value_input_option="USER_ENTERED")

    def append_report(
        self,
        report_date: date,
        responded_at: datetime,
        group_title: str,
        display_name: str,
        telegram_id: int,
        parsed: ParsedReport,
        raw_text: str,
    ) -> None:
        self._sheet.append_row(
            [
                report_date.isoformat(),
                responded_at.strftime("%H:%M:%S"),
                group_title,
                display_name,
                str(telegram_id),
                parsed.get("id"),                 # Змінено: безпечно беремо ID
                parsed.get("location"),           # Змінено: назву села
                parsed.get("pension", 0.0),        # Змінено: суму пенсії
                parsed.get("trade", 0.0),          # Змінено: суму торгівлі
                parsed.get("subscription", 0.0),   # Змінено: кількість передплати
                raw_text,
            ],
            value_input_option="USER_ENTERED",
        )