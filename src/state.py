from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "state.db"


class StateStore:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate_schema()
        self._init_schema()

    def _migrate_schema(self) -> None:
        """Стара схема без group_id — перестворюємо таблиці."""
        row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='person_day'"
        ).fetchone()
        if not row:
            return
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(person_day)").fetchall()]
        if "group_id" in cols:
            return
        self._conn.executescript(
            """
            DROP TABLE IF EXISTS person_day;
            DROP TABLE IF EXISTS day_meta;
            """
        )
        self._conn.commit()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS person_day (
                report_date TEXT NOT NULL,
                group_id TEXT NOT NULL,
                telegram_id INTEGER NOT NULL,
                responded INTEGER NOT NULL DEFAULT 0,
                responded_at TEXT,
                dm_sent INTEGER NOT NULL DEFAULT 0,
                dm_cancelled INTEGER NOT NULL DEFAULT 0,
                parsed_json TEXT,
                PRIMARY KEY (report_date, group_id, telegram_id)
            );
            CREATE TABLE IF NOT EXISTS day_meta (
                report_date TEXT NOT NULL,
                group_id TEXT NOT NULL,
                reminder_sent INTEGER NOT NULL DEFAULT 0,
                missing_tag_sent INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (report_date, group_id)
            );
            """
        )
        self._conn.commit()

    def ensure_day(self, report_date: date, group_id: str, telegram_ids: list[int]) -> None:
        key = report_date.isoformat()
        self._conn.execute(
            "INSERT OR IGNORE INTO day_meta (report_date, group_id) VALUES (?, ?)",
            (key, group_id),
        )
        for tid in telegram_ids:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO person_day (report_date, group_id, telegram_id)
                VALUES (?, ?, ?)
                """,
                (key, group_id, tid),
            )
        self._conn.commit()

    def mark_reminder_sent(self, report_date: date, group_id: str) -> None:
        key = report_date.isoformat()
        self._conn.execute(
            """
            INSERT INTO day_meta (report_date, group_id, reminder_sent)
            VALUES (?, ?, 1)
            ON CONFLICT(report_date, group_id) DO UPDATE SET reminder_sent = 1
            """,
            (key, group_id),
        )
        self._conn.commit()

    def reminder_sent(self, report_date: date, group_id: str) -> bool:
        row = self._conn.execute(
            "SELECT reminder_sent FROM day_meta WHERE report_date = ? AND group_id = ?",
            (report_date.isoformat(), group_id),
        ).fetchone()
        return bool(row and row["reminder_sent"])

    def mark_missing_tag_sent(self, report_date: date, group_id: str) -> None:
        key = report_date.isoformat()
        self._conn.execute(
            """
            INSERT INTO day_meta (report_date, group_id, missing_tag_sent)
            VALUES (?, ?, 1)
            ON CONFLICT(report_date, group_id) DO UPDATE SET missing_tag_sent = 1
            """,
            (key, group_id),
        )
        self._conn.commit()

    def missing_tag_sent(self, report_date: date, group_id: str) -> bool:
        row = self._conn.execute(
            "SELECT missing_tag_sent FROM day_meta WHERE report_date = ? AND group_id = ?",
            (report_date.isoformat(), group_id),
        ).fetchone()
        return bool(row and row["missing_tag_sent"])

    def mark_response(
        self,
        report_date: date,
        group_id: str,
        telegram_id: int,
        parsed: dict,
        responded_at: datetime,
    ) -> None:
        key = report_date.isoformat()
        self._conn.execute(
            """
            UPDATE person_day
            SET responded = 1,
                responded_at = ?,
                parsed_json = ?,
                dm_cancelled = 1
            WHERE report_date = ? AND group_id = ? AND telegram_id = ?
            """,
            (
                responded_at.isoformat(),
                json.dumps(parsed, ensure_ascii=False),
                key,
                group_id,
                telegram_id,
            ),
        )
        self._conn.commit()

    def mark_dm_sent(self, report_date: date, group_id: str, telegram_id: int) -> None:
        self._conn.execute(
            """
            UPDATE person_day
            SET dm_sent = 1
            WHERE report_date = ? AND group_id = ? AND telegram_id = ?
            """,
            (report_date.isoformat(), group_id, telegram_id),
        )
        self._conn.commit()

    def pending_dm(self, report_date: date, group_id: str) -> list[int]:
        rows = self._conn.execute(
            """
            SELECT telegram_id FROM person_day
            WHERE report_date = ? AND group_id = ?
              AND responded = 0
              AND dm_sent = 0
              AND dm_cancelled = 0
            """,
            (report_date.isoformat(), group_id),
        ).fetchall()
        return [int(r["telegram_id"]) for r in rows]

    def missing_responses(self, report_date: date, group_id: str) -> list[int]:
        rows = self._conn.execute(
            """
            SELECT telegram_id FROM person_day
            WHERE report_date = ? AND group_id = ? AND responded = 0
            """,
            (report_date.isoformat(), group_id),
        ).fetchall()
        return [int(r["telegram_id"]) for r in rows]

    def reset_day(self, report_date: date, group_id: str | None = None) -> None:
        key = report_date.isoformat()
        if group_id:
            self._conn.execute(
                "DELETE FROM person_day WHERE report_date = ? AND group_id = ?",
                (key, group_id),
            )
            self._conn.execute(
                "DELETE FROM day_meta WHERE report_date = ? AND group_id = ?",
                (key, group_id),
            )
        else:
            self._conn.execute(
                "DELETE FROM person_day WHERE report_date = ?",
                (key,),
            )
            self._conn.execute(
                "DELETE FROM day_meta WHERE report_date = ?",
                (key,),
            )
        self._conn.commit()

    def has_responded(self, report_date: date, group_id: str, telegram_id: int) -> bool:
        row = self._conn.execute(
            """
            SELECT responded FROM person_day
            WHERE report_date = ? AND group_id = ? AND telegram_id = ?
            """,
            (report_date.isoformat(), group_id, telegram_id),
        ).fetchone()
        return bool(row and row["responded"])
