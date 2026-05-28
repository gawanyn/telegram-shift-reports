# Telegram Shift Reports (userbot)

Два робочі чати, звіти в Google Таблицю, нагадування та теги.

## Запуск

```powershell
cd C:\Users\Taras Shcherban\Projects\telegram-shift-reports
.\.venv\Scripts\Activate.ps1
python -m src.login --reset   # лише перший раз
python -m src.main
```

## Керування (без годинника)

Поки в `config/settings.yaml` стоїть `schedule_enabled: false`, у **робочому чаті** зі свого акаунта напишіть:

| Команда | Дія |
|---------|-----|
| `!нагадування` | Нагадування про звіт |
| `!хто` | Теги тих, хто не відписав |
| `!лс` | Особисті повідомлення |
| `!скинути` | Скинути стан сьогодні (повторний тест) |
| `!допомога` | Список команд |

Звіт у групі обробляється автоматично (якщо людина в `people.yaml`).

## Продакшн (пізніше)

У `config/settings.yaml`:

```yaml
schedule_enabled: true
branch_calendar_enabled: true
reminder_time: "17:00"
missing_check_time: "19:00"
dm_time: "20:00"
```

## Конфіги

- `config/people.yaml`, `config/branches.yaml`, `config/messages.yaml`
- `.env` — API, два id чатів, Google Sheets

## Формат звіту

```
77503, Долина 3, Виплачена пенсія — 120, Торгівля — 1500 грн, Передплата — 10 шт.
```
