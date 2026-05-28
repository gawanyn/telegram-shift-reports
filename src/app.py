from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import date, datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telethon import TelegramClient, events
from telethon.errors import UsernameNotOccupiedError
from telethon.tl.types import User

from .config_loader import AppConfig, ChatGroup, Person, load_config
from .env_setup import telegram_api_hash, telegram_api_id
from .telegram_auth import SESSION_NAME, require_authorized
from .parser import ParsedReport, parse_report
from .schedule_utils import expected_people_today, parse_hhmm, today_local
from .sheets import SheetsWriter
from .state import StateStore

logger = logging.getLogger(__name__)


class ShiftReportBot:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.tz = ZoneInfo(self.cfg.timezone)
        self.state = StateStore()
        self.client = TelegramClient(
            SESSION_NAME,
            telegram_api_id(),
            telegram_api_hash(),
        )
        self._chat_to_group: dict[int, str] = {}
        self._person_by_group_user: dict[tuple[str, int], Person] = {}
        self._sheets: SheetsWriter | None = None
        self._scheduler = AsyncIOScheduler(timezone=self.tz)
        self._followup_tasks: dict[tuple[str, str, int, str], asyncio.Task] = {}
        self._my_id: int | None = None

    async def start(self) -> None:
        await require_authorized(self.client)
        me = await self.client.get_me()
        self._my_id = me.id
        await self._resolve_groups()
        await self._resolve_people_ids()
        try:
            self._sheets = SheetsWriter()
        except Exception as exc:
            logger.warning("Google Sheets вимкнено: %s", exc)
            self._sheets = None

        self._register_handlers()
        self._register_scheduler()
        self._scheduler.start()
        logger.info(
            "Userbot запущено. Чати: %s",
            ", ".join(f"{g.id}={g.chat_id}" for g in self.cfg.groups if g.chat_id),
        )
        if not self.cfg.schedule_enabled:
            logger.info(
                "Команди у робочому чаті (з вашого акаунта): "
                "!нагадування  !хто  !лс  !скинути  !допомога"
            )
        await self.client.run_until_disconnected()

    async def _resolve_groups(self) -> None:
        for group in self.cfg.groups:
            if group.chat_id is None:
                raise RuntimeError(
                    f"Не задано {group.env_var} у .env для групи «{group.title}»"
                )
            chat_id = group.chat_id
            if isinstance(chat_id, str):
                entity = await self.client.get_entity(chat_id)
                chat_id = entity.id
                group.chat_id = chat_id
            self._chat_to_group[int(chat_id)] = group.id
            normalized = self._normalize_chat_id(int(chat_id))
            if normalized != int(chat_id):
                self._chat_to_group[normalized] = group.id
            logger.info("Чат «%s» → %s", group.title, chat_id)

    async def _resolve_people_ids(self) -> None:
        for person in self.cfg.people:
            if person.telegram_id:
                self._register_person(person)
                logger.info(
                    "Зареєстровано %s [%s] id=%s",
                    person.display_name,
                    person.group,
                    person.telegram_id,
                )
                continue
            if not person.username:
                logger.warning(
                    "Пропущено %s [%s]: немає telegram_id і username",
                    person.display_name,
                    person.group,
                )
                continue
            try:
                entity = await self.client.get_entity(person.username)
            except (ValueError, UsernameNotOccupiedError):
                logger.warning(
                    "Пропущено %s [%s]: username @%s не існує",
                    person.display_name,
                    person.group,
                    person.username,
                )
                continue
            if isinstance(entity, User) and entity.id:
                person.telegram_id = entity.id
                self._register_person(person)
                logger.info("ID %s [%s] → %s", person.display_name, person.group, entity.id)

    def _register_person(self, person: Person) -> None:
        if person.telegram_id is None:
            return
        self._person_by_group_user[(person.group, person.telegram_id)] = person

    def _normalize_chat_id(self, chat_id: int) -> int:
        raw = str(chat_id)
        if raw.startswith("-100"):
            return int("-" + raw[4:])
        return chat_id

    def _group_for_chat(self, chat_id: int) -> str | None:
        return self._chat_to_group.get(chat_id)

    def _chat_ids_for_matching(self, chat_id: int) -> list[int]:
        ids = [chat_id]
        normalized = self._normalize_chat_id(chat_id)
        if normalized != chat_id:
            ids.append(normalized)
        return ids

    def _register_handlers(self) -> None:
        chat_ids: list[int] = []
        for group in self.cfg.groups:
            if group.chat_id is not None:
                chat_ids.extend(self._chat_ids_for_matching(group.chat_id))
        chat_ids = sorted(set(chat_ids))
        if chat_ids:
            logger.info("Слухаємо повідомлення в чатах: %s", chat_ids)
        else:
            logger.warning(
                "Немає chat_id для прослуховування. Перевірте .env, WORK_GROUP_STATIONARY_ID"
            )

        @self.client.on(events.NewMessage(chats=chat_ids))
        async def on_group_message(event: events.NewMessage.Event) -> None:
            await self._handle_group_message(event)

    def _register_scheduler(self) -> None:
        if not self.cfg.schedule_enabled:
            return

        rh, rm = parse_hhmm(self.cfg.reminder_time)
        mh, mm = parse_hhmm(self.cfg.missing_check_time)
        dh, dm = parse_hhmm(self.cfg.dm_time)

        self._scheduler.add_job(
            self.job_send_reminder,
            CronTrigger(hour=rh, minute=rm, timezone=self.tz),
            id="reminder",
        )
        self._scheduler.add_job(
            self.job_tag_missing,
            CronTrigger(hour=mh, minute=mm, timezone=self.tz),
            id="missing_tag",
        )
        self._scheduler.add_job(
            self.job_send_dm,
            CronTrigger(hour=dh, minute=dm, timezone=self.tz),
            id="dm_missing",
        )

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    async def _init_today_state(self, group_id: str) -> tuple[date, list[Person]]:
        day = today_local(self.cfg, self._now())
        expected = expected_people_today(self.cfg, group_id, day)
        ids = [p.telegram_id for p in expected if p.telegram_id]
        self.state.ensure_day(day, group_id, ids)
        return day, expected

    async def job_send_reminder(self) -> None:
        for group in self.cfg.groups:
            await self._job_send_reminder_for_group(group)

    async def _job_send_reminder_for_group(self, group: ChatGroup) -> None:
        day, expected = await self._init_today_state(group.id)
        if not expected:
            logger.info("[%s] %s: вихідний день, звітів не чекаємо", group.id, day)
            return
        if self.state.reminder_sent(day, group.id):
            return
        text = self.cfg.message_for_group(group.id, "reminder")
        await self.client.send_message(group.chat_id, text)
        self.state.mark_reminder_sent(day, group.id)
        logger.info("Нагадування [%s] за %s", group.id, day)

    async def job_tag_missing(self) -> None:
        for group in self.cfg.groups:
            await self._job_tag_missing_for_group(group)

    async def _job_tag_missing_for_group(self, group: ChatGroup) -> None:
        day = today_local(self.cfg, self._now())
        
        # 1. Беремо ВСІХ людей цієї групи прямо з конфігу people.yaml
        all_people = [p for p in self.cfg.people if p.group == group.id]
        
        missing_ids = []
        for person in all_people:
            if not person.telegram_id:
                continue
            # 2. Перевіряємо, чи є відмітка про зданий звіт у базі
            if not self.state.has_responded(day, group.id, person.telegram_id):
                missing_ids.append(person.telegram_id)

        # 3. Якщо боржників реально немає
        if not missing_ids:
            await self.client.send_message(
                group.chat_id, 
                "✅ Всі відділення групи здали звіти! Дякую."
            )
            return

        # 4. Якщо боржники є — тегаємо їх красиво
        mentions = self._format_mentions(group.id, missing_ids)
        template = self.cfg.message_for_group(group.id, "missing_in_group")
        
        await self.client.send_message(
            group.chat_id,
            template.format(mentions=mentions),
            parse_mode="md"
        )

    async def job_send_dm(self) -> None:
        for group in self.cfg.groups:
            await self._job_send_dm_for_group(group)

    async def _job_send_dm_for_group(self, group: ChatGroup) -> None:
        day, _ = await self._init_today_state(group.id)
        text = self.cfg.message_for_group(group.id, "dm_missing")
        for tid in self.state.pending_dm(day, group.id):
            try:
                await self.client.send_message(tid, text)
                self.state.mark_dm_sent(day, group.id, tid)
            except Exception as exc:
                logger.error("DM [%s] не надіслано %s: %s", group.id, tid, exc)

    async def _try_admin_command(
        self, event: events.NewMessage.Event, group_id: str
    ) -> bool:
        raw = (event.message.message or "").strip()
        if not raw.startswith("!"):
            return False

        cmd = raw.split()[0].lower()
        group = self.cfg.group_by_id(group_id)
        if not group:
            return False

        handlers = {
            "!нагадування": lambda: self._job_send_reminder_for_group(group),
            "!reminder": lambda: self._job_send_reminder_for_group(group),
            "!хто": lambda: self._job_tag_missing_for_group(group),
            "!missing": lambda: self._job_tag_missing_for_group(group),
            "!лс": lambda: self._job_send_dm_for_group(group),
            "!dm": lambda: self._job_send_dm_for_group(group),
            "!скинути": lambda: self._cmd_reset_day(group_id),
            "!reset": lambda: self._cmd_reset_day(group_id),
        }
        if cmd in ("!допомога", "!help"):
            await event.reply(
                "Команди:\n"
                "!нагадування — нагадування в чат\n"
                "!хто — теги без звіту\n"
                "!лс — особисті повідомлення\n"
                "!скинути — скинути стан на сьогодні"
            )
            return True

        action = handlers.get(cmd)
        if not action:
            return False

        logger.info("Команда %s у [%s]", cmd, group_id)
        
        # === ЗАХИСТ ВІД КРИВИХ ID: підставляємо реальний ID чату ===
        if group and event.chat_id:
            group.chat_id = event.chat_id
        # ==========================================================
        
        result = action()
        if asyncio.iscoroutine(result):
            await result
        return True

    async def _cmd_reset_day(self, group_id: str) -> None:
        day = today_local(self.cfg, self._now())
        self.state.reset_day(day, group_id)
        logger.info("Скинуто стан [%s] за %s", group_id, day)

    async def _handle_group_message(self, event: events.NewMessage.Event) -> None:
        chat_id = event.chat_id
        if chat_id is None:
            return
        group_id = self._group_for_chat(chat_id)
        if not group_id:
            return

        sender = await event.get_sender()
        sender_id = getattr(sender, "id", None)
        sender_name = (
            getattr(sender, "first_name", None)
            or getattr(sender, "username", None)
            or str(sender_id)
        )
        raw = (event.message.message or "").strip()
        logger.info(
            "Повідомлення [%s] від %s id=%s: %s",
            group_id,
            sender_name,
            sender_id,
            raw or "<порожнє повідомлення>",
        )

        if (
            isinstance(sender, User)
            and self._my_id
            and sender.id == self._my_id
            and await self._try_admin_command(event, group_id)
        ):
            
            try:
                await event.delete()
            except Exception as e:
                logger.warning(f"Не вдалося видалити команду: {e}")

            return
        if not isinstance(sender, User) or sender.bot:
            return

        person = self._person_by_group_user.get((group_id, sender.id))
        if not person:
            logger.info(
                "Ігнор id=%s у [%s]: немає в config/people.yaml",
                sender.id,
                group_id,
            )
            return

        day = today_local(self.cfg, self._now())
        expected = expected_people_today(self.cfg, group_id, day)
        if person not in expected:
            logger.info(
                "Ігнор %s [%s]: сьогодні не робочий день відділення",
                person.display_name,
                group_id,
            )
            return

        if person.telegram_id:
            self.state.ensure_day(day, group_id, [person.telegram_id])

        raw = (event.message.message or "").strip()
        if not raw:
            return

        parsed = parse_report(raw)

        if parsed is None:
            logger.warning(
                "Не розпізнано звіт від %s [%s]: %s",
                person.display_name,
                group_id,
                raw[:80],
            )
            if not self.state.has_responded(day, group_id, sender.id):
                err = self.cfg.message_for_group(group_id, "parse_error_reply")
                if err:
                    await event.reply(err)
            return

        now = self._now()
        already = self.state.has_responded(day, group_id, sender.id)
        self.state.mark_response(
            day,
            group_id,
            sender.id,
            {
                "branch_code": parsed.get("id"),
                "branch_name": parsed.get("location"),
                "pension_paid": parsed.get("pension", 0.0),
                "trade_uah": parsed.get("trade", 0.0),
                "prepayment_units": parsed.get("subscription", 0.0),
            },
            now,
        )

        group_meta = self.cfg.group_by_id(group_id)
        group_title = group_meta.title if group_meta else group_id

        sheet_ok = False
        if self._sheets:
            try:
                self._sheets.append_report(
                    day,
                    now,
                    group_title,
                    person.display_name,
                    sender.id,
                    parsed,
                    raw,
                )
                sheet_ok = True
            except Exception as exc:
                logger.error("Помилка запису в таблицю: %s", exc)

        logger.info(
            f"Звіт OK: {person.display_name} [{group_id}] "
            f"пенсія={parsed.get('pension', 0.0)} "
            f"торгівля={parsed.get('trade', 0.0)} "
            f"передплата={parsed.get('subscription', 0.0)} → таблиця=так"
        )

        if not already:
            await self._schedule_followups(
                day, group_id, sender.id, person, parsed, event
            )

    async def _schedule_followups(
        self,
        day: date,
        group_id: str,
        telegram_id: int,
        person: Person,
        parsed: ParsedReport,
        event: events.NewMessage.Event,
    ) -> None:
        checks: list[tuple[str, list]] = []
        if parsed.zero_trade:
            checks.append(
                ("trade", self.cfg.messages.get("zero_trade_followups", []))
            )
        if parsed.zero_prepayment:
            checks.append(
                ("prepayment", self.cfg.messages.get("zero_prepayment_followups", []))
            )

        for kind, templates in checks:
            if not templates:
                continue
            key = (day.isoformat(), group_id, telegram_id, kind)
            if key in self._followup_tasks:
                continue

            delay = random.randint(
                self.cfg.followup_delay_min_sec,
                self.cfg.followup_delay_max_sec,
            )
            self._followup_tasks[key] = asyncio.create_task(
                self._send_followup_after_delay(
                    delay, person, random.choice(templates), event, key
                )
            )

    async def _send_followup_after_delay(
        self,
        delay: int,
        person: Person,
        template: str,
        event: events.NewMessage.Event,
        key: tuple[str, str, int, str],
    ) -> None:
        try:
            await asyncio.sleep(delay)
            text = template.format(
                name=person.display_name,
                username=person.username or "",
            )
            await event.reply(text)
        finally:
            self._followup_tasks.pop(key, None)

    def _format_mentions(self, group_id: str, telegram_ids: list[int]) -> str:
        parts: list[str] = []
        for tid in telegram_ids:
            person = self._person_by_group_user.get((group_id, tid))
            if person:
                if person.username:
                    parts.append(f"@{person.username}")
                else:
                    # Замість "id" підставляємо реальне ім'я з конфігу:
                    parts.append(f"[{person.display_name}](tg://user?id={tid})")
            else:
                parts.append(f"[Користувач {tid}](tg://user?id={tid})")
        return " ".join(parts)
