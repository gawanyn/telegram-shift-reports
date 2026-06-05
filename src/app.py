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
        self._group_entities: dict[str, any] = {}
        self._sheets: SheetsWriter | None = None
        self._scheduler = AsyncIOScheduler(timezone=self.tz)
        self._followup_tasks: dict[tuple[str, str, int, str], asyncio.Task] = {}
        self._my_id: int | None = None
        self.daily_plans: dict = {}
        logger.info("Завантажено планів для %d відділень", len(self.daily_plans))

    async def start(self) -> None:
        await require_authorized(self.client)
        me = await self.client.get_me()
        self._my_id = me.id
        await self._resolve_groups()
        await self._resolve_people_ids()
        try:
            self._sheets = SheetsWriter()
            # 🔄 Завантажуємо плани через правильний, робочий об'єкт:
            self.daily_plans = self._sheets.load_daily_plans()
            logger.info("Завантажено планів для %d відділень", len(self.daily_plans))
        except Exception as exc:
            logger.warning("Google Sheets вимкнено: %s", exc)
            self._sheets = None
            self.daily_plans = {}

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

        await self._catch_up_missed_reports()

        await self.client.run_until_disconnected()

    async def _resolve_groups(self) -> None:
        try:
            logger.info("📦 Завантаження діалогів Telegram для пошуку чатів за назвою...")
            dialogs = await self.client.get_dialogs()
        except Exception as e:
            logger.error("⚠️ Не вдалося завантажити діалоги: %s", e)
            dialogs = []

        for group in self.cfg.groups:
            if group.chat_id is None:
                raise RuntimeError(
                    f"Не задано {group.env_var} у .env для групи «{group.title}»"
                )

            entity = None
            target_title = group.title.strip().lower() if group.title else ""

            # 1. Спробувати знайти чат у локальному кеші за назвою
            if target_title:
                for d in dialogs:
                    if d.name and d.name.strip().lower() == target_title:
                        entity = d.entity
                        break

            # 2. Якщо заданий username, спробувати його напряму
            if not entity and isinstance(group.chat_id, str) and group.chat_id.startswith("@"): 
                try:
                    entity = await self.client.get_entity(group.chat_id)
                except Exception as exc:
                    logger.debug("Не вдалося знайти username %s: %s", group.chat_id, exc)

            # 3. Якщо заданий числовий ID, пробуємо різні варіанти форматів
            if not entity:
                raw_chat_id = str(group.chat_id).strip()
                numeric_id = None
                try:
                    if raw_chat_id.startswith("-100"):
                        numeric_id = int(raw_chat_id)
                    elif raw_chat_id.startswith("-"):
                        numeric_id = int(raw_chat_id)
                    else:
                        numeric_id = int(raw_chat_id)
                except ValueError:
                    numeric_id = None

                if numeric_id is not None:
                    allowed_ids = {numeric_id, self._normalize_chat_id(numeric_id)}
                    if numeric_id > 0:
                        allowed_ids.update({-numeric_id, int(f"-100{numeric_id}")})
                    elif str(numeric_id).startswith("-100"):
                        allowed_ids.add(int(str(numeric_id).replace("-100", "-")))

                    for d in dialogs:
                        if d.id in allowed_ids or self._normalize_chat_id(d.id) in allowed_ids:
                            entity = d.entity
                            break

                # 4. Якщо все ще нічого, пробуємо прямий виклик get_entity для ID
                if not entity and numeric_id is not None:
                    candidates = [numeric_id]
                    if numeric_id > 0:
                        candidates.append(int(f"-100{numeric_id}"))
                        candidates.append(-numeric_id)
                    elif str(numeric_id).startswith("-100"):
                        candidates.append(int(str(numeric_id).replace("-100", "-")))

                    for candidate in candidates:
                        try:
                            entity = await self.client.get_entity(candidate)
                            break
                        except Exception:
                            continue

            if not entity:
                raise ValueError(
                    f"❌ КАТАСТРОФА: Бот не зміг знайти чат «{group.title}» ні за назвою, ні за ID.\n"
                    f"Переконайтеся, що ви з цього акаунта зайшли в чат або правильно вказали {group.env_var}."
                )

            self._group_entities[group.id] = entity
            chat_id = entity.id
            group.chat_id = chat_id

            for cid in self._chat_ids_for_matching(int(chat_id)):
                self._chat_to_group[cid] = group.id

            logger.info("✅ Чат «%s» успішно підключено! (ID: %s)", group.title, chat_id)

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
        group_id = self._chat_to_group.get(chat_id)
        if group_id is not None:
            return group_id
        return self._chat_to_group.get(self._normalize_chat_id(chat_id))

    def _chat_ids_for_matching(self, chat_id: int) -> list[int]:
        ids = {chat_id}
        raw = str(chat_id)

        if chat_id > 0:
            ids.add(-chat_id)
            ids.add(int(f"-100{chat_id}"))
        elif raw.startswith("-100"):
            ids.add(int(raw[4:]))
            ids.add(int("-" + raw[4:]))
        else:
            ids.add(abs(chat_id))
            ids.add(int(f"-100{abs(chat_id)}"))

        return sorted(ids)

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
        day = today_local(self.cfg, self._now())
        
        # 1. Отримуємо список людей, які сьогодні мають працювати
        expected = expected_people_today(self.cfg, group.id, day)
        
        # 🔥 ФІЛЬТР: залишаємо тільки тих, хто зараз не у відпустці/лікарняному
        expected = [p for p in expected if p.is_active]
        
        if not expected:
            logger.info("[%s] %s: за графіком сьогодні вихідний у всіх відділень", group.id, day)
            return

        # 2. Обходимо message_for_group і беремо СИРИЙ словник повідомлень групи
        group_msgs = self.cfg.messages.get(group.id, {}) if hasattr(self.cfg, "messages") else {}
        
        # Якщо структура в конфігу плоска, або шукаємо в підгрупі:
        if not group_msgs and hasattr(self.cfg, "messages"):
            raw_reminder = self.cfg.messages.get("reminder")
        else:
            raw_reminder = group_msgs.get("reminder")

        # Якщо не знайшли в словнику, падаємо на стандартний метод
        if not raw_reminder:
            raw_reminder = self.cfg.message_for_group(group.id, "reminder")
        
        # 3. Вибираємо випадковий рядок (тепер це точно буде чистий список або рядок)
        if isinstance(raw_reminder, (list, tuple)):
            base_text = random.choice(raw_reminder)
        else:
            base_text = str(raw_reminder)
        
        # 4. Формуємо красивий список працюючих відділень з тегами
        lines = []
        for person in expected:
            # Створюємо клікабельний пуш-тег
            if person.username:
                mention = f"@{person.username}"
            elif person.telegram_id:
                mention = f"[{person.display_name}](tg://user?id={person.telegram_id})"
            else:
                mention = person.display_name
                
            # Беремо індекс ВПЗ, якщо він є
            branch = person.branch_code if person.branch_code else "ВПЗ"
            
            lines.append(f"🏢 {branch} — {mention}")
            
        branches_list = "\n".join(lines)
        
        # 5. Склеюємо все до купи через акуратну тонку лінію-роздільник
        final_text = (
            f"{base_text}\n"
            f"⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
            f"📋 **Сьогодні на контролі:**\n"
            f"{branches_list}"
        )
        
        # 6. Надсилаємо сформований текст у чат
        await self.client.send_message(group.chat_id, final_text, parse_mode="md")
        
        self.state.mark_reminder_sent(day, group.id)
        logger.info("Нагадування з випадковим текстом та списком [%s] за %s", group.id, day)

    async def job_tag_missing(self) -> None:
        for group in self.cfg.groups:
            await self._job_tag_missing_for_group(group)

    async def _job_tag_missing_for_group(self, group: ChatGroup) -> None:
        day = today_local(self.cfg, self._now())
        
        # 1. Беремо тільки тих, чиї ВІДДІЛЕННЯ працюють СЬОГОДНІ (перевірка графіка)
        expected_today = expected_people_today(self.cfg, group.id, day)
        
        # Відсіюємо відпускників з тих, хто сьогодні за графіком працює
        all_people = [p for p in expected_today if getattr(p, 'is_active', True)]
        
        # 2. 🔥 Групуємо людей за індексом відділення (branch_code)
        branch_status = {}
        for person in all_people:
            if not person.branch_code or not person.telegram_id:
                continue
            
            if person.branch_code not in branch_status:
                branch_status[person.branch_code] = {
                    "responded": False,
                    "telegram_ids": []
                }
            
            # Додаємо ID працівника до його відділення
            branch_status[person.branch_code]["telegram_ids"].append(person.telegram_id)
            
            # Якщо хоча б хтось ОДИН із цього відділення здав звіт — все відділення зелене!
            if self.state.has_responded(day, group.id, person.telegram_id):
                branch_status[person.branch_code]["responded"] = True

        # 3. Збираємо ID боржників тільки з тих відділень, які не здали
        missing_ids = []
        for b_code, status in branch_status.items():
            if not status["responded"]:
                # Якщо звіт не здано, тегаємо всіх прив'язаних до цього відділення
                # (бо бот не знає, чия сьогодні зміна)
                missing_ids.extend(status["telegram_ids"])

        # Якщо всі відділення закриті
        if not missing_ids:
            await self.client.send_message(
                group.chat_id, 
                "✅ Всі відділення групи здали звіти! Дякую."
            )
            return

        # Формуємо красивий список тегів
        mentions = self._format_mentions(group.id, missing_ids)
        template = self.cfg.message_for_group(group.id, "missing_in_group")
        
        current_time_str = self._now().strftime("%H:%M")
        
        await self.client.send_message(
            group.chat_id,
            template.format(mentions=mentions, time=current_time_str),
            parse_mode="md"
        )

    async def job_send_dm(self) -> None:
        for group in self.cfg.groups:
            await self._job_send_dm_for_group(group)

    async def _job_send_dm_for_group(self, group: ChatGroup) -> None:
        day, _ = await self._init_today_state(group.id)
        
        # Отримуємо тільки активних
        all_people = [p for p in self.cfg.people if p.group == group.id and p.is_active]
        
        # 🔥 Така сама логіка групування для особистих повідомлень
        branch_status = {}
        for person in all_people:
            if not person.branch_code or not person.telegram_id:
                continue
            
            if person.branch_code not in branch_status:
                branch_status[person.branch_code] = {
                    "responded": False,
                    "telegram_ids": []
                }
            
            branch_status[person.branch_code]["telegram_ids"].append(person.telegram_id)
            
            if self.state.has_responded(day, group.id, person.telegram_id):
                branch_status[person.branch_code]["responded"] = True

        text = self.cfg.message_for_group(group.id, "dm_missing")
        
        # Відправляємо повідомлення тільки тим, чиє відділення ще не відзвітувало
        for b_code, status in branch_status.items():
            if not status["responded"]:
                for tid in status["telegram_ids"]:
                    # Перевіряємо, чи ми вже писали цій людині сьогодні
                    if tid in self.state.pending_dm(day, group.id):
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
            "!плани_апдейт": lambda: self._cmd_reload_plans(event),
            "!айді": lambda: self._cmd_get_ids(event, group_id),
            "!ids": lambda: self._cmd_get_ids(event, group_id),
            "!чистка": lambda: self._cmd_clear_messages(event),
            "!clear": lambda: self._cmd_clear_messages(event),
        }
        if cmd in ("!допомога", "!help"):
            await event.reply(
                "Команди:\n"
                "!нагадування — нагадування в чат\n"
                "!хто — теги без звіту\n"
                "!лс — особисті повідомлення\n"
                "!скинути — скинути стан на сьогодні\n"
                "!айді — отримати список ID учасників у Збережені\n"
                "!чистка [число] — видалити останні повідомлення (макс 50)"
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

    async def _cmd_get_ids(self, event: events.NewMessage.Event, group_id: str) -> None:
        """Збирає ID всіх учасників чату і відправляє адміну в Збережені повідомлення"""
        import os
        
        chat = await event.get_chat()
        lines = [f"📋 **Список ID учасників групи «{chat.title}»:**\n"]
        
        # Проходимося по всіх учасниках чату
        async for user in self.client.iter_participants(chat):
            if not user.bot:
                # Клеїмо ім'я, юзернейм та номер телефону, якщо він відкритий
                name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "Без імені"
                username = f" (@{user.username})" if user.username else ""
                phone = f" 📱+{user.phone}" if user.phone else ""
                
                lines.append(f"👤 {name}{username}{phone} ➡️ `{user.id}`")
                
        text = "\n".join(lines)
        
        try:
            # Відправляємо собі у "Saved Messages" ("me" - це вбудований аліас Телеграму для збережених)
            if len(text) > 4000:
                # Якщо повідомлення занадто довге (більше 4000 символів), відправляємо як текстовий файл
                file_name = f"ids_{group_id}.txt"
                with open(file_name, "w", encoding="utf-8") as f:
                    f.write(text.replace("**", "").replace("`", ""))
                
                await self.client.send_message("me", f"Файл з ID для групи {group_id}", file=file_name)
                os.remove(file_name)
            else:
                # Якщо влазить у ліміт - відправляємо звичайним текстом
                await self.client.send_message("me", text)
                
            # Відповідаємо в робочий чат, щоб ти зрозумів, що команда пройшла, і за 3 секунди прибираємо за собою
            msg = await event.reply("✅ Список ID успішно надіслано вам у **Збережені повідомлення**.")
            await asyncio.sleep(3)
            await msg.delete()
            
        except Exception as e:
            logger.error("Помилка відправки ID: %s", e)
            await event.reply("❌ Не вдалося відправити список ID. Перевірте логи.")

    async def _cmd_clear_messages(self, event: events.NewMessage.Event) -> None:
        """Видаляє задану кількість ВЛАСНИХ останніх повідомлень у чаті."""
        raw = (event.message.message or "").strip()
        parts = raw.split()
        
        # За замовчуванням видаляємо 5 повідомлень
        limit = 5
        if len(parts) > 1 and parts[1].isdigit():
            limit = int(parts[1])
            
        # Запобіжник, щоб випадково не знести забагато
        if limit > 50:
            limit = 50
            
        try:
            chat = await event.get_chat()
            
            # 🔥 ДОДАНО ФІЛЬТР: from_user='me'. 
            # Бот проігнорує чужі звіти і знайде саме `limit` твоїх останніх повідомлень.
            messages = await self.client.get_messages(chat, limit=limit, from_user='me', max_id=event.id)
            
            if messages:
                await self.client.delete_messages(chat, messages)
                logger.info("🧹 Видалено %d ВЛАСНИХ повідомлень у чаті «%s»", len(messages), chat.title)
                
        except Exception as e:
            logger.error("Помилка видалення повідомлень: %s", e)
            err_msg = await event.reply("❌ Не вдалося видалити повідомлення. Перевірте логи.")
            await asyncio.sleep(3)
            await err_msg.delete()

    async def _cmd_reload_plans(self, event: events.NewMessage.Event) -> None:
        if self._sheets:
            self.daily_plans = self._sheets.load_daily_plans()
            await event.reply(f"🔄 Кеш планів успішно оновлено! Завантажено відділень: {len(self.daily_plans)}")
            logger.info("Адміністратор оновив кеш планів з Google Sheets")
        else:
            await event.reply("❌ Помилка: модуль Google Sheets не активний.")

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
        retrospective = getattr(event, "retrospective", False)
        if not person:
            if not retrospective:
                logger.info(
                    "Ігнор id=%s у [%s]: немає в config/people.yaml",
                    sender.id,
                    group_id,
                )
                return
            person = Person(
                group=group_id,
                display_name=sender_name,
                username=getattr(sender, "username", None),
                telegram_id=sender.id,
                branch_code="",
            )
            logger.info(
                "Ретроспективно оброблюю повідомлення від невідомого користувача %s id=%s",
                sender_name,
                sender.id,
            )

        day = today_local(self.cfg, self._now())
        expected = expected_people_today(self.cfg, group_id, day)
        if person not in expected and not retrospective:
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

        b_code = parsed.get("id", "")
        if b_code:
            b_code = str(b_code).strip()
            
        fact_trade = float(parsed.get("trade", 0.0))
        fact_sub = float(parsed.get("subscription", 0.0))

        # Шукаємо місячні плани у нашому кеші daily_plans
        branch_plans = self.daily_plans.get(b_code)

        # Отримуємо блоки з конфігу messages.yaml
        msg_group = self.cfg.messages.get(group_id, {}) if hasattr(self.cfg, "messages") else {}
        markers = msg_group.get("performance_markers", self.cfg.messages.get("performance_markers", {}))
        cries = msg_group.get("battle_cries", self.cfg.messages.get("battle_cries", {}))

        if branch_plans and markers and cries:
            plan_trade = branch_plans.get("trade", 0.0)
            plan_sub = branch_plans.get("prepayment", 0.0)

            # 1. Розрахунок та статус для ТОРГІВЛІ
            p_trade = round((fact_trade / plan_trade) * 100) if plan_trade > 0 else 100
            if p_trade >= 100:
                status_trade = "overfulfilled"
            elif p_trade >= 90:
                status_trade = "normal"
            elif p_trade >= 50:
                status_trade = "attention"
            else:
                status_trade = "critical"
            marker_trade = markers.get(status_trade, "")

            # 2. Розрахунок та статус для ПЕРЕДПЛАТИ
            p_sub = round((fact_sub / plan_sub) * 100) if plan_sub > 0 else 100
            if p_sub >= 100:
                status_sub = "overfulfilled"
            elif p_sub >= 90:
                status_sub = "normal"
            elif p_sub >= 50:
                status_sub = "attention"
            else:
                status_sub = "critical"
            marker_sub = markers.get(status_sub, "")

            # 3. Визначаємо фінальний бойовий клич дня
            
            # ПЕРЕВІРКА НА ЧИСТИЙ НУЛЬ (якщо факт = 0, а план був > 0)
            has_zero = (fact_trade == 0 and plan_trade > 0) or (fact_sub == 0 and plan_sub > 0)

            if has_zero:
                # Якщо є хоча б один нуль — беремо фразу з блоку zero_alert
                cry = random.choice(cries.get("zero_alert", ["🛑 Нуль у звіті неприпустимий. Будь ласка, активізуйте роботу!"]))
            
            # Якщо нулів немає, але є критичне просідання (<50%)
            elif status_trade == "critical" or status_sub == "critical":
                cry = random.choice(cries.get("critical_alert", ["🚨 Необхідно терміново підтягнути показники!"]))
            
            # Якщо обидва показники відпрацьовані чудово (>=90%)
            elif status_trade in ("overfulfilled", "normal") and status_sub in ("overfulfilled", "normal"):
                cry = random.choice(cries.get("both_win", ["💪 Чудова робота, колеги!"]))
            
            # Середній варіант (50-89%, зону уваги)
            else:
                cry = random.choice(cries.get("one_win", ["📌 Звіт прийнято. Працюємо далі."]))

            # 4. Збираємо красиве табло аналітики
            reply_text = (
                f"✅ **Звіт прийнято і записано!**\n"
                f"⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
                f"📊 **Аналітика виконання плану:**\n"
                f"🛒 **Торгівля:** {fact_trade:.0f} з {plan_trade:.0f} грн ({p_trade}%) — {marker_trade}\n"
                f"📰 **Передплата:** {fact_sub:.0f} з {plan_sub:.0f} шт ({p_sub}%) — {marker_sub}\n"
                f"⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
                f"{cry}"
            )
        else:
            # Якщо планів немає в таблиці — звичайна стандартна відповідь
            reply_text = (
                f"✅ **Звіт прийнято і записано!**\n"
                f"🛒 Торгівля: {fact_trade:.0f} грн.\n"
                f"📰 Передплата: {fact_sub:.0f} шт.\n"
                f"*(План на сьогодні не знайдено)*"
            )

        # Надсилаємо аналітику миттєво як підтвердження прийому звіту
        await event.reply(reply_text)

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
        parsed: dict,
        event: events.NewMessage.Event,
    ) -> None:
        checks: list[tuple[str, list]] = []
        
        # Перевіряємо нулі через .get() як у звичайному словнику
        fact_trade = float(parsed.get("trade", 0.0))
        fact_sub = float(parsed.get("subscription", 0.0))
        
        if fact_trade == 0:
            checks.append(
                ("trade", self.cfg.messages.get("zero_trade_followups", []))
            )
        if fact_sub == 0:
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
            
            # 🔥 Формуємо повноцінний клікабельний тег для сповіщення
            if person.username:
                mention = f"@{person.username}"
            elif person.telegram_id:
                mention = f"[{person.display_name}](tg://user?id={person.telegram_id})"
            else:
                mention = person.display_name

            # Підставляємо тег замість звичайного тексту
            text = template.format(
                name=mention,
                username=person.username or "",
            )
            
            # Обов'язково вказуємо parse_mode="md", щоб Телеграм перетворив посилання на тег
            await event.reply(text, parse_mode="md")
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
    
    async def _catch_up_missed_reports(self) -> None:
        """Автоматично перевіряє та обробляє звіти, надіслані сьогодні з 12:00 до моменту запуску бота"""
        now_local = self._now()
        day = today_local(self.cfg, now_local)
        
        target_time = datetime(
            day.year, day.month, day.day, 12, 0, 0, tzinfo=self.tz
        )
        
        logger.info("🤖 Запуск ретроспективної перевірки повідомлень з 12:00 за сьогодні (%s)...", day)

        for group in self.cfg.groups:
            # Беремо готову сутність, яку ми залізобетонно витягнули в _resolve_groups
            chat_entity = self._group_entities.get(group.id)
            if not chat_entity:
                logger.warning("⚠️ Не знайдено збереженої сутності для групи [%s]", group.title)
                continue

            processed_reports: list[tuple[int, str, str, str]] = []
            processed_report_keys: set[tuple[int, str]] = set()
            if self._sheets:
                try:
                    processed_reports = self._sheets.load_processed_reports(
                        day, group.title, group.id
                    )
                    logger.info(
                        "[%s] Завантажено з Google Sheets вже опрацьованих звітів: %d",
                        group.id,
                        len(processed_reports),
                    )
                    for telegram_id, report_text, row_date, row_time in processed_reports:
                        logger.info(
                            "[%s] Опрацьований звіт з таблиці: sender_id=%s date=%s time=%s",
                            group.id,
                            telegram_id,
                            row_date,
                            row_time,
                        )
                        # 🔥 Новий унікальний ключ: (ID відправника, Дата, Час)
                        processed_report_keys.add((telegram_id, str(row_date).strip(), str(row_time).strip()))
                    
                    logger.info(
                        "[%s] 📊 Завантажено %d ключів для дублікат-перевірки (sender_id, date, time)",
                        group.id,
                        len(processed_report_keys),
                    )
                    
                    logger.info(
                        "[%s] 📊 Завантажено %d ключів для дублікат-перевірки (sender_id, normalized_text):",
                        group.id,
                        len(processed_report_keys),
                    )
                    for key in sorted(processed_report_keys)[:5]:
                        logger.info(
                            "[%s] Ключ у таблиці: sender_id=%s text=%s",
                            group.id,
                            key[0],
                            key[1][:80],
                        )
                except Exception as e:
                    logger.warning(
                        "[%s] Не вдалося завантажити опрацьовані звіти з Google Sheets: %s",
                        group.id,
                        e,
                    )

            count_processed = 0
            scanned_messages = 0
            count_bot_messages = 0
            count_no_sender = 0
            count_already_responded = 0
            count_already_in_sheet = 0
            count_empty_text = 0
            count_not_recognized = 0
            try:
                # Читаємо історію через 100% валідну сутність чату
                async for message in self.client.iter_messages(chat_entity):
                    scanned_messages += 1
                    msg_date_local = message.date.astimezone(self.tz)

                    if msg_date_local < target_time:
                        break

                    if message.sender_id == self._my_id:
                        count_bot_messages += 1
                        logger.info("[%s] 🤖 Повідомлення від бота, пропускаємо", group.id)
                        continue

                    sender_id = message.sender_id
                    if not sender_id:
                        count_no_sender += 1
                        logger.info("[%s] ⚠️ Без sender_id, пропускаємо", group.id)
                        continue

                    raw_text = " ".join(str(message.message or "").split()).strip()
                    if not raw_text:
                        count_empty_text += 1
                        logger.info("[%s] ⚠️ Порожнє повідомлення від %s", group.id, sender_id)
                        continue

                    # 🔥 Формуємо дату та час повідомлення точно в такому форматі, як вони записані в таблиці
                    msg_date_str = msg_date_local.strftime("%Y-%m-%d")
                    msg_time_str = msg_date_local.strftime("%H:%M:%S")

                    # Новий ключ пошуку
                    search_key = (sender_id, msg_date_str, msg_time_str)
                    
                    if search_key in processed_report_keys:
                        count_already_in_sheet += 1
                        logger.info(
                            "[%s] ✅ Знайдено у таблиці дублікат за часом: sender_id=%s date=%s time=%s",
                            group.id,
                            sender_id,
                            msg_date_str,
                            msg_time_str,
                        )
                        if not self.state.has_responded(day, group.id, sender_id):
                            self.state.ensure_day(day, group.id, [sender_id])
                            parsed = parse_report(raw_text) or {}
                            self.state.mark_response(day, group.id, sender_id, parsed, msg_date_local)
                        continue

                    # Then check state (for messages after the Google Sheets check)
                    if self.state.has_responded(day, group.id, sender_id):
                        count_already_responded += 1
                        logger.info(
                            "[%s] ⏭️ У state вже відповідав: sender_id=%s",
                            group.id,
                            sender_id,
                        )
                        continue

                    parsed = parse_report(raw_text)
                    if parsed is None:
                        count_not_recognized += 1
                        logger.debug(
                            "Ретроспективне повідомлення [%s] від %s не розпізнано як звіт: %s",
                            group.id,
                            sender_id,
                            raw_text[:120],
                        )
                        continue

                    logger.info(
                        "📥 Знайдено новий звіт (не у таблиці): sender_id=%s text=%s",
                        sender_id,
                        raw_text[:80],
                    )
                    
                    class SimulatedEvent:
                        def __init__(self, msg, cid, client):
                            self.message = msg
                            self.chat_id = cid
                            self.id = msg.id
                            self._client = client
                            self.retrospective = True

                        # 🔥 ФІКС: тепер заглушка приймає будь-які аргументи і просто їх ігнорує
                        async def reply(self, *args, **kwargs):
                            pass

                        async def get_sender(self):
                            return await self.message.get_sender()

                    simulated_event = SimulatedEvent(message, message.chat_id, self.client)
                    await self._handle_group_message(simulated_event)
                    count_processed += 1
                    
            except Exception as e:
                logger.error("⚠️ Не вдалося прочитати історію чату [%s]: %s", group.title, e)
                continue

            logger.info(
                "[%s] Ретроспективну перевірку завершено. Статистика %d повідомлень: "
                "від_бота=%d, без_sender=%d, порожні=%d, вже_відповідь=%d, у_таблиці=%d, не_розпізнані=%d, обработено=%d",
                group.id,
                scanned_messages,
                count_bot_messages,
                count_no_sender,
                count_empty_text,
                count_already_responded,
                count_already_in_sheet,
                count_not_recognized,
                count_processed,
            )