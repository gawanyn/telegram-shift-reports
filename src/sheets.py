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
        sheet_name = os.environ.get("GOOGLE_REPORTS_SHEET_NAME", "").strip()
        if not sheet_name:
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
        spreadsheet = client.open_by_key(spreadsheet_id)
        try:
            self._sheet = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            if not os.environ.get("GOOGLE_REPORTS_SHEET_NAME"):
                worksheets = spreadsheet.worksheets()
                if len(worksheets) == 1:
                    self._sheet = worksheets[0]
                else:
                    available = [worksheet.title for worksheet in worksheets]
                    raise RuntimeError(
                        f"Вкладка Google Sheets '{sheet_name}' не знайдено. "
                        f"Доступні вкладки: {available}"
                    )
            else:
                raise RuntimeError(
                    f"Вкладка Google Sheets '{sheet_name}' не знайдено. "
                    "Перевірте GOOGLE_REPORTS_SHEET_NAME або GOOGLE_SHEET_NAME."
                )
        self._ensure_headers()

    def _normalize_sheet_value(self, raw: str | None) -> str:
        return " ".join(str(raw or "").strip().lower().split())

    def _matches_group_name(self, row_group: str, group_title: str, group_id: str | None = None) -> bool:
        normalized_row_group = self._normalize_sheet_value(row_group)
        if not normalized_row_group:
            return False
        if normalized_row_group == self._normalize_sheet_value(group_title):
            return True
        if group_id and normalized_row_group == self._normalize_sheet_value(group_id):
            return True
        return False

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

    def load_daily_plans(self) -> dict[str, dict[str, float]]:
        """Зчитує плани по торгівлі та передплаті з вкладки 'ПЛАНИ' за допомогою gspread"""
        try:
            plans_sheet_name = os.environ.get("GOOGLE_PLANS_SHEET_NAME", "ПЛАНИ").strip()
            # Оскільки self._sheet — це вкладка зі звітами, ми через її властивість .spreadsheet
            # можемо легко переключитися на вкладку з планами.
            plans_sheet = self._sheet.spreadsheet.worksheet(plans_sheet_name)
            
            # Витягуємо всі матричні дані з цієї вкладки (включаючи шапку)
            all_values = plans_sheet.get_all_values()
            if not all_values or len(all_values) < 2:
                return {}

            plans = {}
            # Пропускаємо перший рядок (шапку) і йдемо по кожному рядку
            for row in all_values[1:]:
                # Перевіряємо, щоб рядок мав хоча б 4 стовпчики і перший стовпчик (Код) не був порожнім
                if len(row) >= 4 and row[0].strip():
                    branch_code = str(row[0]).strip()
                    try:
                        # Конвертуємо дані, замінюючи коми на крапки (для Катерин Федорівн, якщо вони впишуть 100,5)
                        trade_plan = float(str(row[2]).strip().replace(",", "."))
                        sub_plan = float(str(row[3]).strip().replace(",", "."))
                        
                        plans[branch_code] = {
                            "trade": trade_plan,
                            "prepayment": sub_plan
                        }
                    except ValueError:
                        continue
            return plans
        except Exception as e:
            print(f"[ERROR] Не вдалося завантажити плани з вкладки ПЛАНИ: {e}")
            return {}

    def load_processed_reports(
        self, report_date: date, group_title: str, group_id: str | None = None
    ) -> list[tuple[int, str, str, str]]:
        """Завантажує вже записані звіти з аркуша для поточної дати і групи."""
        try:
            all_values = self._sheet.get_all_values()
            if not all_values or len(all_values) < 2:
                return []

            processed: list[tuple[int, str, str, str]] = []
            target_date = report_date.isoformat()
            for row in all_values[1:]:
                if len(row) < 11:
                    continue
                row_date = row[0].strip()
                row_time = row[1].strip() if len(row) > 1 else ""
                row_group = row[2].strip()
                row_telegram_id = row[4].strip()
                row_text = self._normalize_sheet_value(row[10])

                if row_date != target_date:
                    continue
                if not self._matches_group_name(row_group, group_title, group_id):
                    continue
                try:
                    telegram_id = int(row_telegram_id)
                except ValueError:
                    continue
                processed.append((telegram_id, row_text, row_date, row_time))
            return processed
        except Exception as e:
            print(f"[ERROR] Не вдалося завантажити оброблені звіти з Google Sheets: {e}")
            return []