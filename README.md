# Telegram Shift Reports (userbot)

Userbot для збору щоденних звітів з робочих чатів Telegram, запис у Google Sheets, нагадування і теги.

**Що робить**
- Приймає структуровані звіти від працівників у групах.
- Записує дані у Google Sheets.
- Надсилає нагадування, теги та особисті повідомлення за розкладом.

**Структура проєкту**
- `src/` — основний код (бот, аутентифікація, парсер, робота з Google Sheets, стан у SQLite).
- `config/` — YAML-конфіги: `settings.yaml`, `groups.yaml`, `people.yaml`, `branches.yaml`, `messages.yaml`.
- `credentials/` — сервісний аккаунт Google (service_account.json).
- `data/` — локальні дані та база `state.db`.

Prerequisites / Встановлення

1. Створіть та активуйте віртуальне оточення (Windows PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

2. Налаштуйте `.env` (скопіюйте із `.env.example` якщо є) і заповніть обов'язкові змінні.

Обов'язкові змінні оточення (мінімум):
- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `TELEGRAM_PHONE` (формат +380... або локальний 0...)
- `WORK_GROUP_STATIONARY_ID` (ID або @username)
- `WORK_GROUP_MOBILE_ID` (ID або @username)

Для Google Sheets (якщо плануєте використовувати інтеграцію):
- `GOOGLE_SPREADSHEET_ID`
- `GOOGLE_SERVICE_ACCOUNT_FILE` (за замовчуванням `credentials/service_account.json`)
- опціонально: `GOOGLE_REPORTS_SHEET_NAME` / `GOOGLE_SHEET_NAME`, `GOOGLE_PLANS_SHEET_NAME`

Запуск

1. Увійдіть в Telegram (потрібно один раз, або при зміні сесії):

```powershell
python -m src.login --reset   # якщо потрібно видалити стару сесію і зайти заново
```

2. Запустіть бота:

```powershell
python -m src.main
```

Команди в робочому чаті (якщо `schedule_enabled: false` — використовуються вручну):

- `!нагадування` — надсилання нагадування у групу
- `!хто` — теги тих, хто не відписав
- `!лс` — надіслати особисті повідомлення тим, хто не здав звіт
- `!скинути` — скинути стан поточного дня
- `!допомога` — список команд

Ручні виклики (локально, коли `src.main` зупинено):

```powershell
python -m src.trigger reminder
python -m src.trigger missing
python -m src.trigger dm
python -m src.trigger all
python -m src.trigger reset-day
```

Конфіги
- `config/settings.yaml` — основні опції (timezone, schedule_enabled, reminder_time, missing_check_time, dm_time та ін.). Додавайте `enabled_metrics` якщо потрібно моніторити лише окремі показники.
- `config/groups.yaml`, `config/people.yaml`, `config/branches.yaml`, `config/messages.yaml` — наповніть згідно з вашою структурою.

Куди дивитися в коді (корисні файли)
- `src/app.py` — головна логіка бота, обробка повідомлень і розклад.
- `src/parser.py` — парсер текстових звітів (витяг ID, локації та сум).
- `src/sheets.py` — робота з Google Sheets.
- `src/state.py` — збереження стану у SQLite (`data/state.db`).
- `src/env_setup.py` — валідація `.env` і нормалізація телефонів.

Найважливіші зауваження після перевірки коду
- `config/settings.yaml` повинен містити ключі `missing_check_time` і `dm_time` — вони використовуються напряму у `config_loader.load_config()` без значень за замовчуванням; переконайтеся, що вони є, інакше буде KeyError.
- README доповнився інструкціями по налаштуванню `.env` та Google Sheets.
- Парсер знаходиться в `src/parser.py`; якщо помітите, що якісь локації або індекси не розпізнаються — додайте їх у `VALID_LOCATIONS` / `VALID_INDICES`.

Тестування та відлагодження
- Для перевірки парсера можна швидко викликати його з Python REPL:

```powershell
python -c "from src.parser import parse_report; print(parse_report('77503 Долина 3 пенсія 120 торгівля 1500 передплата 10'))"
```

- Логи виводяться через `logging` — налаштування рівня логів у `src/main.py`.

Питання чи потрібна допомога?
Якщо хочеш — можу:
- Запустити локальний quick-smoke test (потрібні робочі значення в `.env`),
- Покрити парсер тестами або додати приклади у `tests/`,
- Автоматично перевірити відсутні ключі в `config/settings.yaml` і запропонувати шаблон.

---
Updated: автоматично оновлено README в репозиторії.

